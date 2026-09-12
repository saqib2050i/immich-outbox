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


# ---- what the first real trace turned up --------------------------------

def test_the_verdict_is_tagged_so_the_page_need_not_repeat_it(rig):
    """It appeared twice on screen: once in the running list of findings and
    again as its own card, the same paragraph verbatim."""
    from app import diagnose
    out = diagnose._findings(_report({"EXIF:DateTimeOriginal": ""},
                                     {"EXIF:DateTimeOriginal": ""}))
    v = [f for f in out if f.get("kind") == "verdict"]
    assert len(v) == 2, out
    assert all(f.get("kind") != "verdict" for f in out
               if "byte-for-byte" in f["text"])


def test_a_downloaded_copy_has_no_meaningful_modification_time(rig):
    """Immich's copy is fetched to a temp file, so its mtime is when the
    download happened. Reporting that as the file's own invents a
    difference between two copies that are byte for byte identical."""
    from app import diagnose
    exif = {"EXIF:DateTimeOriginal": "2024:01:01 11:20:38",
            "File:FileModifyDate": "2026:09:12 13:13:32+00:00"}
    rows = {r["tag"]: r for r in diagnose._tag_table(exif, downloaded=True)}
    assert rows["FileModifyDate"]["state"] == "n/a"
    assert "downloaded" in rows["FileModifyDate"]["text"]
    # And read in place it is a real fact about a real file.
    rows = {r["tag"]: r for r in diagnose._tag_table(exif)}
    assert rows["FileModifyDate"]["state"] == "value"


def test_the_download_time_is_not_counted_as_a_difference(rig):
    """It was listed beside the outbox copy's real mtime and marked as
    differing, directly under the finding saying the two are identical."""
    from app import diagnose
    assert "FileModifyDate" not in diagnose._dates(
        {"File:FileModifyDate": "2026:09:12 13:13:32+00:00"}, downloaded=True)
    assert "FileModifyDate" in diagnose._dates(
        {"File:FileModifyDate": "2024:01:01 06:20:38+00:00"})


def test_a_file_blanked_all_the_way_through(rig):
    """The real one: not just DateTimeOriginal but CreateDate, ModifyDate,
    both offsets, Make, Model and Software, every one present and empty. So
    there is no spare date to fall back on and no zone in the file at all --
    the zone has to come from Immich."""
    from app import diagnose
    exif = {k: "" for k in (
        "EXIF:DateTimeOriginal", "EXIF:CreateDate", "EXIF:ModifyDate",
        "EXIF:OffsetTimeOriginal", "EXIF:OffsetTime", "EXIF:Make",
        "EXIF:Model", "EXIF:Software")}
    exif["File:MIMEType"] = "image/jpeg"
    v = diagnose.verdict(exif, "IMAGE")
    assert v["dated"] is False and v["state"] == diagnose.BLANK
    assert v["others"] == [], "nothing in this file is a date"
    assert "carry a date elsewhere" not in v["reason"]


# ---- "Send it, then trace" ----------------------------------------------
#
# `forced` bypasses the date window and nothing else. claim_batch still
# excludes confirmed assets, motion components, video when video is off,
# anything over the size ceiling and anything a date mismatch holds back;
# top_up declines when paused, when the outbox is not mounted, and when the
# cap is reached. Every one of those made the button do nothing, and
# _send_now returned ok:True regardless -- which the page then did not
# render at all. Nine ways to press a button and be told nothing.

def _row(**over):
    from app import db
    db.upsert_assets([asset(1, name="IMG_0001.jpg")])
    c = db.connect()
    if over:
        sets = ", ".join(f"{k}=?" for k in over)
        c.execute(f"UPDATE assets SET {sets} WHERE id='asset-1'",
                  tuple(over.values()))
        c.commit()
    return dict(c.execute("SELECT * FROM assets WHERE id='asset-1'").fetchone())


