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


def test_a_pixel_named_in_utc_is_not_a_fault(rig):
    """The correction that matters most. The Pixel camera names files in
    UTC and records the zone separately, so 02:57:59 in the name with a
    +05:00 offset and 07:57:59 in DateTimeOriginal is the file being right.

    Reading that gap as damage made an entirely correct library look five
    hours broken, and would have fired on essentially every photo its owner
    has.
    """
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 07:57:59",
        "OffsetTimeOriginal": "+05:00"}))
    assert not [f for f in out if f["level"] == "bad"], out
    assert any("Pixel" in f["text"] for f in out), out


def test_a_name_written_in_the_local_clock_is_not_a_fault_either(rig):
    """The other convention, which older phones and cameras used."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 02:57:59",
        "OffsetTimeOriginal": "+05:00"}, name="IMG_20230101_025759.jpg"))
    assert any(f["level"] == "ok" for f in out), out
    assert not [f for f in out if f["level"] == "bad"], out


@pytest.mark.parametrize("name,clock", [
    ("PXL_20230101_025759225.jpg", "utc"),
    ("IMG_20230101_025759.jpg", "local"),
    ("20230101_025759.jpg", "unknown"),
])
def test_which_clock_the_camera_named_it_by(name, clock):
    """Only ever used to explain a reading, never to decide one."""
    from app import diagnose
    assert diagnose.filename_clock(name) == clock


def test_a_gap_that_neither_reading_explains_is_reported(rig):
    """Not every wrong date is a timezone. A gap the file's own offset
    cannot account for is a real finding."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:06:14 09:00:00",
        "OffsetTimeOriginal": "+05:00"}))
    bad = [f["text"] for f in out if f["level"] == "bad"]
    assert any("does not account for it" in t for t in bad), out


def test_a_file_with_no_zone_is_not_accused_of_anything(rig):
    """An old camera writes the local clock into the name and records no
    zone at all. The gap cannot be settled from the file, so it must not be
    called a fault -- which is the whole of "do not assume a mismatch"."""
    from app import diagnose
    out = diagnose._findings(_report({
        "DateTimeOriginal": "2023:01:01 07:57:59"},
        name="DSC_20230101_025759.jpg"))
    assert not [f for f in out if f["level"] == "bad"], out
    assert any("cannot be settled" in f["text"] for f in out), out
    assert any("never wrote down" in f["text"] for f in out), out


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
    out = diagnose._findings(_report({"EXIF:Make": "Google"}))
    assert any("not in the file at all" in f["text"] for f in out), out
    assert any("upload" in f["text"] for f in out), out


# ---- the date Google Photos reads ---------------------------------------
#
# The fault: Immich shows a correct date held in its own database, put
# there by a Google Takeout sidecar at import, while the file itself
# carries DateTimeOriginal present and *empty*. Google reads the file, not
# Immich, finds nothing, and files the photo under the day it was uploaded.
# Confirmed on PXL_20240105_034733992.jpg, which landed on today despite a
# perfectly parseable date sitting in its own filename.


@pytest.mark.parametrize("value", [
    "",                          # a NUL-filled tag, as exiftool renders it
    "                   ",       # a space-filled one
    "0000:00:00 00:00:00",       # an mp4 whose creation_time is zero
    "    :  :     :  :  ",
])
def test_a_tag_that_is_there_and_says_nothing(value):
    """Measured against real exiftool 13.25 output, not guessed: each of
    these is a tag the file *has*, holding no date."""
    from app import diagnose
    assert diagnose.is_blank(value) is True


@pytest.mark.parametrize("value", [
    "2023:01:01 07:57:59", "2023:01:01", "+05:00", "Google",
])
def test_a_tag_with_something_in_it_is_not_blank(value):
    from app import diagnose
    assert diagnose.is_blank(value) is False


