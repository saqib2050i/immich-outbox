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


async def test_cancel_with_skip_does_not_come_back(rig, monkeypatch):
    """The complaint: cancel-all, press refresh, forty more appear. With
    mode=skip the cancelled files must not rejoin the queue on the next
    top-up."""
    from app import db, feeder, immich, main, settings

    settings.save({"max_batch_files": 40})
    db.upsert_assets([asset(i, size=100) for i in range(10)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 10

    r = await main.queue_cancel({"all": True, "mode": "skip"})
    assert r["cancelled"] == 10 and r["mode"] == "skip"
    assert rig.files() == set()

    # The refresh that used to bring forty more back.
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0, "skipped files were re-sent"
    assert db.counts()["skipped"] == 10
    assert db.counts()["confirmed"] == 0


async def test_cancel_default_still_means_send_later(rig, monkeypatch):
    from app import db, feeder, immich, main

    db.upsert_assets([asset(0, size=100)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    await main.queue_cancel({"all": True})
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1, "default cancel should allow resend"


async def test_dismiss_failed_clears_without_resending(rig, monkeypatch):
    from app import db, feeder, immich, main, settings

    settings.save({"alert_failed_count": 2})
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    for i in range(3):
        db.mark_failed(f"asset-{i}", "original missing from Immich storage (HTTP 404)",
                       permanent=True)

    r = await main.failed_dismiss({})
    assert r["dismissed"] == 3
    assert db.counts()["failed"] == 0
    assert db.counts()["skipped"] == 3
    assert db.problems() == []

    # Not eligible for sending any more...
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0

    # ...and the failures alert is gone.
    from app import alerts
    assert not any(a["key"] == "failures" for a in alerts.evaluate())


async def test_dismiss_specific_failed_ids(rig):
    from app import db, main

    db.upsert_assets([asset(i, size=100) for i in range(2)])
    db.mark_failed("asset-0", "boom")
    db.mark_failed("asset-1", "boom")
    r = await main.failed_dismiss({"ids": ["asset-0"]})
    assert r["dismissed"] == 1
    assert db.counts()["failed"] == 1


async def test_a_dismissed_file_can_be_deliberately_resent(rig):
    """Skip is an exclusion, not a tombstone."""
    from app import db

    db.upsert_assets([asset(0, size=100)])
    db.mark_failed("asset-0", "boom")
    db.dismiss_failed(["asset-0"])
    assert db.force_send(ids=["asset-0"]) == 1
    row = db.connect().execute("SELECT state, forced FROM assets WHERE id='asset-0'").fetchone()
    assert (row["state"], row["forced"]) == ("pending", 1)


async def test_dismiss_never_touches_motion_parts_protection(rig):
    """force_send resurrecting skipped rows must still exclude components."""
    from app import db

    db.upsert_assets([asset(0, size=100)])
    db.mark_motion_parts(["asset-0"])
    assert db.force_send(ids=["asset-0"]) == 0


# ---- the backlog: the pile behind the queue -------------------------------

async def test_the_backlog_is_visible_by_month(rig):
    from app import db, main

    db.upsert_assets(
        [asset(i, size=100, taken="2025-11-05") for i in range(4)] +
        [asset(10 + i, size=200, taken="2026-01-09") for i in range(2)])
    db.mark_failed("asset-0", "original missing from Immich storage (HTTP 404)",
                   permanent=True)

    b = await main.backlog()
    by = {m["month"]: m for m in b["months"]}
    assert b["total"] == 6
    assert by["2025-11"]["total"] == 4 and by["2025-11"]["failed"] == 1
    assert by["2026-01"]["total"] == 2
    assert b["months"][0]["month"] == "2026-01", "newest first"


async def test_dismissing_a_month_stops_it_refilling_the_queue(rig, monkeypatch):
    """The real case: a month was force-sent, the originals were then
    deleted from Immich, and every refresh pulled another forty."""
    from app import db, feeder, immich, main, settings

    settings.save({"max_batch_files": 5})
    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(20)]
                     + [asset(50, size=100, taken="2026-02-01")])
    db.force_send_month("2025-11")

    r = await main.backlog_dismiss({"month": "2025-11"})
    assert r["dismissed"] == 20

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    added = await feeder.top_up(used)
    assert added == 1, "a dismissed month came back"
    assert db.queue_contents()[0]["taken_at"].startswith("2026-02")

    assert (await main.backlog())["total"] == 0


async def test_a_dismissed_month_can_be_sent_again_after_a_rescan(rig):
    """Dismissing is an exclusion, not a tombstone: once the files are back
    in Immich, sending the month must work."""
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(3)])
    await main.backlog_dismiss({"month": "2025-11"})
    assert db.counts()["skipped"] == 3

    assert db.force_send_month("2025-11") == 3
    assert db.counts()["pending"] == 3
    assert (await main.backlog())["total"] == 3


async def test_dismissing_the_whole_backlog(rig):
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(5)]
                     + [asset(10 + i, size=100, taken="2019-03-02") for i in range(5)])
    r = await main.backlog_dismiss({"all": True})
    assert r["dismissed"] == 10
    assert (await main.backlog())["total"] == 0
    assert db.counts()["skipped"] == 10


async def test_dismissing_the_backlog_leaves_the_outbox_alone(rig, monkeypatch):
    """Files already written are the phone's business; only the waiting
    pile is dismissed."""
    from app import db, feeder, immich, main, settings

    settings.save({"max_batch_files": 3})
    db.upsert_assets([asset(i, size=100) for i in range(8)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3

    await main.backlog_dismiss({"all": True})
    assert db.counts()["queued"] == 3
    assert len(rig.files()) == 3
    assert db.counts()["confirmed"] == 0


async def test_dismissing_never_touches_confirmed_assets(rig):
    """They are in Google Photos; their record must not be rewritten."""
    from app import db, main

    db.upsert_assets([asset(0, size=100)])
    db.mark_queued(["asset-0"])
    db.confirm_absent([])
    assert db.counts()["confirmed"] == 1

    await main.backlog_dismiss({"all": True})
    assert db.counts()["confirmed"] == 1
    assert db.counts()["skipped"] == 0


async def test_backlog_dismiss_needs_a_scope(rig):
    from fastapi import HTTPException
    from app import main
    with pytest.raises(HTTPException) as exc:
        await main.backlog_dismiss({})
    assert exc.value.status_code == 400
