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

    rig.cap(300)                      # three 100-byte files fill it
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


# ---- undoing a dismissal --------------------------------------------------

async def test_dismissed_assets_are_findable_again(rig):
    """The trap: every view filters 'skipped' out, so dismissing the whole
    backlog hid 12,000 files with no way to see or restore them."""
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(4)]
                     + [asset(10 + i, size=100, taken="2026-02-01") for i in range(2)])
    await main.backlog_dismiss({"all": True})
    assert (await main.backlog())["total"] == 0

    d = await main.dismissed()
    assert d["total"] == 6
    assert {m["month"]: m["total"] for m in d["months"]} == {"2025-11": 4, "2026-02": 2}


async def test_restoring_a_month_puts_it_back_in_the_backlog(rig):
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(4)]
                     + [asset(10 + i, size=100, taken="2026-02-01") for i in range(2)])
    await main.backlog_dismiss({"all": True})

    r = await main.dismissed_restore({"month": "2025-11"})
    assert r["restored"] == 4
    assert (await main.backlog())["total"] == 4
    assert (await main.dismissed())["total"] == 2


async def test_restoring_everything(rig, monkeypatch):
    from app import db, feeder, immich, main

    db.upsert_assets([asset(i, size=100) for i in range(5)])
    await main.backlog_dismiss({"all": True})
    assert db.counts()["skipped"] == 5

    assert (await main.dismissed_restore({"all": True}))["restored"] == 5
    assert db.counts()["pending"] == 5

    # And they actually send again.
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 5


async def test_restore_never_resurrects_motion_components(rig):
    """They are skipped because of what they are, not by choice."""
    from app import db, main

    db.upsert_assets([asset(0, size=100), asset(1, size=50, kind="VIDEO")])
    db.mark_motion_parts(["asset-1"])
    await main.backlog_dismiss({"all": True})

    assert (await main.dismissed())["total"] == 1, "a motion clip was offered for restore"
    assert (await main.dismissed_restore({"all": True}))["restored"] == 1
    row = db.connect().execute("SELECT state FROM assets WHERE id='asset-1'").fetchone()
    assert row["state"] == "skipped"


async def test_reconciliation_separates_dismissed_from_motion_clips(rig):
    """It reported 12,000 hand-dismissed photos as 'motion-photo clips'."""
    from app import db, main

    db.upsert_assets([asset(i, size=100) for i in range(4)])
    db.mark_motion_parts(["asset-3"])
    await main.backlog_dismiss({"all": True})

    groups = {g["reason"]: g["total"] for g in db.reconciliation()}
    assert groups["dismissed"] == 3
    assert groups["motion_part"] == 1


async def test_a_rescan_does_not_undo_a_dismissal(rig):
    """Documenting why restore has to exist: scans are INSERT OR IGNORE, so
    they never touch an existing row."""
    from app import db, main
    from conftest import asset as mk

    db.upsert_assets([mk(0, size=100)])
    await main.backlog_dismiss({"all": True})
    db.upsert_assets([mk(0, size=100)])          # the rescan
    assert db.counts()["skipped"] == 1
    assert db.counts()["pending"] == 0


# ---- the library must not read as finished ------------------------------

async def test_dismissed_files_do_not_make_a_month_look_backed_up(rig):
    """Reported as "everything is being considered backed up": the timeline
    filtered skipped rows out, so a month whose files had been dismissed
    showed confirmed == total and read as done."""
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(10)])
    db.mark_queued(["asset-0", "asset-1"])
    db.confirm_absent([])
    await main.backlog_dismiss({"all": True, "scope": "all"})

    row = next(m for m in db.timeline() if m["month"] == "2025-11")
    assert row["total"] == 10, "dismissed files vanished from the timeline"
    assert row["confirmed"] == 2
    assert row["dismissed"] == 8
    assert row["confirmed"] != row["total"], "the month still reads as done"


async def test_a_wholly_dismissed_month_still_appears(rig):
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2024-07-01") for i in range(5)])
    await main.backlog_dismiss({"all": True, "scope": "all"})

    assert any(m["month"] == "2024-07" for m in db.timeline())
    assert any(m["month"] == "2024-07" for m in db.monthly_breakdown())
    assert db.month_detail("2024-07")["groups"], "the month detail went empty"


async def test_motion_clips_stay_out_of_the_library_views(rig):
    """They are not photos; only hand-dismissed rows come back into view."""
    from app import db

    db.upsert_assets([asset(0, size=100, taken="2026-03-01"),
                      asset(1, size=50, kind="VIDEO", taken="2026-03-01")])
    db.mark_motion_parts(["asset-1"])
    row = next(m for m in db.timeline() if m["month"] == "2026-03")
    assert row["total"] == 1


# ---- the backlog is what you asked for, not the whole library ------------