async def test_a_confirmed_asset_can_be_sent_again_from_here(rig, monkeypatch):
    """Invariant 4 exists because re-sending duplicates a photo. That is
    true of a file whose bytes changed and false of one whose have not:
    Google Photos matches an upload against what it holds, so an identical
    file is recognised rather than added, and Free up space clears it again.

    Which makes this the only way to see what actually leaves the building
    for a file whose outbox copy was cleared months ago -- and those are the
    ones worth asking about, since a wrong date is noticed in Google Photos
    long after the fact.
    """
    from conftest import fake_download
    from app import db, diagnose, immich
    monkeypatch.setattr(immich, "stream_original", fake_download())
    out = await diagnose._send_now(_row(state="confirmed",
                                        outbox_name="IMG_0001.jpg"))
    assert out["ok"] is True and out["moved"] is True
    assert "second time" in out["text"]
    assert "recognises rather than adds" in out["text"]
    after = dict(db.connect().execute(
        "SELECT * FROM assets WHERE id='asset-1'").fetchone())
    assert after["state"] == "queued"


async def test_a_confirmed_asset_that_would_be_altered_is_refused(rig):
    """The one case where it really would duplicate: a changed file is a new
    photo to Google Photos, so dedupe cannot save it."""
    from app import diagnose, settings
    settings.save({"fix_dates": True})
    out = await diagnose._send_now(_row(
        state="confirmed", taken_at="2024-01-05T03:47:33Z",
        exif_taken_at="2019-01-01T00:00:00Z"))
    assert out["ok"] is False and out["moved"] is False
    assert "really would arrive as a duplicate" in out["text"]


async def test_nothing_automatic_re_sends_a_confirmed_asset(rig):
    """The narrower guard must not have widened the automatic path. This is
    invariant 4 where it actually lives."""
    from app import db
    db.upsert_assets([asset(1)])
    c = db.connect()
    c.execute("UPDATE assets SET state='confirmed', forced=1 WHERE id='asset-1'")
    c.commit()
    from app import settings
    rows = db.claim_batch(10 ** 9, 10, settings.load().eligibility)
    assert [r["id"] for r in rows] == []


async def test_a_motion_component_is_refused_and_says_why(rig):
    """The still carries the clip. Relaying the component alone puts a
    stray video in the library."""
    from app import db, diagnose
    row = _row()
    db.connect().execute("INSERT OR IGNORE INTO motion_parts (id) VALUES (?)",
                         (row["id"],))
    db.connect().commit()
    out = await diagnose._send_now(row)
    assert out["ok"] is False
    assert "motion photo" in out["text"]


async def test_video_switched_off_is_a_reason_not_a_silence(rig):
    from app import db, diagnose, settings
    settings.save({"include_video": False})
    db.upsert_assets([asset(2, kind="VIDEO", name="VID_0002.mp4")])
    row = dict(db.connect().execute(
        "SELECT * FROM assets WHERE id='asset-2'").fetchone())
    out = await diagnose._send_now(row)
    assert out["ok"] is False
    assert "video is switched off" in out["text"]


async def test_over_the_size_ceiling_is_a_reason(rig):
    from app import diagnose, settings
    settings.save({"max_asset_mb": 1})
    out = await diagnose._send_now(_row(size=50 * 1024 * 1024))
    assert out["ok"] is False
    assert "ceiling" in out["text"]


async def test_a_held_back_date_mismatch_is_a_reason(rig):
    """Sending it would hand Google Photos the stale date, which it keeps."""
    from app import diagnose, settings
    settings.save({"fix_dates": False})
    out = await diagnose._send_now(_row(date_mismatch=1))
    assert out["ok"] is False
    assert "corrected in Immich" in out["text"]


async def test_paused_is_a_reason(rig):
    from app import diagnose, settings
    settings.save({"paused": True})
    out = await diagnose._send_now(_row())
    assert out["ok"] is False and "paused" in out["text"]


async def test_an_outbox_that_is_not_there_is_a_reason(rig):
    """The condition invariant 1 rests on, and it used to be silent here.

    The guard only fires when the ledger says files should be in the outbox
    and none are -- an empty unmarked directory with nothing in flight is a
    fresh start, and outbox_ready() claims it."""
    from app import config, db, diagnose
    db.upsert_assets([asset(9, name="IMG_0009.jpg")])
    c = db.connect()
    c.execute("UPDATE assets SET state='queued', outbox_name='IMG_0009.jpg' "
              "WHERE id='asset-9'")
    c.commit()
    row = _row()

    gone = rig.root / "not-mounted"
    gone.mkdir()
    config.OUTBOX_DIR = str(gone)
    out = await diagnose._send_now(row)
    assert out["ok"] is False, out
    assert "outbox is not there" in out["text"], out


