"""Motion photos are two Immich assets.

The still carries the embedded clip; the extracted component must never be
relayed on its own or Google Photos shows a stray video beside the photo.
The component can be scanned before or after the still that points at it, so
both orders have to end the same way.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio

STILL, CLIP = "asset-0", "asset-1"


def _pair():
    still = asset(0, size=100)
    clip = asset(1, size=50, kind="VIDEO")
    return still, clip


async def test_component_skipped_when_the_still_is_seen_first(rig, monkeypatch):
    from app import db, feeder, immich

    still, clip = _pair()
    db.upsert_assets([still])
    db.mark_motion_parts([CLIP])
    db.upsert_assets([clip])

    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    assert rig.files() == {"IMG_0000.jpg"}
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "skipped"


async def test_component_retired_when_it_is_seen_first(rig, monkeypatch):
    """The component arrives, gets queued, and only then is it identified."""
    from app import db, feeder, immich

    still, clip = _pair()
    db.upsert_assets([clip, still])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert len(rig.files()) == 2        # both went out, nothing knew better

    # Now the still is scanned and names its component.
    db.mark_motion_parts([CLIP])
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "skipped"

    # The stray clip is cleaned out of the outbox -- the one deletion this
    # service is allowed to make.
    feeder.reconcile()
    assert rig.files() == {"IMG_0000.jpg"}
    # And its absence was NOT read as a Google Photos confirmation.
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "skipped"
    assert db.counts()["confirmed"] == 0


async def test_a_confirmed_component_is_left_alone(rig):
    """Rewriting history would not remove it from Google Photos."""
    from app import db

    still, clip = _pair()
    db.upsert_assets([clip, still])
    db.mark_queued([CLIP])
    db.confirm_absent([])
    assert db.counts()["confirmed"] == 1

    db.mark_motion_parts([CLIP])
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "confirmed"


async def test_components_stay_skipped_through_a_resend(rig):
    """That an asset is a component is a fact about the library, not test
    state, so 'send everything again' must not resurrect it."""
    from app import db

    still, clip = _pair()
    db.upsert_assets([still, clip])
    db.mark_motion_parts([CLIP])

    db.reset_states()
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "skipped"

    db.requeue_many([STILL, CLIP])
    assert db.connect().execute(
        "SELECT state FROM assets WHERE id=?", (CLIP,)).fetchone()["state"] == "skipped"


async def test_components_are_never_claimed(rig):
    from app import db, settings

    still, clip = _pair()
    db.upsert_assets([still, clip])
    db.mark_motion_parts([CLIP])
    cfg = settings.load()
    rows = db.claim_batch(cfg.outbox_max_bytes, 40, {
        "include_video": True, "max_asset_bytes": cfg.max_asset_bytes,
        "ongoing": True, "ongoing_from": "1970-01-01", "backfill": False,
        "backfill_start": "2015-01-01", "backfill_end": "2015-01-31"})
    assert [r["id"] for r in rows] == [STILL]