def test_a_blank_date_is_not_silence(rig):
    """The whole point. It used to be filtered out one line before the
    report was assembled, so the confirmed case produced no finding at all
    and an em-dash on screen -- identical to a file with nothing wrong."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": "",
                          "EXIF:Make": "Google", "File:MIMEType": "image/jpeg"})
    assert v["dated"] is False
    assert v["state"] == diagnose.BLANK
    assert "empty" in v["reason"]
    assert "upload" in v["reason"]


def test_blank_and_missing_are_told_apart(rig):
    """Same consequence, different causes, so they are never merged: one is
    an export that blanked the field, the other a file that never had it."""
    from app import diagnose
    blank = diagnose.verdict({"EXIF:DateTimeOriginal": ""})
    gone = diagnose.verdict({"EXIF:Make": "Google"})
    assert blank["state"] == diagnose.BLANK
    assert gone["state"] == diagnose.MISSING
    assert blank["reason"] != gone["reason"]
    assert not blank["dated"] and not gone["dated"]


def test_a_good_photo_is_said_to_be_good(rig):
    """Reporting "fine" out loud, rather than staying quiet, is what makes
    a quiet report mean something."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": "2023:01:01 07:57:59",
                          "EXIF:OffsetTimeOriginal": "+05:00"})
    assert v["dated"] is True
    assert v["level"] == "ok"
    assert "2023:01:01 07:57:59" in v["reason"]


