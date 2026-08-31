"""Delivered files must carry the date the photo was taken.

Google Photos dates a file by its embedded capture date and falls back to
the file's modification time when there is none -- true for a lot of video,
screenshots, and anything whose EXIF was stripped in transit. Left at the
download time, that media lands in Google Photos dated today.
"""

import os
import time
from datetime import datetime, timezone

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


def mtime_of(rig, name):
    return os.path.getmtime(rig.outbox / name)


def as_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


async def test_a_delivered_file_carries_its_capture_date(rig, monkeypatch):
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00.000Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1

    name = db.queue_contents()[0]["outbox_name"]
    when = as_utc(mtime_of(rig, name))
    assert (when.year, when.month, when.day) == (2019, 6, 4)
    assert (when.hour, when.minute) == (14, 30)


async def test_it_is_not_the_download_time(rig, monkeypatch):
    """The bug: every file arrived stamped now, so undated media sorted to
    today in Google Photos."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100, taken="2015-01-02T03:04:05.000Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    name = db.queue_contents()[0]["outbox_name"]
    assert time.time() - mtime_of(rig, name) > 86400 * 365, \
        "the file was stamped with the download time"


@pytest.mark.parametrize("taken,expect", [
    ("2019-06-04T14:30:00.000Z", (2019, 6, 4)),
    ("2019-06-04T14:30:00+00:00", (2019, 6, 4)),
    ("2019-06-04", (2019, 6, 4)),
    ("2019-06-04T14:30:00", (2019, 6, 4)),
])
async def test_the_date_formats_immich_actually_sends(rig, taken, expect):
    from app import feeder
    when = as_utc(feeder.capture_time(taken))
    assert (when.year, when.month, when.day) == expect


@pytest.mark.parametrize("bad", [None, "", "not a date", "0000", "2019-13-45"])
async def test_an_unreadable_date_is_left_alone(rig, bad):
    """Better an honest download time than a wrong one."""
    from app import feeder
    assert feeder.capture_time(bad) is None


async def test_an_unreadable_date_does_not_break_delivery(rig, monkeypatch):
    from app import db, feeder, immich

    rows = [asset(0, size=100)]
    rows[0]["taken_at"] = "nonsense"
    db.upsert_assets(rows)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1, "a bad date stopped the file going out"


async def test_the_file_contents_are_untouched(rig, monkeypatch):
    """Only the timestamp is set; the bytes still pass through unchanged."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=2048, taken="2019-06-04T14:30:00.000Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    name = db.queue_contents()[0]["outbox_name"]
    assert (rig.outbox / name).read_bytes() == b"x" * 2048


async def test_files_delivered_earlier_are_repaired(rig, monkeypatch):
    """Anything already in the outbox still carries the download time, and
    Google Photos has not taken it yet -- so it is still fixable."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100, taken="2018-03-09T11:00:00.000Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    name = db.queue_contents()[0]["outbox_name"]
    os.utime(rig.outbox / name, (time.time(), time.time()))   # as the old code left it
    assert time.time() - mtime_of(rig, name) < 10

    assert feeder.repair_capture_times() == 1
    when = as_utc(mtime_of(rig, name))
    assert (when.year, when.month, when.day) == (2018, 3, 9)

    # Second pass has nothing to do.
    assert feeder.repair_capture_times() == 0


async def test_repair_survives_a_file_that_has_gone(rig, monkeypatch):
    """The phone may clear one between the query and the stat."""
    from app import db, feeder, immich

    db.upsert_assets([asset(i, size=100, taken="2018-03-09T11:00:00.000Z")
                      for i in range(2)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    names = [i["outbox_name"] for i in db.queue_contents()]
    for n in names:
        os.utime(rig.outbox / n, (time.time(), time.time()))
    (rig.outbox / names[0]).unlink()

    assert feeder.repair_capture_times() == 1


async def test_housekeeping_repairs_without_failing_the_cycle(rig, monkeypatch):
    from app import feeder, settings

    settings.save({"backup_enabled": False})

    def boom(limit=500):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(feeder, "repair_capture_times", boom)
    await feeder.housekeeping()          # must not raise
