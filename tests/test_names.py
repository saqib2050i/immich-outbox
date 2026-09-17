"""What the filename says about when a photo was taken.

An independent witness: it comes from the camera at the moment of the
shutter, and it is untouched by the import that filled Immich's date fields
from a Takeout sidecar. But only where you know which clock was being read
-- the Pixel writes UTC into the name and records the zone separately, and
almost everything else writes the local wall clock. Reading one as the other
turned an entirely correct library into a five-hour fault once already.

Measured against the library this was built for: 7,235 of 7,239 Pixel names
sit exactly on Immich's instant, and 7,310 of 7,447 Samsung-shaped names sit
exactly five hours from it. Those are the two conventions, in one library.
"""

import pytest

from conftest import asset

pytestmark = pytest.mark.asyncio


def reading(name, taken_at, video=False):
    from app import diagnose
    return diagnose.name_reading(name, taken_at, video)


# ---- which clock the name was written by ---------------------------------

async def test_a_local_name_gives_the_zone_it_was_taken_at(rig):
    """The gap between a local clock and the moment it belongs to is the
    offset, and it is evidence about this one file."""
    told = reading("IMG_20230101_075759.jpg", "2023-01-01T02:57:59.000Z")
    assert told["verdict"] == "zone"
    assert told["offset"] == 5.0


async def test_a_pixel_name_is_the_instant_and_gives_no_zone(rig):
    """It is already UTC, so it confirms the moment and says nothing about
    where the shutter was."""
    told = reading("PXL_20230101_025759225.jpg", "2023-01-01T02:57:59.000Z")
    assert told["verdict"] == "agrees"
    assert told["offset"] is None


async def test_a_convention_nobody_knows_is_never_guessed(rig):
    """Asserting one manufactures faults out of correct files."""
    for name in ("Snapchat-1521494117.jpg", "images (25).jpeg", "MOVIE.mp4"):
        assert reading(name, "2023-01-01T02:57:59.000Z")["verdict"] == "none"


# ---- the gap has to be an offset somewhere keeps --------------------------

async def test_a_gap_no_zone_explains_is_a_disagreement(rig):
    """The file this came from: a video named 1 January and dated the 8th by
    Immich. One of the two is about something else, and nothing here picks a
    winner."""
    told = reading("VID_20220101_160928.mp4", "2022-01-08T17:20:01.000Z",
                   video=True)
    assert told["verdict"] == "disagrees"
    assert told["offset"] is None
    assert told["gap"] < -160


async def test_a_save_delay_is_not_a_zone(rig):
    """Seven screenshots edited in one sitting: each name is when the
    screenshot was taken and Immich's date is when the edit was saved, so
    the gap is the offset minus a delay that differs per file. Reading those
    as zones put one afternoon in Lahore into three of them."""
    told = reading("Screenshot_20221110-191904~2.png",
                   "2022-11-10T14:53:23.000Z")
    assert told["verdict"] == "disagrees", told


async def test_an_offset_nowhere_keeps_is_not_offered(rig):
    """+04:45 is arithmetic, not a place."""
    from app import diagnose
    assert 4.75 not in diagnose.REAL_OFFSETS
    assert 5.75 in diagnose.REAL_OFFSETS, "Nepal is"
    assert 5.5 in diagnose.REAL_OFFSETS, "so is India"
    told = reading("Screenshot_20221110-193604~2.png",
                   "2022-11-10T14:53:23.000Z")
    assert told["verdict"] == "disagrees"


async def test_the_half_hour_zones_are_read(rig):
    """A trip to India is +05:30, and rounding it to +05:00 is half an hour
    of wrong in every photo of it."""
    told = reading("VID_20221223_131925.mp4", "2022-12-23T07:49:25.000Z",
                   video=True)
    assert told["verdict"] == "zone" and told["offset"] == 5.5


# ---- a name that belongs to another photo --------------------------------