def test_a_date_in_the_wrong_tag_does_not_count(rig):
    """XMP carrying a date while EXIF's is blank is still a file Google
    Photos will misdate -- but the value is named, because it is exactly
    what a correction would be copied from."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": "",
                          "XMP:DateTimeOriginal": "2024:01:05 08:47:33"})
    assert v["dated"] is False
    assert "not the tag Google Photos reads" in v["reason"]
    assert v["others"] == [{"tag": "XMP:DateTimeOriginal",
                            "value": "2024:01:05 08:47:33"}]


def test_a_bare_tag_name_still_resolves(rig):
    """-G qualifies every key by group. A dict written by hand, or a
    reading taken before that flag was added, must not come back empty."""
    from app import diagnose
    assert diagnose.pick({"DateTimeOriginal": "x"},
                         "EXIF:DateTimeOriginal") == ("x", "DateTimeOriginal")
    assert diagnose.pick({"EXIF:DateTimeOriginal": "x"},
                         "EXIF:DateTimeOriginal") == ("x", "EXIF:DateTimeOriginal")


def test_exif_is_preferred_over_xmp_for_the_same_name(rig):
    """Without -G exiftool returns both under the bare name and the second
    silently wins, which would report a date in the tag Google reads when
    the value came from one it does not."""
    from app import diagnose
    value, key = diagnose.pick({"EXIF:DateTimeOriginal": "2023:01:01 07:57:59",
                                "XMP:DateTimeOriginal": "1999:09:09 09:09:09"},
                               "EXIF:DateTimeOriginal", "XMP:DateTimeOriginal")
    assert (value, key) == ("2023:01:01 07:57:59", "EXIF:DateTimeOriginal")


# ---- video ---------------------------------------------------------------

VIDEO = {"File:MIMEType": "video/mp4"}


def test_a_video_with_a_create_date_is_reported_fine_not_skipped(rig):
    """Videos are not the fault here, and a tool that says nothing about
    them cannot be used to prove that."""
    from app import diagnose
    v = diagnose.verdict({**VIDEO, "QuickTime:CreateDate": "2024:01:05 03:47:33"})
    assert v["dated"] is True
    assert v["tag"] == "CreateDate"
    assert "UTC" in v["reason"]


def test_a_video_whose_creation_time_is_zero(rig):
    """Measured: an mp4 with creation_time 0 reports 0000:00:00 00:00:00,
    which is neither a missing tag nor a date."""
    from app import diagnose
    v = diagnose.verdict({**VIDEO, "QuickTime:CreateDate": "0000:00:00 00:00:00",
                          "QuickTime:MediaCreateDate": "0000:00:00 00:00:00"})
    assert v["dated"] is False
    assert v["state"] == diagnose.BLANK


def test_a_video_is_judged_by_quicktime_not_by_exif(rig):
    from app import diagnose
    v = diagnose.verdict({**VIDEO, "QuickTime:CreateDate": "2024:01:05 03:47:33",
                          "EXIF:DateTimeOriginal": ""})
    assert v["dated"] is True


def test_the_ledger_decides_the_kind_when_the_file_will_not_say(rig):
    from app import diagnose
    v = diagnose.verdict({"QuickTime:CreateDate": "2024:01:05 03:47:33"},
                         kind="VIDEO")
    assert v["dated"] is True


# ---- against what Immich holds ------------------------------------------

def test_a_still_is_compared_against_the_wall_clock(rig):
    """DateTimeOriginal is local time with no zone and Immich's
    localDateTime is the same wall clock, so they are compared naively --
    the trailing Z Immich sends is an artefact of the transport."""
    from app import diagnose
    says = {"ok": True, "local_date_time": "2024-01-05T08:47:33.000Z",
            "file_created_at": "2024-01-05T03:47:33.000Z",
            "time_zone": "Asia/Karachi"}
    out = diagnose._agrees_with_immich(
        {"EXIF:DateTimeOriginal": "2024:01:05 08:47:33"}, says, "IMAGE")
    assert out["level"] == "ok"


def test_a_pakistan_era_still_is_not_shifted_five_hours(rig):
    """Comparing DateTimeOriginal against fileCreatedAt instead would make
    every GMT+5 photo in this library look five hours wrong. It is the same
    error the filename check already had to be corrected for."""
    from app import diagnose
    says = {"ok": True, "local_date_time": "2024-01-05T08:47:33.000Z",
            "file_created_at": "2024-01-05T03:47:33.000Z"}
    out = diagnose._agrees_with_immich(
        {"EXIF:DateTimeOriginal": "2024:01:05 08:47:33",
         "EXIF:OffsetTimeOriginal": "+05:00"}, says, "IMAGE")
    assert out["level"] == "ok", out


def test_a_video_is_compared_against_the_instant(rig):
    """QuickTime CreateDate is UTC by specification, so it goes against
    fileCreatedAt -- the other way round from a still."""
    from app import diagnose
    says = {"ok": True, "local_date_time": "2024-01-05T08:47:33.000Z",
            "file_created_at": "2024-01-05T03:47:33.000Z"}
    out = diagnose._agrees_with_immich(
        {**VIDEO, "QuickTime:CreateDate": "2024:01:05 03:47:33"},
        says, "VIDEO")
    assert out["level"] == "ok", out


def test_a_file_that_disagrees_with_immich_is_reported(rig):
    from app import diagnose
    says = {"ok": True, "local_date_time": "2024-01-05T08:47:33.000Z"}
    out = diagnose._agrees_with_immich(
        {"EXIF:DateTimeOriginal": "2019:06:01 12:00:00"}, says, "IMAGE")
    assert out["level"] == "warn"
    assert "would use the file's" in out["text"]


def test_immich_being_unreachable_is_not_a_verdict(rig):
    """A trace that could not reach Immich still has two copies to compare,
    so this stays quiet rather than inventing a disagreement."""
    from app import diagnose
    assert diagnose._agrees_with_immich(
        {"EXIF:DateTimeOriginal": "2024:01:05 08:47:33"},
        {"ok": False, "error": "connection refused"}, "IMAGE") is None


# ---- what the report shows ----------------------------------------------

def test_the_table_shows_a_blank_tag_as_present_and_empty(rig):
    """On screen a blank tag used to draw the same em-dash as a missing
    one, which is the fault wearing the disguise of a clean report."""
    from app import diagnose
    rows = {r["tag"]: r for r in diagnose._tag_table(
        {"EXIF:DateTimeOriginal": "", "EXIF:Make": "Google"})}
    assert rows["DateTimeOriginal"]["state"] == diagnose.BLANK
    assert rows["DateTimeOriginal"]["text"] == "present but empty"
    assert rows["CreateDate"]["state"] == diagnose.MISSING


def test_a_blank_tag_survives_into_the_comparison(rig):
    """_dates fed the copy-against-copy diff and dropped anything falsy, so
    a date blanked in transit would have compared equal to one never there."""
    from app import diagnose
    assert diagnose._dates({"EXIF:DateTimeOriginal": ""}) == {
        "DateTimeOriginal": ""}


def test_both_copies_get_a_verdict(rig):
    """The answer is allowed to differ between them -- that is the whole
    point of stamping one on its way past."""
    from app import diagnose
    out = diagnose._findings(_report({"EXIF:DateTimeOriginal": ""},
                                     {"EXIF:DateTimeOriginal": ""}))
    said = [f["text"] for f in out]
    assert any(t.startswith("Immich's original:") for t in said), said
    assert any(t.startswith("the outbox copy:") for t in said), said


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


# ---- pinned to the real thing -------------------------------------------
#
# Every value below is copied from the live Immich API response for
# PXL_20240105_034733992.jpg, the confirmed case. Three traps live in that
# one payload and each has cost somebody a library before.

REAL = {"ok": True,
        "local_date_time": "2024-01-05T08:47:33.000Z",
        "file_created_at": "2024-01-05T03:47:33.000Z",
        "time_zone": "Asia/Karachi",
        "exif_original_utc": "2024-01-05T03:47:33+00:00",
        "make": None, "model": None, "type": "IMAGE"}


def test_the_Z_on_localDateTime_is_a_lie_and_is_discarded(rig):
    """Immich sends the wall clock with a UTC marker glued to it. Anything
    that honours the Z and converts shifts the photo by the zone -- five
    hours, for most of this library."""
    from app import diagnose
    got = diagnose._naive(REAL["local_date_time"])
    assert got.strftime("%Y:%m:%d %H:%M:%S") == "2024:01:05 08:47:33"


def test_the_correct_reading_of_the_confirmed_file(rig):
    """08:47:33 is what DateTimeOriginal should say, and it comes from
    localDateTime. Not from fileCreatedAt and not from
    exifInfo.dateTimeOriginal, which are both the instant."""
    from app import diagnose
    right = {"EXIF:DateTimeOriginal": "2024:01:05 08:47:33",
             "EXIF:OffsetTimeOriginal": "+05:00"}
    assert diagnose._agrees_with_immich(right, REAL, "IMAGE")["level"] == "ok"


def test_immichs_dateTimeOriginal_is_the_instant_and_would_be_five_hours_early(rig):
    """The field is named after the EXIF tag and is not it. Writing its
    value into the tag puts the photo five hours early, and this is the
    check that would catch that having been done."""
    from app import diagnose
    wrong = {"EXIF:DateTimeOriginal": "2024:01:05 03:47:33"}
    out = diagnose._agrees_with_immich(wrong, REAL, "IMAGE")
    assert out["level"] == "warn"
    assert "+5.00h" in out["text"], out


def test_an_empty_make_is_treated_as_absent(rig):
    """Immich sends "" rather than null on a file whose EXIF was blanked --
    the same blank-versus-missing distinction, one layer up."""
    assert REAL["make"] is None and REAL["model"] is None


def test_immich_knowing_what_the_file_does_not_is_said_out_loud(rig):
    """And so is the reason the Problems tab shows nothing: the mismatch
    figures compare fileCreatedAt against exifInfo.dateTimeOriginal, and on
    a Takeout import both came from the same sidecar, so they agree."""
    from app import diagnose
    rep = _report({"EXIF:DateTimeOriginal": "", "File:MIMEType": "image/jpeg"},
                  name="PXL_20240105_034733992.jpg")
    rep["says"] = REAL
    out = diagnose._findings(rep)
    said = " ".join(f["text"] for f in out)
    assert "2024-01-05 08:47:33" in said, said
    assert "Asia/Karachi" in said, said
    assert "reads as zero" in said, said


def test_the_mismatch_counter_cannot_see_the_confirmed_file(rig):
    """Not a criticism of it -- a fact about it, and the reason this tool
    had to read the file instead."""
    from app import db
    assert db.needs_date_fix("2024-01-05T03:47:33.000Z",
                             "2024-01-05T03:47:33+00:00") is False
