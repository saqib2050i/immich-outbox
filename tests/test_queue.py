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


# ---- saying why nothing is moving ---------------------------------------

async def status_of(rig):
    from app import main
    return (await main.queue())["status"]


async def test_status_says_the_outbox_is_full(rig, monkeypatch):
    """The normal steady state of this system, and the one thing the
    dashboard never said out loud. Full is not free space reaching zero --
    it is the next file no longer fitting."""
    from app import db, feeder, immich, settings

    rig.cap(1000)
    db.upsert_assets([asset(i, size=300) for i in range(3)]
                     + [asset(50 + i, size=300) for i in range(5)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    feeder.reconcile()          # the cycle's second pass, which records usage

    s = await status_of(rig)
    assert s["state"] == "full", s
    assert "full" in s["detail"] and "phone" in s["detail"]
    assert s["free_bytes"] < 300, "free space alone was used, not whether it fits"


async def test_status_says_paused(rig):
    from app import db, settings
    settings.save({"paused": True})
    db.upsert_assets([asset(0, size=100)])
    s = await status_of(rig)
    assert s["state"] == "paused" and "paused" in s["detail"].lower()


async def test_status_says_idle_when_nothing_waits(rig):
    s = await status_of(rig)
    assert s["state"] == "idle"
    assert s["waiting"] == 0


async def test_status_says_waiting_when_there_is_room(rig):
    from app import db
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    s = await status_of(rig)
    assert s["state"] == "waiting"
    assert s["waiting"] == 3
    assert "next check" in s["detail"]


async def test_status_reports_a_missing_outbox(rig):
    from app import config, db
    db.upsert_assets([asset(0, size=100)])
    db.mark_queued(["asset-0"])
    config.OUTBOX_DIR = str(rig.root / "not-mounted")
    s = await status_of(rig)
    assert s["state"] == "blocked"
    assert "refusing" in s["detail"]


async def test_waiting_count_excludes_the_resting_library(rig):
    """Files outside every window are not waiting for anything."""
    from app import db, settings

    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-28",
                   "backfill_enabled": False})
    db.upsert_assets([asset(0, size=100, taken="2026-08-30")]
                     + [asset(10 + i, size=100, taken="2015-01-01") for i in range(20)])
    s = await status_of(rig)
    assert s["waiting"] == 1, "the resting library was counted as queued"
