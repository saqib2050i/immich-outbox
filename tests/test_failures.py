"""Failure triage: what broke, said honestly, retried only when retrying helps."""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def test_a_404_fails_permanently_in_one_attempt(rig, monkeypatch):
    """Immich returning 404 for an original means the file is gone from its
    storage. Five retries produce five identical failures, so the whole
    retry budget is spent at once."""
    from app import db, feeder, immich

    async def missing(asset_id):
        raise immich.OriginalMissing(
            "original missing from Immich storage (HTTP 404) — the file "
            "is likely offline or moved out of an external library")

    db.upsert_assets([asset(0, size=100)])
    monkeypatch.setattr(immich, "stream_original", missing)

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0

    row = db.connect().execute("SELECT * FROM assets WHERE id='asset-0'").fetchone()
    assert row["state"] == "failed"
    assert row["attempts"] == db.MAX_ATTEMPTS, "a 404 should not be retried"

    # And it is not claimed again on the next cycle.
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0


async def test_a_transient_failure_still_gets_its_retries(rig, monkeypatch):
    from app import db, feeder, immich

    async def flaky(asset_id):
        raise RuntimeError("ConnectError: [Errno -2] Name or service not known")

    db.upsert_assets([asset(0, size=100)])
    monkeypatch.setattr(immich, "stream_original", flaky)
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    row = db.connect().execute("SELECT attempts FROM assets WHERE id='asset-0'").fetchone()
    assert row["attempts"] == 1, "a transient error burnt more than one attempt"


async def test_retry_failed_resurrects_even_permanent_failures(rig, monkeypatch):
    """After the user fixes the Immich library, 'Send failed again' must
    work on 404s too — permanence is about automatic retries, not about
    forbidding a deliberate one."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100)])
    db.mark_failed("asset-0", "original missing from Immich storage (HTTP 404)",
                   permanent=True)
    assert db.retry_failed() == 1

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1


async def test_the_breakdown_groups_by_what_went_wrong(rig):
    from app import db

    db.upsert_assets([asset(i, size=100) for i in range(6)])
    db.mark_failed("asset-0", "original missing from Immich storage (HTTP 404)", permanent=True)
    db.mark_failed("asset-1", "HTTP 404 Not Found", permanent=True)
    db.mark_failed("asset-2", "ConnectError: [Errno -2] Name or service not known")
    db.mark_failed("asset-3", "size mismatch: got 12, expected 100")
    db.mark_failed("asset-4", "something exotic")

    kinds = {b["kind"]: b["total"] for b in db.failure_breakdown()}
    assert kinds == {"missing": 2, "unreachable": 1, "truncated": 1, "other": 1}


async def test_the_alert_names_missing_originals_not_folder_permissions(rig):
    """The old text sent a 404 sufferer to check Syncthing ownership."""
    from app import alerts, db, settings

    settings.save({"alert_failed_count": 2})
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    for i in range(3):
        db.mark_failed(f"asset-{i}",
                       "original missing from Immich storage (HTTP 404)",
                       permanent=True)

    fail = next(a for a in alerts.evaluate() if a["key"] == "failures")
    assert "missing from Immich itself" in fail["message"]
    assert "404" in fail["message"]
    assert "folder permissions" not in fail["message"]


async def test_cancel_all_empties_the_queue_without_ids(rig, monkeypatch):
    """Nobody ticks a hundred checkboxes."""
    from app import db, feeder, immich, main, settings

    settings.save({"max_batch_files": 40})
    db.upsert_assets([asset(i, size=100) for i in range(12)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 12

    r = await main.queue_cancel({"all": True})
    assert r["cancelled"] == 12 and r["removed"] == 12
    assert rig.files() == set()
    assert db.counts()["pending"] == 12
    assert db.counts()["confirmed"] == 0, "cancel-all was read as a backup"


async def test_cancel_all_with_an_empty_queue_is_a_no_op(rig):
    from app import main
    r = await main.queue_cancel({"all": True})
    assert r == {"ok": True, "cancelled": 0, "removed": 0}
