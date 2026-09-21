"""What else Immich holds that the file does not carry.

The dates were never the only thing a Takeout sidecar carried into Immich
and left out of the file. It carries five: the dates, the location, the
description, whether it was a favourite, and who is in it. Two of those five
can be written into a file at all, and both are reported here and written by
nothing.

The other direction matters just as much, because it stops somebody hunting:
make, model, lens, ISO, aperture and focal length are read *out of the file*
by Immich, so a file missing one is a file Immich cannot supply it for
either. There is nothing there to restore.
"""

import pytest

from conftest import asset

pytestmark = pytest.mark.asyncio

SAYS = {"ok": True, "latitude": 31.5204, "longitude": 74.3587,
        "place": "Lahore, Punjab, Pakistan", "description": "",
        "local_date_time": "2023-06-14T08:39:21.000Z",
        "file_created_at": "2023-06-14T03:39:21.000Z"}


async def test_a_location_immich_has_and_the_file_does_not(rig):
    from app import diagnose
    found = diagnose.gaps({"EXIF:DateTimeOriginal": ""}, SAYS)
    assert [g["what"] for g in found] == ["location"]
    assert "Lahore" in found[0]["immich"]


async def test_a_file_that_carries_its_own_location_is_not_reported(rig):
    """It is only a gap where the file lacks it. Half this library's photos
    have coordinates of their own -- that is where the zone came from."""
    from app import diagnose
    for tag in ("EXIF:GPSLatitude", "Composite:GPSPosition",
                "QuickTime:GPSCoordinates"):
        assert diagnose.gaps({tag: "31.5204 N"}, SAYS) == []


async def test_a_description_is_the_other_half(rig):
    from app import diagnose
    says = dict(SAYS, latitude=None, longitude=None,
                description="Parishey's first day at school")
    found = diagnose.gaps({}, says)
    assert [g["what"] for g in found] == ["description"]
    assert "first day" in found[0]["immich"]

    carried = diagnose.gaps({"EXIF:ImageDescription": "already here"}, says)
    assert carried == []


async def test_nothing_is_claimed_when_immich_could_not_be_asked(rig):
    """A failed lookup is not an empty library."""
    from app import diagnose
    assert diagnose.gaps({}, {"ok": False, "error": "timed out"}) == []


async def test_the_camera_fields_are_not_offered_as_gaps(rig):
    """Immich reads make, model, lens, ISO and aperture out of the file. A
    file missing one is a file Immich cannot supply it for, and offering to
    restore it would send somebody looking for data nobody has."""
    from app import diagnose
    says = dict(SAYS, latitude=None, longitude=None, description=None,
                make="NIKON CORPORATION", model="NIKON D5300")
    assert diagnose.gaps({}, says) == []


async def test_it_is_recorded_while_the_bytes_are_here(rig, monkeypatch):
    """The one moment they are. Nothing is held back for it, and nothing is
    written."""
    from app import db, diagnose, feeder, immich, settings
    from conftest import fake_download
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0, taken="2023-06-14T03:39:21.000Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    monkeypatch.setattr(diagnose, "read_exif",
                        lambda p: {"EXIF:DateTimeOriginal": "2023:06:14 08:39:21"})

    async def detail(asset_id):
        return dict(SAYS)
    monkeypatch.setattr(immich, "asset_detail", detail)
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["hold_gaps"] == "location"
    assert row["state"] == "queued", "a missing location holds nothing back"
    assert db.gap_counts() == {"location": 1}


async def test_the_count_covers_files_that_were_never_held(rig):
    """A file with a perfectly good date can still have gone to Google
    Photos with no location, and that is the same fault."""
    from app import db
    db.upsert_assets([asset(1), asset(2)])
    db.record_check("asset-1", {"hold": False, "kind": "ok",
                                "gaps": ["location", "description"],
                                "checked_at": db.now(), "checked_sum": "x"})
    db.record_check("asset-2", {"hold": False, "kind": "ok",
                                "gaps": ["location"],
                                "checked_at": db.now(), "checked_sum": "y"})
    assert db.gap_counts() == {"location": 2, "description": 1}


async def test_the_trace_says_it_and_says_nothing_writes_it(rig):
    from app import diagnose
    out = diagnose._findings({
        "filename": "Snapchat-1.jpg",
        "asset": {"id": "a", "kind": "IMAGE",
                  "taken_at": "2023-06-14T03:39:21.000Z"},
        "says": SAYS,
        "immich": {"ok": True, "exif": {"EXIF:DateTimeOriginal": ""}},
        "outbox": {"present": False},
    })
    said = [f["text"] for f in out if "location" in f["text"]]
    assert said, out
    assert "Nothing here writes it." in said[0]