async def test_an_asset_immich_cannot_serve_is_a_reason(rig):
    from app import diagnose
    out = await diagnose._send_now(_row(missing_at="2026-01-01T00:00:00Z"))
    assert out["ok"] is False
    assert "no longer serves" in out["text"]


async def test_already_in_the_outbox_says_so_rather_than_nothing(rig):
    """The commonest case by far: the file traced a moment ago is still
    there, and the button did nothing without a word."""
    import os
    from app import config, diagnose
    row = _row(outbox_name="IMG_0001.jpg")
    open(os.path.join(config.OUTBOX_DIR, "IMG_0001.jpg"), "wb").write(b"x")
    out = await diagnose._send_now(row)
    assert out["ok"] is True and out["moved"] is False
    assert "Already in the outbox" in out["text"]


async def test_a_confirmed_file_is_not_called_still_in_the_outbox(rig):
    """A confirmed asset keeps its outbox_name for good: the file left the
    outbox because Google Photos cleared it off the phone, which is *how*
    it was confirmed. Reading the ledger here announced "already in the
    outbox" about a file that demonstrably was not -- and on exactly the
    kind of file somebody traces, one they found in Google Photos wearing
    the wrong date.

    The send itself then fails for want of a download stub, which is not
    what this is testing. What it tests is that the precondition asks the
    filesystem rather than the ledger."""
    import os
    from app import config, diagnose
    row = _row(state="confirmed", outbox_name="IMG_0001.jpg")
    assert not os.path.exists(os.path.join(config.OUTBOX_DIR, "IMG_0001.jpg"))

    out = await diagnose._send_now(row)
    assert "Already in the outbox" not in out["text"], out


async def test_a_queued_file_whose_copy_went_missing_can_be_sent_again(rig,
                                                                       monkeypatch):
    """Not confirmed, and the file is not there. That is not a reason to
    refuse -- it is the reason to send."""
    from conftest import fake_download
    from app import diagnose, immich
    monkeypatch.setattr(immich, "stream_original", fake_download())
    out = await diagnose._send_now(_row(outbox_name="IMG_0001.jpg"))
    assert out["moved"] is True, out


async def test_a_send_that_works_says_where_it_went(rig, monkeypatch):
    from conftest import fake_download
    from app import diagnose, immich
    monkeypatch.setattr(immich, "stream_original", fake_download())
    out = await diagnose._send_now(_row())
    assert out["ok"] is True and out["moved"] is True
    assert "IMG_0001.jpg" in out["text"]


async def test_a_full_outbox_is_reported_rather_than_shrugged_at(rig,
                                                                 monkeypatch):
    """top_up declines without raising, so this used to come back ok:True
    over an outbox that had refused the file."""
    from conftest import fake_download
    from app import diagnose, immich, settings
    monkeypatch.setattr(immich, "stream_original", fake_download())
    settings.save({"outbox_max_gb": 0})
    out = await diagnose._send_now(_row(size=5_000_000))
    assert out["ok"] is False and out["moved"] is False
    assert "outbox is full" in out["text"]


async def test_the_send_result_reaches_the_report(rig):
    """It was returned and then dropped on the floor: the page never read
    it, so a refusal looked identical to a file that was never asked for."""
    from app import diagnose
    rep = _report({"EXIF:DateTimeOriginal": "2023:01:01 07:57:59"})
    rep["sent"] = {"ok": False, "moved": False, "text": "Not sent, because X."}
    assert any("Not sent, because X." in f["text"]
               for f in diagnose._findings(rep))


