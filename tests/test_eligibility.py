"""What is allowed out of the ledger, and when.

One rule runs by itself -- everything from the cut-off date onwards.
Anything older goes only when it is asked for, which is what `forced`
means. There used to be a second window, a start/end range stepped forward
by hand; Library lists every month with what is left in it, so the window
was a second place for the same decision to live.

Eligibility is applied at release time, not at scan time, so the ledger
always holds the whole library and moving the cut-off frees assets on the
next cycle instead of needing a rescan.
"""

import pytest

from conftest import asset

pytestmark = pytest.mark.asyncio


def claim(**over):
    """What the feeder would take right now.

    Built from cfg.eligibility rather than assembled by hand, so a test
    cannot pass against a filter shape the feeder no longer uses.
    """
    from app import db, settings
    cfg = settings.load()
    filt = dict(cfg.eligibility)
    filt.update(over)
    return [r["id"] for r in db.claim_batch(cfg.outbox_max_bytes, 40, filt)]


async def test_ongoing_window_is_a_floor(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01"), asset(1, taken="2026-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01"})
    assert claim() == ["asset-1"]




async def test_moving_the_cutoff_needs_no_rescan(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01"})
    assert claim() == []
    # The row was never dropped, only withheld.
    settings.save({"ongoing_from": "2010-01-01"})
    assert claim() == ["asset-0"]


async def test_forced_ignores_the_windows_and_jumps_the_queue(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01"), asset(1, taken="2026-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01"})
    assert db.force_send(ids=["asset-0"]) == 1
    assert claim() == ["asset-0", "asset-1"], "forced asset did not go first"


async def test_forced_never_resends_a_confirmed_asset(rig):
    """It is already in Google Photos; sending again makes a duplicate."""
    from app import db

    db.upsert_assets([asset(0), asset(1)])
    db.mark_queued(["asset-0"])
    db.confirm_absent([])
    assert db.counts()["confirmed"] == 1

    assert db.force_send(ids=["asset-0", "asset-1"]) == 1
    assert claim() == ["asset-1"]


async def test_video_can_be_switched_off(rig):
    from app import db

    db.upsert_assets([asset(0), asset(1, kind="VIDEO")])
    assert claim(include_video=True) == ["asset-0", "asset-1"]
    assert claim(include_video=False) == ["asset-0"]


async def test_oversized_assets_are_withheld(rig):
    from app import db

    db.upsert_assets([asset(0, size=100), asset(1, size=10_000)])
    assert claim(max_asset_bytes=1000) == ["asset-0"]


async def test_retries_stop_after_five_attempts(rig):
    from app import db

    db.upsert_assets([asset(0)])
    for _ in range(5):
        db.mark_failed("asset-0", "nope")
    assert claim() == []
    assert db.retry_failed() == 1
    assert claim() == ["asset-0"]




# ---- the second window is gone --------------------------------------

async def test_there_is_one_automatic_rule(rig):
    """A window stepped forward by hand was a second place for the same
    decision to live, and Library already lists every month with what is
    left in it. Anything older than the cut-off waits to be asked for."""
    from app import settings
    spec = set(settings.SPEC)
    assert not [k for k in spec if "backfill" in k], \
        "the backfill window is still configurable"
    assert "ongoing_from" in spec
    assert set(settings.load().eligibility) == {
        "include_video", "max_asset_bytes", "ongoing", "ongoing_from", "fix_dates"}


async def test_old_photos_wait_to_be_asked_for(rig):
    """The behaviour that replaces the window: outside the cut-off nothing
    moves until it is forced, and forcing is what Library's month buttons
    do."""
    from app import db, settings
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-01-01"})
    db.upsert_assets([asset(0, taken="2015-06-01"), asset(1, taken="2026-06-01")])

    assert claim() == ["asset-1"], "only what is inside the cut-off"

    db.force_send_month("2015-06")
    assert set(claim()) == {"asset-0", "asset-1"}, "asked for, so it goes"


async def test_a_stale_backfill_setting_is_simply_ignored(rig):
    """Upgrading leaves cfg_backfill_* rows in the ledger. load() reads only
    the keys it knows, so they are inert rather than a migration."""
    from app import db, settings
    db.set_meta("cfg_backfill_enabled", "true")
    db.set_meta("cfg_backfill_start", "2015-01-01")

    cfg = settings.load()
    assert not hasattr(cfg, "backfill_enabled")
    assert "backfill" not in str(cfg.eligibility)
