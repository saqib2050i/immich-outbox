"""What is allowed out of the ledger, and when.

Eligibility is applied at release time, not at scan time, so the ledger
always holds the whole library and widening a window frees assets on the
next cycle instead of needing a rescan.
"""

import pytest

from conftest import asset

pytestmark = pytest.mark.asyncio


def claim(**over):
    from app import db, settings
    cfg = settings.load()
    filt = {"include_video": cfg.include_video,
            "max_asset_bytes": cfg.max_asset_bytes,
            "ongoing": cfg.ongoing_enabled, "ongoing_from": cfg.ongoing_from,
            "backfill": cfg.backfill_enabled,
            "backfill_start": cfg.backfill_start,
            "backfill_end": cfg.backfill_end}
    filt.update(over)
    return [r["id"] for r in db.claim_batch(cfg.outbox_max_bytes, 40, filt)]


async def test_ongoing_window_is_a_floor(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01"), asset(1, taken="2026-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01",
                   "backfill_enabled": False})
    assert claim() == ["asset-1"]


async def test_backfill_window_is_a_range(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-12-31"), asset(1, taken="2015-01-15"),
                      asset(2, taken="2015-02-01")])
    settings.save({"ongoing_enabled": False, "backfill_enabled": True,
                   "backfill_start": "2015-01-01", "backfill_end": "2015-01-31"})
    assert claim() == ["asset-1"]


async def test_windows_combine(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2015-01-15"), asset(1, taken="2019-06-01"),
                      asset(2, taken="2026-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-01-01",
                   "backfill_enabled": True, "backfill_start": "2015-01-01",
                   "backfill_end": "2015-01-31"})
    assert claim() == ["asset-0", "asset-2"]


async def test_widening_a_window_needs_no_rescan(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01",
                   "backfill_enabled": False})
    assert claim() == []
    # The row was never dropped, only withheld.
    settings.save({"ongoing_from": "2010-01-01"})
    assert claim() == ["asset-0"]


async def test_forced_ignores_the_windows_and_jumps_the_queue(rig):
    from app import db, settings

    db.upsert_assets([asset(0, taken="2014-06-01"), asset(1, taken="2026-06-01")])
    settings.save({"ongoing_enabled": True, "ongoing_from": "2020-01-01",
                   "backfill_enabled": False})
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


async def test_the_backfill_window_steps_a_calendar_month(rig):
    from app import settings

    settings.save({"backfill_start": "2015-12-01", "backfill_end": "2015-12-31"})
    cfg = settings.advance_window(1)
    assert (cfg.backfill_start, cfg.backfill_end) == ("2016-01-01", "2016-01-31")

    cfg = settings.advance_window(1)
    assert (cfg.backfill_start, cfg.backfill_end) == ("2016-02-01", "2016-02-29")

    cfg = settings.advance_window(-1)
    assert (cfg.backfill_start, cfg.backfill_end) == ("2016-01-01", "2016-01-31")


async def test_window_progress_tracks_a_month(rig):
    from app import db

    db.upsert_assets([asset(i, taken="2015-01-10") for i in range(3)])
    p = db.window_progress("2015-01-01", "2015-01-31")
    assert (p["total"], p["remaining"], p["done"]) == (3, 3, False)

    db.mark_queued(["asset-0", "asset-1", "asset-2"])
    db.confirm_absent([])
    p = db.window_progress("2015-01-01", "2015-01-31")
    assert (p["confirmed"], p["done"]) == (3, True)
