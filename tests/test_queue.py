"""The live queue: what is in the outbox, and the controls over it."""

import asyncio

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def fill(monkeypatch, n=3, size=100, batch=40):
    from app import db, feeder, immich, settings
    settings.save({"max_batch_files": batch})
    db.upsert_assets([asset(i, size=size) for i in range(n)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    return await feeder.top_up(used)


async def test_each_file_is_marked_as_it_lands(rig, monkeypatch):
    """The complaint this fixes: a batch of 40 showed as nothing for
    minutes and then jumped to 40, because the ledger was only written
    once the whole loop finished."""
    from app import db, feeder, immich, settings

    settings.save({"max_batch_files": 40})
    db.upsert_assets([asset(i, size=100) for i in range(5)])

    seen = []
    real = fake_download()

    async def watched(asset_id):
        # Sampled before each download: how many rows the ledger already
        # calls queued, and what the revision was.
        seen.append((db.counts()["queued"], db.revision()))
        return await real(asset_id)

    monkeypatch.setattr(immich, "stream_original", watched)
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 5

    queued_counts = [c for c, _ in seen]
    assert queued_counts == [0, 1, 2, 3, 4], \
        f"the queue only grew at the end of the batch: {queued_counts}"

    revisions = [r for _, r in seen]
    assert revisions == sorted(revisions) and len(set(revisions)) == 5, \
        "the event stream had nothing to push between files"


async def test_progress_names_the_file_being_fetched(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"max_batch_files": 40})
    db.upsert_assets([asset(i, size=100) for i in range(3)])

    snapshots = []
    real = fake_download()

    async def watched(asset_id):
        snapshots.append(db.progress())
        return await real(asset_id)

    monkeypatch.setattr(immich, "stream_original", watched)
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    assert [s["filename"] for s in snapshots] == \
        ["IMG_0000.jpg", "IMG_0001.jpg", "IMG_0002.jpg"]
    assert [s["done"] for s in snapshots] == [0, 1, 2]
    assert all(s["total"] == 3 for s in snapshots)
    # Cleared when the batch ends, so the UI does not show a stale file.
    assert db.progress() is None


async def test_the_queue_lists_what_is_in_the_outbox(rig, monkeypatch):
    from app import db, main

    await fill(monkeypatch, n=3)
    q = await main.queue()

    assert q["count"] == 3
    assert q["bytes"] == 300
    assert {i["filename"] for i in q["items"]} == \
        {"IMG_0000.jpg", "IMG_0001.jpg", "IMG_0002.jpg"}
    assert all(i["on_disk"] for i in q["items"])
    assert q["paused"] is False


async def test_the_queue_admits_when_a_file_has_already_gone(rig, monkeypatch):
    """Between the phone clearing a file and the next reconcile, the row
    still says queued. Showing it as present would be a lie."""
    from app import main

    await fill(monkeypatch, n=2)
    rig.deliver(1)

    q = await main.queue()
    on_disk = {i["filename"]: i["on_disk"] for i in q["items"]}
    assert sorted(on_disk.values()) == [False, True]


async def test_cancel_removes_the_file_and_does_not_confirm_it(rig, monkeypatch):
    """The trap: cancelling deletes from the outbox, and absence is how a
    backup is recorded. The row has to go back to pending in the same
    breath or the asset is marked backed up when it is not."""
    from app import db, feeder, main

    await fill(monkeypatch, n=3)
    victim = db.queue_contents()[0]

    result = await main.queue_cancel({"ids": [victim["id"]]})
    assert result["cancelled"] == 1 and result["removed"] == 1

    assert victim["outbox_name"] not in rig.files()
    row = db.connect().execute(
        "SELECT * FROM assets WHERE id=?", (victim["id"],)).fetchone()
    assert row["state"] == "pending", "a cancelled file was recorded as backed up"
    assert row["seen_on_phone"] == 0
    assert row["sent_at"] is None

    # And a later reconcile does not retroactively confirm it either.
    feeder.reconcile()
    assert db.counts()["confirmed"] == 0
    assert db.counts()["pending"] == 1


async def test_a_cancelled_file_can_be_sent_again(rig, monkeypatch):
    from app import db, feeder, immich, main

    await fill(monkeypatch, n=1)
    victim = db.queue_contents()[0]
    await main.queue_cancel({"ids": [victim["id"]]})
    assert rig.files() == set()

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1
    assert len(rig.files()) == 1


async def test_cancel_leaves_everything_else_alone(rig, monkeypatch):
    from app import db, main

    await fill(monkeypatch, n=4)
    victim = db.queue_contents()[0]["id"]
    await main.queue_cancel({"ids": [victim]})

    assert db.counts()["queued"] == 3
    assert len(rig.files()) == 3


async def test_cancel_rejects_a_malformed_request(rig):
    from fastapi import HTTPException
    from app import main

    with pytest.raises(HTTPException) as exc:
        await main.queue_cancel({"ids": "not-a-list"})
    assert exc.value.status_code == 400
    assert (await main.queue_cancel({"ids": []}))["cancelled"] == 0


async def test_pause_stops_new_files_without_touching_the_outbox(rig, monkeypatch):
    """Pausing must not empty the outbox: the service cannot recall a file,
    and deleting one would be read as a backup."""
    from app import db, feeder, immich, main, settings

    await fill(monkeypatch, n=2, batch=2)
    before = rig.files()

    r = await main.pause({"paused": True})
    assert r["paused"] is True
    assert settings.load().paused is True
    assert rig.files() == before, "pausing removed files from the outbox"

    db.upsert_assets([asset(9, size=100)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert rig.files() == before


async def test_resume_tops_up_immediately(rig, monkeypatch):
    from app import db, immich, main, settings

    settings.save({"paused": True, "max_batch_files": 40})
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    r = await main.pause({"paused": False})
    assert r["paused"] is False
    assert r["added"] == 3, "resume waited for the next cycle"
    assert len(rig.files()) == 3


async def test_pause_toggles_when_not_told_which_way(rig, monkeypatch):
    from app import main, settings

    settings.save({"paused": False})
    assert (await main.pause(None))["paused"] is True
    assert (await main.pause(None))["paused"] is False
    assert settings.load().paused is False


async def test_the_watcher_confirms_without_a_feed_cycle(rig, monkeypatch):
    """Departures used to be noticed only on the feed cycle, so a phone that
    cleared files just after one ran left stale numbers for ten minutes."""
    from app import db, feeder

    await fill(monkeypatch, n=3)
    assert db.counts()["queued"] == 3

    rig.deliver(3)
    monkeypatch.setattr(feeder, "WATCH_INTERVAL_SECONDS", 0.01)
    task = asyncio.create_task(feeder.watch())
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if db.counts()["confirmed"] == 3:
                break
    finally:
        task.cancel()

    assert db.counts()["confirmed"] == 3
    assert db.counts()["queued"] == 0


async def test_the_watcher_yields_to_a_running_feed(rig, monkeypatch):
    """It must never block behind a download: that feed is already
    publishing per-file updates."""
    from app import db, feeder

    await fill(monkeypatch, n=1)
    monkeypatch.setattr(feeder, "WATCH_INTERVAL_SECONDS", 0.01)

    async with feeder.CYCLE_LOCK:
        task = asyncio.create_task(feeder.watch())
        try:
            rig.deliver(1)
            await asyncio.sleep(0.1)
            # The lock is held, so the watcher skipped rather than waited.
            assert db.counts()["confirmed"] == 0
        finally:
            task.cancel()


async def test_the_watcher_survives_an_error(rig, monkeypatch):
    from app import feeder

    boom = {"n": 0}

    def exploding():
        boom["n"] += 1
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(feeder, "reconcile", exploding)
    monkeypatch.setattr(feeder, "WATCH_INTERVAL_SECONDS", 0.01)
    task = asyncio.create_task(feeder.watch())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert boom["n"] > 1, "the watcher died on the first error"
