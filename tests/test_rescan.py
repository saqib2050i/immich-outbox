"""A rescan has to bring the ledger back in line with Immich.

Scans used to be INSERT OR IGNORE and nothing else, so anything corrected
in Immich after an asset was first seen never reached the ledger.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


def immich_page(rows):
    async def list_assets(taken_after=None):
        yield rows
    return list_assets


async def test_a_corrected_capture_date_moves_the_month(rig, monkeypatch):
    """Reported: an import carried a wrong date, it was fixed in Immich, and
    the timeline still showed 1.2 GB in December 2016 forever."""
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=1_200_000, taken="2016-12-11")])
    assert [m["month"] for m in db.timeline()] == ["2016-12"]

    corrected = asset(0, size=1_200_000, taken="2026-07-04")
    monkeypatch.setattr(immich, "list_assets", immich_page([corrected]))
    await worker.full_scan()

    months = [m["month"] for m in db.timeline()]
    assert "2016-12" not in months, "the old month survived a rescan"
    assert months == ["2026-07"]


async def test_a_rescan_does_not_change_what_has_been_sent(rig, monkeypatch):
    """Refreshing the description of an asset must not re-queue it."""
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=100, taken="2016-12-11")])
    db.mark_queued(["asset-0"])
    db.confirm_absent([])
    assert db.counts()["confirmed"] == 1

    monkeypatch.setattr(immich, "list_assets",
                        immich_page([asset(0, size=100, taken="2026-07-04")]))
    await worker.full_scan()

    row = db.connect().execute("SELECT * FROM assets WHERE id='asset-0'").fetchone()
    assert row["state"] == "confirmed"
    assert row["taken_at"] == "2026-07-04"


async def test_a_rescan_keeps_the_name_the_file_has_on_disk(rig, monkeypatch):
    """Renaming in Immich must not orphan a file already in the outbox."""
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=100)])
    name = db.reserve_outbox_name("asset-0", "IMG_0000.jpg")
    db.mark_queued(["asset-0"])

    renamed = asset(0, size=100)
    renamed["filename"] = "holiday.jpg"
    monkeypatch.setattr(immich, "list_assets", immich_page([renamed]))
    await worker.full_scan()

    row = db.connect().execute("SELECT * FROM assets WHERE id='asset-0'").fetchone()
    assert row["filename"] == "holiday.jpg"
    assert row["outbox_name"] == name, "the on-disk name changed under the file"


async def test_an_asset_deleted_from_immich_leaves_the_library_view(rig, monkeypatch):
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=100, taken="2026-01-02"),
                      asset(1, size=100, taken="2026-01-03")])
    assert db.timeline()[0]["total"] == 2

    monkeypatch.setattr(immich, "list_assets",
                        immich_page([asset(0, size=100, taken="2026-01-02")]))
    await worker.full_scan()

    assert db.missing_count() == 1
    assert db.timeline()[0]["total"] == 1


async def test_a_missing_asset_is_never_claimed(rig, monkeypatch):
    """It cannot be downloaded, so trying only burns retries."""
    from app import db, feeder, immich, worker

    db.upsert_assets([asset(0, size=100), asset(1, size=100)])
    monkeypatch.setattr(immich, "list_assets", immich_page([asset(0, size=100)]))
    await worker.full_scan()

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1
    assert db.queue_contents()[0]["id"] == "asset-0"


async def test_an_asset_that_comes_back_is_unflagged(rig, monkeypatch):
    """The flag is a fact about the last scan, not a tombstone."""
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=100), asset(1, size=100)])
    monkeypatch.setattr(immich, "list_assets", immich_page([asset(0, size=100)]))
    await worker.full_scan()
    assert db.missing_count() == 1

    monkeypatch.setattr(immich, "list_assets",
                        immich_page([asset(0, size=100), asset(1, size=100)]))
    await worker.full_scan()
    assert db.missing_count() == 0


async def test_a_failed_scan_marks_nothing_missing(rig, monkeypatch):
    """A half-finished pass looks exactly like a library that lost
    everything the pass had not reached yet."""
    from app import db, immich, worker

    db.upsert_assets([asset(i, size=100) for i in range(5)])

    async def dies(taken_after=None):
        yield [asset(0, size=100)]
        raise RuntimeError("connection reset mid-scan")

    monkeypatch.setattr(immich, "list_assets", dies)
    await worker.full_scan()
    assert db.missing_count() == 0, "a failed scan condemned the rest of the library"


async def test_an_empty_scan_marks_nothing_missing(rig, monkeypatch):
    """A key that suddenly returns nothing must not wipe the library view."""
    from app import db, immich, worker

    db.upsert_assets([asset(i, size=100) for i in range(5)])

    async def nothing(taken_after=None):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(immich, "list_assets", nothing)
    await worker.full_scan()
    assert db.missing_count() == 0


async def test_an_incremental_scan_refreshes_nothing_and_condemns_nothing(rig, monkeypatch):
    """It only looks at a recent window, so it can conclude nothing about
    the rest of the library."""
    from app import db, immich, worker

    db.upsert_assets([asset(0, size=100, taken="2016-12-11"),
                      asset(1, size=100, taken="2016-12-12")])
    monkeypatch.setattr(immich, "list_assets",
                        immich_page([asset(0, size=100, taken="2026-07-04")]))
    await worker._scan("2026-07-01T00:00:00.000Z", "incremental")

    assert db.missing_count() == 0
    row = db.connect().execute("SELECT taken_at FROM assets WHERE id='asset-0'").fetchone()
    assert row["taken_at"] == "2016-12-11", "an incremental scan rewrote history"
