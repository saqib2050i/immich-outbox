"""The cycle lock, and the chores that have to run without anyone watching."""

import asyncio

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def test_the_cap_holds_when_a_manual_send_meets_a_cycle(rig, monkeypatch):
    """Two top-ups at once would each size their batch against the same free
    space. The lock is what makes them take turns."""
    from app import db, feeder, immich, main, settings

    settings.save({"outbox_max_gb": 1, "max_batch_files": 40})
    mb = 1024 * 1024
    db.upsert_assets([asset(i, size=300 * mb) for i in range(8)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    await asyncio.gather(main.refresh(), main.refresh(), main.refresh())

    cap = settings.load().outbox_max_bytes
    assert rig.used() <= cap
    assert len(rig.files()) == 3            # 3 x 300 MB fits, a fourth does not


async def test_a_reset_cannot_land_inside_a_download(rig, monkeypatch):
    """The failure this guards: the reset empties the outbox and wipes the
    ledger while a download is in flight, the download then completes into
    the emptied folder, and the file is left with no ledger row -- an orphan
    that is downloaded again later under a new name, as a duplicate."""
    from app import db, feeder, immich, main, settings

    settings.save({"max_batch_files": 2})
    db.upsert_assets([asset(i, size=100) for i in range(2)])

    started, release = asyncio.Event(), asyncio.Event()
    real = fake_download()

    async def slow(asset_id):
        started.set()
        await release.wait()
        return await real(asset_id)

    monkeypatch.setattr(immich, "stream_original", slow)

    async def feeder_cycle():
        async with feeder.CYCLE_LOCK:
            _, used = feeder.reconcile()
            return await feeder.top_up(used)

    cycle = asyncio.create_task(feeder_cycle())
    await started.wait()

    reset = asyncio.create_task(main.reset({"scope": "outbox"}))
    await asyncio.sleep(0)                  # give the reset every chance to cut in
    release.set()

    added, result = await asyncio.gather(cycle, reset)
    assert added == 2
    assert result["ok"]

    # Whatever order they ran in, the outbox and the ledger agree: every file
    # present has a row, and every row that says 'queued' has a file.
    present, _ = feeder.reconcile()
    queued = {r["id"] for r in db.connect().execute(
        "SELECT id FROM assets WHERE state='queued'")}
    assert set(present) == queued
    assert len(rig.files()) == len(queued)


async def test_housekeeping_runs_the_alert_check(rig, monkeypatch):
    """Alerting only from an open dashboard is not alerting: the failure
    this system has is going quiet, and it goes quiet when nobody looks."""
    from app import db, feeder, settings

    settings.save({"backup_enabled": False, "alert_stall_days": 40})
    db.upsert_assets([asset(0)])
    db.mark_queued(["asset-0"])
    db.set_meta("outbox_problem", "the outbox is not mounted")

    await feeder.housekeeping()

    import json
    keys = {a["key"] for a in json.loads(db.get_meta("alerts") or "[]")}
    assert "outbox_missing" in keys


async def test_housekeeping_takes_the_scheduled_backup(rig):
    from app import backup, db, feeder, settings

    settings.save({"backup_enabled": True})
    db.upsert_assets([asset(0)])
    assert backup.list_backups() == []

    await feeder.housekeeping()
    assert len(backup.list_backups()) == 1

    # Not again on the next cycle: it is a daily chore.
    await feeder.housekeeping()
    assert len(backup.list_backups()) == 1


async def test_scheduled_backups_can_be_switched_off(rig):
    from app import backup, feeder, settings

    settings.save({"backup_enabled": False})
    await feeder.housekeeping()
    assert backup.list_backups() == []


async def test_housekeeping_survives_a_broken_webhook(rig, monkeypatch):
    from app import alerts, backup, db, feeder, settings

    async def boom():
        raise RuntimeError("webhook is on fire")

    monkeypatch.setattr(alerts, "check_and_notify", boom)
    settings.save({"backup_enabled": True})
    db.upsert_assets([asset(0)])

    await feeder.housekeeping()             # must not raise
    # The backup still happened: one broken chore must not skip the other.
    assert len(backup.list_backups()) == 1


async def test_a_fresh_install_is_not_reported_as_stalled(rig):
    """With no confirmation yet, fall back to when the oldest file went out
    -- otherwise the first cycle claims nothing has been confirmed 'ever'."""
    from app import alerts, db, settings

    settings.save({"alert_stall_days": 40})
    db.upsert_assets([asset(0)])
    db.mark_queued(["asset-0"])
    assert db.get_meta("last_confirm_at") is None

    keys = {a["key"] for a in alerts.evaluate()}
    assert "stalled" not in keys


async def test_a_genuine_stall_is_reported(rig):
    from app import alerts, db, settings

    settings.save({"alert_stall_days": 40})
    db.upsert_assets([asset(0)])
    db.mark_queued(["asset-0"])
    db.connect().execute(
        "UPDATE assets SET sent_at='2020-01-01T00:00:00+00:00'")
    db.connect().commit()

    keys = {a["key"] for a in alerts.evaluate()}
    assert "stalled" in keys


async def test_a_rescan_asks_the_worker_rather_than_starting_a_second_one(rig):
    from app import db, main

    await main.rescan()
    assert db.get_meta("force_full_scan") == "1"


async def test_the_rescan_flag_is_cleared_before_the_scan(rig, monkeypatch):
    """A rescan requested while one is running is a request for another, so
    clearing afterwards would swallow it."""
    from app import db, immich, worker

    seen = []

    async def one_empty_page(taken_after=None):
        seen.append(db.get_meta("force_full_scan"))
        return
        yield                                # pragma: no cover

    monkeypatch.setattr(immich, "list_assets", one_empty_page)
    db.set_meta("force_full_scan", "1")
    await worker.full_scan()

    assert seen == ["0"]
    assert db.get_meta("last_full_scan")