async def test_a_failed_download_is_not_reported_as_sent(rig, monkeypatch):
    """`outbox_name` is recorded when the transfer is set up and survives
    the download failing, so the name is not evidence the file arrived.
    Reporting "Sent" over an empty outbox was the tool telling the exact
    kind of lie it exists to catch.

    Nothing is at risk from the row itself: confirmation needs
    state='queued' AND seen_on_phone=1, and a failed asset is neither.
    """
    import os
    from app import config, db, diagnose, immich

    async def explode(asset_id):
        raise RuntimeError("Name or service not known")
    monkeypatch.setattr(immich, "stream_original", explode)

    out = await diagnose._send_now(_row())
    after = dict(db.connect().execute(
        "SELECT * FROM assets WHERE id='asset-1'").fetchone())

    assert after["outbox_name"], "the name is recorded even so"
    assert not os.path.exists(
        os.path.join(config.OUTBOX_DIR, after["outbox_name"]))
    assert out["ok"] is False and out["moved"] is False
    assert "failed" in out["text"]
    assert after["state"] != "confirmed"


async def test_the_send_is_the_first_thing_the_report_says(rig):
    """It is what the reader just pressed a button to make happen, and a
    refusal explains everything underneath it."""
    from app import diagnose
    rep = _report({"EXIF:DateTimeOriginal": "2023:01:01 07:57:59"})
    rep["immich"] = {"ok": False, "error": "connection refused"}
    rep["sent"] = {"ok": False, "moved": False, "text": "Not sent, because X."}
    assert diagnose._findings(rep)[0]["text"] == "Not sent, because X."


# ---- the modification time is a real carrier ----------------------------
#
# Snapchat-618209934.jpg has no date tag of any kind -- not DateTimeOriginal,
# not CreateDate, nothing but Software: Picasa -- and Google Photos dated it
# Jan 1 2024, 6:30 AM, the same second as the outbox copy's mtime. The
# verdict called that file "would fall back to upload time", which was this
# tool being wrong out loud about a file that was fine.
#
# feeder.stamp_capture_time() sets every delivered file's mtime to Immich's
# capture instant, and Syncthing preserves it to the phone. It was built for
# exactly this and predates all of the above.

import datetime as _dt

SNAP_TAKEN = "2024-01-01T06:30:52.000Z"
SNAP_MTIME = _dt.datetime(2024, 1, 1, 6, 30, 52,
                          tzinfo=_dt.timezone.utc).timestamp()


def test_a_stamped_modification_time_dates_the_file(rig):
    from app import diagnose
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=SNAP_MTIME, taken_at=SNAP_TAKEN)
    assert v["dated"] is True, v
    assert v["level"] == "warn"
    assert "modification time" in v["headline"]
    assert "2024-01-01 06:30:52" in v["reason"]


def test_the_weakness_of_that_carrier_is_stated(rig):
    """It works, and it is not as good as the tag. Both are true and the
    report says both."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=SNAP_MTIME, taken_at=SNAP_TAKEN)
    assert "does not survive" in v["reason"]
    assert "falls back to it when there is no tag" in v["reason"]


def test_a_downloaded_copy_is_never_credited_with_its_mtime(rig):
    """Immich's copy is fetched to a temp file moments earlier, so its mtime
    is today. trace() passes one only for a copy read in place."""
    from app import diagnose
    import time
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=time.time(), taken_at=SNAP_TAKEN)
    assert v["dated"] is False, v
    assert "upload time" in v["headline"]


def test_no_mtime_means_the_old_answer_still_stands(rig):
    from app import diagnose
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE")
    assert v["dated"] is False
    assert v["headline"] == "Would fall back to upload time"


def test_the_tag_still_beats_the_modification_time(rig):
    """A real DateTimeOriginal is the strong answer and stays the headline."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": "2024:01:01 06:30:52"},
                         "IMAGE", mtime=SNAP_MTIME, taken_at=SNAP_TAKEN)
    assert v["level"] == "ok"
    assert v["headline"] == "Would be dated correctly by Google"


# ---- blank poisons the fallback; absent does not ------------------------
#
# Measured, not reasoned. Two files from this library, the same route
# (Immich -> outbox -> Syncthing -> Pixel -> Google Photos), both stamped
# with a correct modification time, landing nine hundred days apart:
#
#   Snapchat-618209934.jpg   tag absent          -> Jan 1 2024, 6:30 AM
#   PXL_20240101_062038690   tag present, empty  -> today, 12:33 PM
#
# One variable. A blank tag evidently reads to the media scanner as
# metadata it cannot parse, and it never reaches the modification time.