async def test_a_creation_is_named_after_its_source(rig):
    """`20211010_155825-COLLAGE.jpg` was built in June 2023 out of a photo
    taken in October 2021, and Immich is right about both. Reading the name
    as this file's date moves a collage two years."""
    told = reading("20211010_155825-COLLAGE.jpg", "2023-06-15T17:28:01.000Z")
    assert told["verdict"] == "made"


async def test_a_creation_is_recognised_by_the_library_holding_its_source(rig):
    """The general form, for the creation types nobody here has seen: if
    the library holds the file this one is named after, the time in the name
    is a fact about that file."""
    from app import db
    told = reading("20211010_155825-NEW_EFFECT_NOBODY_KNOWS.jpg",
                   "2023-06-15T17:28:01.000Z")
    assert told["verdict"] != "made", "nothing to go on yet"

    db.upsert_assets([asset(1, name="20211010_155825.jpg",
                            taken="2021-10-10T10:58:25.000Z")])
    told = reading("20211010_155825-NEW_EFFECT_NOBODY_KNOWS.jpg",
                   "2023-06-15T17:28:01.000Z")
    assert told["verdict"] == "made"


# ---- where it ranks ------------------------------------------------------

async def test_the_name_outranks_the_rule(rig):
    """The rule is a sentence about a whole library; this is evidence about
    one file. It is what puts a journey in the right zone."""
    from app import diagnose, settings
    settings.save({"assume_zone_before": "2025-03-04",
                   "assume_zone_offset": "+05:00"})
    kind, zone = diagnose.zone_source(
        {}, {"ok": True}, "2025-03-04T22:27:43.000Z",
        "IMG_20250305_012743.jpg")
    assert kind == "name" and zone == "+03:00", \
        "the journey out, not the country left behind"


async def test_the_name_does_not_outrank_the_shutter(rig):
    """Coordinates and the file's own offset are recorded where the photo
    was taken. The name is two numbers that might both be wrong."""
    from app import diagnose
    says = {"ok": True, "time_zone": "Asia/Karachi",
            "latitude": 31.5, "longitude": 74.3}
    kind, _ = diagnose.zone_source({}, says, "2023-01-01T02:57:59.000Z",
                                   "IMG_20230101_075759.jpg")
    assert kind == "gps"

    kind, _ = diagnose.zone_source({"EXIF:OffsetTimeOriginal": "+05:00"},
                                   {"ok": True}, "2023-01-01T02:57:59.000Z",
                                   "IMG_20230101_075759.jpg")
    assert kind == "file"


async def test_the_name_is_the_wall_clock_where_it_decided_the_zone(rig):
    """Immich's own conversion is not consulted: it converted through a zone
    this has just outranked."""
    from app import diagnose, settings
    settings.save({"assume_zone_before": "2025-03-04",
                   "assume_zone_offset": "+05:00"})
    says = {"ok": True, "time_zone": "UTC+1",
            "local_date_time": "2025-03-04T23:27:43.000Z",
            "file_created_at": "2025-03-04T22:27:43.000Z"}
    local, off, kind = diagnose.wall_clock(says, {}, "2025-03-04T22:27:43.000Z",
                                           "IMG_20250305_012743.jpg")
    assert kind == "name" and off == 3.0
    assert local.strftime("%Y:%m:%d %H:%M:%S") == "2025:03:05 01:27:43"


async def test_a_held_row_carries_what_its_name_says(rig):
    """So the tab can group by it, and say why a creation's name is ignored
    without anybody having to know what a COLLAGE is."""
    from app import diagnose
    out = diagnose.rejudge({"id": "a", "kind": "VIDEO",
                            "filename": "VID_20220101_160928.mp4",
                            "taken_at": "2022-01-08T17:20:01.000Z",
                            "hold_kind": "blank", "hold_zone": "assumed",
                            "writes": [], "says": None})
    assert out["name_says"]["verdict"] == "disagrees"
    assert out["name_says"]["when"] == "2022-01-01 16:09:28"