async def test_the_backlog_separates_asked_from_the_rest_of_the_library(rig):
    """Reported as "backlog should not contain the whole library, just the
    files i asked to send"."""
    from app import db, main, settings

    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-01-01",
                   "backfill_enabled": False})
    db.upsert_assets(
        [asset(i, size=100, taken="2026-02-01") for i in range(3)] +      # eligible
        [asset(10 + i, size=100, taken="2015-06-01") for i in range(50)])  # resting
    db.force_send(ids=["asset-10", "asset-11"])                           # asked

    b = await main.backlog()
    assert b["asked"] == 2
    assert b["eligible"] == 3
    assert b["resting"] == 48
    assert b["total"] == 53


async def test_dismiss_everything_spares_the_resting_library(rig):
    """The one-click mistake: 12,000 files that were never queued for
    anything got dismissed because they happened to be 'pending'."""
    from app import db, main, settings

    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-01-01",
                   "backfill_enabled": False})
    db.upsert_assets(
        [asset(i, size=100, taken="2026-02-01") for i in range(3)] +
        [asset(10 + i, size=100, taken="2015-06-01") for i in range(50)])
    db.force_send(ids=["asset-10"])

    r = await main.backlog_dismiss({"all": True})       # default scope
    assert r["dismissed"] == 4, "the resting library was dismissed too"

    b = await main.backlog()
    assert b["total"] == 49 and b["resting"] == 49
    assert b["asked"] == 0 and b["eligible"] == 0


async def test_dismiss_scope_all_is_still_available(rig, monkeypatch):
    from app import db, main, settings

    settings.save({"ongoing_enabled": False, "backfill_enabled": False})
    db.upsert_assets([asset(i, size=100, taken="2015-06-01") for i in range(5)])
    assert (await main.backlog_dismiss({"all": True}))["dismissed"] == 0
    assert (await main.backlog_dismiss({"all": True, "scope": "all"}))["dismissed"] == 5


async def test_dismissing_a_named_month_ignores_scope(rig):
    """Clicking Dismiss on a month means that month, window or not."""
    from app import db, main, settings

    settings.save({"ongoing_enabled": False, "backfill_enabled": False})
    db.upsert_assets([asset(i, size=100, taken="2015-06-01") for i in range(4)])
    assert (await main.backlog_dismiss({"month": "2015-06"}))["dismissed"] == 4


async def test_an_unknown_scope_is_rejected(rig):
    from fastapi import HTTPException
    from app import main
    with pytest.raises(HTTPException) as exc:
        await main.backlog_dismiss({"all": True, "scope": "everything-please"})
    assert exc.value.status_code == 400


# ---- a dismissed month must stay reachable ------------------------------

async def test_a_dismissed_month_is_not_reported_as_finished(rig):
    """Reported: clear the queue and the library goes green, with no send
    option when you open a month."""
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2026-03-05") for i in range(6)])
    db.mark_queued(["asset-0"])
    db.confirm_absent([])
    await main.backlog_dismiss({"all": True, "scope": "all"})

    row = next(m for m in db.timeline() if m["month"] == "2026-03")
    assert row["confirmed"] == 1
    assert row["total"] == 6
    assert row["confirmed"] != row["total"], "month would render as done"

    detail = await main.month_detail("2026-03")
    sendable = sum(g["remaining"] + g["dismissed"] for g in detail["groups"])
    assert sendable == 5, "the month offered nothing to send"


async def test_month_detail_counts_dismissed_separately(rig):
    from app import db, main

    db.upsert_assets([asset(i, size=100, taken="2026-04-05") for i in range(4)])
    await main.backlog_dismiss({"all": True, "scope": "all"})

    groups = (await main.month_detail("2026-04"))["groups"]
    assert sum(g["dismissed"] for g in groups) == 4
    assert sum(g["remaining"] for g in groups) == 0


async def test_sending_a_dismissed_month_puts_it_back(rig, monkeypatch):
    """The send button has to actually work on a dismissed month."""
    from app import db, feeder, immich, main

    db.upsert_assets([asset(i, size=100, taken="2026-05-05") for i in range(4)])
    await main.backlog_dismiss({"all": True, "scope": "all"})
    assert db.counts()["skipped"] == 4

    r = await main.month_send({"month": "2026-05"})
    assert r["queued"] == 4

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 4


async def test_a_month_outside_the_window_is_still_sendable_by_hand(rig, monkeypatch):
    """With ongoing_from set to a recent date, old months are not eligible
    automatically -- 'send this month' is the way to move them."""
    from app import db, feeder, immich, main, settings

    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-28",
                   "backfill_enabled": False})
    db.upsert_assets([asset(i, size=100, taken="2025-11-05") for i in range(3)])

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0, "an out-of-window month sent itself"

    assert (await main.month_send({"month": "2025-11"}))["queued"] == 3
    assert db.counts()["queued"] == 3