KARACHI_TAKEN = "2024-01-01T06:20:38.000Z"
KARACHI_MTIME = _dt.datetime(2024, 1, 1, 6, 20, 38,
                             tzinfo=_dt.timezone.utc).timestamp()


def test_a_blank_tag_is_not_rescued_by_a_good_modification_time(rig):
    """The whole finding in one assertion. This is the file that proved it,
    and crediting it with its mtime would call a broken file good."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": "", "EXIF:CreateDate": ""},
                         "IMAGE", mtime=KARACHI_MTIME, taken_at=KARACHI_TAKEN)
    assert v["dated"] is False, v
    assert v["headline"] == "Would fall back to upload time"


def test_and_the_report_says_why_the_good_mtime_does_not_help(rig):
    """The table shows a correct FileModifyDate two lines below the verdict.
    Without this the reader draws exactly the wrong conclusion from it."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:DateTimeOriginal": ""}, "IMAGE",
                         mtime=KARACHI_MTIME, taken_at=KARACHI_TAKEN)
    assert "does not save it" in v["reason"]
    assert "no date tag at all falls through" in v["reason"]


def test_an_absent_tag_with_the_same_mtime_is_rescued(rig):
    """Same modification time, same everything, one difference."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=KARACHI_MTIME, taken_at=KARACHI_TAKEN)
    assert v["dated"] is True, v
    assert "modification time" in v["headline"]


# ---- is the zone known, or merely reported? ------------------------------
#
# Immich derives a zone from GPS when the file carries no offset tag, and
# reports UTC when it has neither. Those are the same string and opposite
# facts, five hours apart for most of this library:
#
#   PXL_20240101_062038690   Model Town, Punjab, Pakistan  -> Asia/Karachi
#   Snapchat-618209934       no coordinates at all         -> "UTC+0"

GPS_SAYS = {"ok": True, "time_zone": "Asia/Karachi",
            "latitude": 31.459011, "longitude": 74.37055,
            "place": "Model Town, Punjab, Pakistan",
            "local_date_time": "2024-01-01T11:20:38.000Z"}
BLIND_SAYS = {"ok": True, "time_zone": "UTC+0", "latitude": None,
              "longitude": None, "place": None,
              "local_date_time": "2024-01-01T06:30:52.000Z"}


def test_a_zone_derived_from_coordinates_is_a_finding(rig):
    from app import diagnose
    assert diagnose.zone_source({}, GPS_SAYS) == ("gps", "Asia/Karachi")


def test_utc_on_a_file_with_no_coordinates_is_a_default(rig):
    """Immich saying it does not know, not saying the photo was taken at
    Greenwich. Reading the second as the first is how a Karachi photo comes
    to look correct at five hours early."""
    from app import diagnose
    assert diagnose.zone_source({}, BLIND_SAYS)[0] == "none"


def test_the_file_s_own_offset_outranks_everything(rig):
    """Any pic that carries zone information is honoured regardless of when
    it was taken."""
    from app import diagnose
    assert diagnose.zone_source(
        {"EXIF:OffsetTimeOriginal": "+05:00"}, BLIND_SAYS) == ("file", "+05:00")


def test_a_blank_offset_is_not_zone_information(rig):
    """It is present and empty, like everything else in that file."""
    from app import diagnose
    assert diagnose.zone_source(
        {"EXIF:OffsetTimeOriginal": ""}, BLIND_SAYS)[0] == "none"


def test_a_file_with_no_zone_anywhere_is_said_to_have_none(rig):
    from app import diagnose
    rep = _report({"EXIF:Software": "Picasa"}, name="Snapchat-618209934.jpg")
    rep["says"] = BLIND_SAYS
    said = " ".join(f["text"] for f in diagnose._findings(rep))
    assert "No time zone anywhere" in said, said
    assert "not evidence either is right" in said, said


def test_a_gps_zone_is_reported_as_settled(rig):
    from app import diagnose
    rep = _report({"EXIF:DateTimeOriginal": ""},
                  name="PXL_20240101_062038690.jpg")
    rep["says"] = GPS_SAYS
    said = " ".join(f["text"] for f in diagnose._findings(rep))
    assert "derived from the coordinates" in said, said
    assert "Model Town" in said, said


# ---- the owner's rule for a photo with no zone --------------------------
#
# Not readable from any file: it is where they were living, and this
# library spans a move on 4 March 2026. Configured, so it is a fact about a
# person rather than a constant in a diagnostic.

def _rule(rig):
    from app import settings
    settings.save({"assume_zone_before": "2026-03-04",
                   "assume_zone_offset": "+05:00"})


def test_a_photo_older_than_the_move_is_read_at_the_offset(rig):
    from app import diagnose
    _rule(rig)
    kind, zone = diagnose.zone_source({}, BLIND_SAYS, "2024-01-01T06:30:52Z")
    assert (kind, zone) == ("assumed", "+05:00")


def test_a_photo_after_the_move_is_not(rig):
    from app import diagnose
    _rule(rig)
    assert diagnose.zone_source(
        {}, BLIND_SAYS, "2026-06-01T12:00:00Z")[0] == "none"


def test_the_file_s_own_zone_still_wins_over_the_rule(rig):
    """Any pic that contains timezone info, regardless of time period, is
    honoured."""
    from app import diagnose
    _rule(rig)
    assert diagnose.zone_source({"EXIF:OffsetTimeOriginal": "+01:00"},
                                BLIND_SAYS, "2024-01-01T06:30:52Z") == (
        "file", "+01:00")


def test_coordinates_still_win_over_the_rule(rig):
    """A photo taken on a trip has GPS saying so, and that outranks a guess
    about where its owner lived."""
    from app import diagnose
    _rule(rig)
    assert diagnose.zone_source({}, GPS_SAYS, "2024-01-01T06:20:38Z")[0] == "gps"


def test_the_rule_is_off_until_both_halves_are_set(rig):
    from app import diagnose, settings
    settings.save({"assume_zone_before": "2026-03-04",
                   "assume_zone_offset": ""})
    assert diagnose.zone_source({}, BLIND_SAYS, "2024-01-01T06:30:52Z")[0] == "none"


# ---- and what that means for a file riding on its mtime -----------------

def test_a_named_zone_resolves_to_hours_at_the_capture_instant(rig):
    """Asia/Karachi, resolved then rather than now, so a DST boundary in
    between cannot move it."""
    from app import diagnose
    assert diagnose.zone_hours("gps", "Asia/Karachi",
                               "2024-01-01T06:20:38Z") == 5.0


def test_not_knowing_the_offset_is_not_knowing_it_to_be_zero(rig):
    """Conflating those is the whole of this section."""
    from app import diagnose
    assert diagnose.zone_hours("none", "UTC+0", "2024-01-01T06:20:38Z") is None


def test_an_mtime_file_taken_in_karachi_shows_five_hours_early(rig):
    """The correction this forces. Google Photos shows an mtime as UTC --
    Snapchat-618209934.jpg came back labelled GMT+00:00 -- so the instant is
    right and the clock on screen is the offset out."""
    from app import diagnose
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=KARACHI_MTIME, taken_at=KARACHI_TAKEN,
                         says=GPS_SAYS)
    assert v["level"] == "warn"
    assert "5h out" in v["headline"]
    assert "appears 5 hours early" in v["reason"]
    assert "day is right" in v["reason"]


def test_a_photo_taken_before_dawn_lands_on_the_wrong_day_too(rig):
    """Local 02:00 at +05:00 is 21:00 the previous day in UTC."""
    from app import diagnose
    says = dict(GPS_SAYS, local_date_time="2024-01-01T02:00:00.000Z")
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=_dt.datetime(2023, 12, 31, 21, 0,
                                            tzinfo=_dt.timezone.utc).timestamp(),
                         taken_at="2023-12-31T21:00:00.000Z", says=says)
    assert "wrong day" in v["reason"], v["reason"]


def test_a_genuine_utc_photo_on_its_mtime_is_simply_right(rig):
    from app import diagnose
    says = dict(BLIND_SAYS, time_zone="UTC+0", latitude=51.5, longitude=0.0)
    v = diagnose.verdict({"EXIF:Software": "Picasa"}, "IMAGE",
                         mtime=SNAP_MTIME, taken_at=SNAP_TAKEN, says=says)
    assert v["level"] == "ok"
    assert "lands right" in v["reason"]
