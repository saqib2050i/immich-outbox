"""Tracing one file from Immich to the phone.

The thing this exists to stop: a file that arrived damaged and a file this
service damaged looked identical from every screen here. Five hours went
missing from a library and the only way to tell which had happened was to
pull a copy off the phone over a cable and parse its EXIF by hand.
"""

import pytest

from conftest import asset


def test_a_name_that_matches_nothing_says_so(rig):
    """The likeliest thing to happen at this box, and it used to be
    indistinguishable from a file with no problems."""
    from app import db, diagnose
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])

    row, near = diagnose.find("nope.jpg")
    assert row is None


def test_a_near_miss_is_offered(rig):
    from app import db, diagnose
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])

    row, near = diagnose.find("PXL_20230101_025759225.JPEG")
    assert row is None
    assert "PXL_20230101_025759225.jpg" in near


def test_a_pasted_path_still_finds_the_file(rig):
    """Pasting from a file manager is how the name arrives half the time."""
    from app import db, diagnose
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])

    row, near = diagnose.find("/mnt/user/photos/PXL_20230101_025759225.jpg")
    assert row is None
    assert near == ["PXL_20230101_025759225.jpg"]


def test_the_case_of_the_name_does_not_matter(rig):
    from app import db, diagnose
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])
    row, _ = diagnose.find("pxl_20230101_025759225.JPG")
    assert row and row["filename"] == "PXL_20230101_025759225.jpg"


@pytest.mark.parametrize("name,when", [
    ("PXL_20230101_025759225.jpg", "2023-01-01 02:57:59"),
    ("PXL_20230122_060737908.PORTRAIT.jpg", "2023-01-22 06:07:37"),
    ("IMG_20240415_133000.jpg", "2024-04-15 13:30:00"),
    ("VID_20240415_133000.mp4", "2024-04-15 13:30:00"),
    ("20240415_133000.jpg", "2024-04-15 13:30:00"),
    ("DSC_9941.NEF", None),
])
def test_the_camera_name_is_read_as_a_local_time(name, when):
    """Once a file's own EXIF is in doubt, the name the camera gave it is
    the only independent witness there is."""
    from app import diagnose
    got = diagnose.filename_time(name)
    assert (got.strftime("%Y-%m-%d %H:%M:%S") if got else None) == when


# ---- the diagnosis itself -----------------------------------------------

def _report(src, dst=None, name="PXL_20230101_025759225.jpg", **asset_over):
    a = {"state": "queued", "taken_at": None, "exif_taken_at": None,
         "date_mismatch": 0, "forced": 0, "outbox_name": name}
    a.update(asset_over)
    rep = {"filename": name, "asset": a,
           "immich": {"ok": True, "bytes": 100, "sha256": "aaa", "exif": src}}
    if dst is not None:
        rep["outbox"] = {"present": True, "bytes": 100, "sha256": "aaa",
                         "exif": dst}
    return rep


def test_a_utc_time_in_a_local_field_is_named_for_what_it_is(rig):
    """The real case. The camera called it 02:57:59; the file says 07:57:59
    and also says its zone is +05:00. Those two cannot both be true, and
    the difference is exactly the offset."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 07:57:59",
        "OffsetTimeOriginal": "+05:00"}))
    bad = [f["text"] for f in out if f["level"] == "bad"]
    assert any("+05:00" in t and "UTC" in t for t in bad), out


def test_a_file_that_agrees_with_its_own_name_is_reported_as_fine(rig):
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 02:57:59",
        "OffsetTimeOriginal": "+05:00"}))
    assert any(f["level"] == "ok" for f in out), out
    assert not [f for f in out if f["level"] == "bad"], out


def test_a_shift_that_is_not_the_offset_is_still_reported(rig):
    """Not every wrong date is a timezone. Saying only "5 hours out, must be
    the zone" would hide the ones that are not."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:06:14 09:00:00",
        "OffsetTimeOriginal": "+05:00"}))
    assert any(f["level"] == "bad" for f in out), out


def test_identical_copies_are_said_to_be_identical(rig):
    from app import diagnose
    exif = {"DateTimeOriginal": "2023:01:01 02:57:59"}
    out = diagnose._findings(_report(exif, dict(exif)))
    assert any("byte-for-byte" in f["text"] for f in out), out


def test_a_copy_that_changed_with_rewriting_off_is_an_alarm(rig):
    """Invariant 2a: the file passes through byte for byte unless date
    rewriting is on and this asset is flagged. Anything else is serious."""
    from app import diagnose, settings
    settings.save({"fix_dates": False})
    rep = _report({"DateTimeOriginal": "2023:01:01 02:57:59"},
                  {"DateTimeOriginal": "2023:01:01 07:57:59"})
    rep["outbox"]["sha256"] = "bbb"
    out = diagnose._findings(rep)
    bad = [f["text"] for f in out if f["level"] == "bad"]
    assert any("rewriting is OFF" in t for t in bad), out
    assert any("DateTimeOriginal" in t for t in bad), out


def test_exiftool_warnings_are_surfaced(rig):
    """Structural damage shows up there and nowhere else."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 02:57:59",
        "Warning": "Bad IFD0 directory"}))
    assert any("Bad IFD0" in f["text"] for f in out), out


def test_a_report_is_never_silent(rig):
    """A result with nothing in it is indistinguishable from one nobody
    computed, which is the failure this whole tool exists to stop."""
    from app import diagnose
    assert diagnose._findings({"filename": "x.jpg"})


def test_a_file_with_no_date_at_all_is_flagged_not_ignored(rig):
    from app import diagnose
    out = diagnose._findings(_report({"Make": "Google"}))
    assert any("no DateTimeOriginal" in f["text"] for f in out), out


# ---- through the endpoint ------------------------------------------------

def signed_in():
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    auth.set_password("a-good-password")
    c = TestClient(app)
    c.post("/api/login", json={"password": "a-good-password"})
    return c


def test_the_endpoint_refuses_an_empty_name_with_a_reason(rig):
    d = signed_in().post("/api/diagnose", json={"filename": "  "}).json()
    assert "problem" in d and "filename" in d["problem"].lower()


def test_the_endpoint_names_a_file_it_cannot_find(rig):
    from app import db
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])
    d = signed_in().post("/api/diagnose", json={"filename": "ghost.jpg"}).json()
    assert "problem" in d
    assert "ghost.jpg" in d["problem"]


def test_it_is_behind_the_session_gate_like_everything_else(rig):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post(
        "/api/diagnose", json={"filename": "x.jpg"}).status_code == 401


def test_tracing_writes_nothing_to_the_ledger(rig):
    """It reads three copies. Without `send` it must not move an asset."""
    from app import db
    db.upsert_assets([asset(1, name="PXL_20230101_025759225.jpg")])
    before = db.counts()
    signed_in().post("/api/diagnose",
                     json={"filename": "PXL_20230101_025759225.jpg"})
    assert db.counts() == before
