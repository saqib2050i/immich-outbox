"""Live transfer progress, and knowing which image is running."""

import asyncio

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def test_progress_reports_bytes_as_they_arrive(rig, monkeypatch):
    """The complaint: files appear with no sign of how far along the one
    being fetched is."""
    from app import db, feeder, immich, settings

    settings.save({"photo_workers": 1, "video_workers": 1})
    db.upsert_assets([asset(i, size=400_000) for i in range(2)])

    seen = []
    real = fake_download()

    async def watched(asset_id):
        resp, client = await real(asset_id)
        original = resp.aiter_bytes

        async def spy(chunk):
            async for part in original(chunk):
                seen.append(feeder.transfer_snapshot())
                yield part
        resp.aiter_bytes = spy
        return resp, client

    monkeypatch.setattr(immich, "stream_original", watched)
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 2

    live = [s for s in seen if s["transfers"]]
    assert live, "no progress was published during the transfer"
    assert all(t["size"] == 400_000
               for s in live for t in s["transfers"])

    # Bytes only ever go up within a file.
    first = [t["bytes"] for s in live for t in s["transfers"]
             if t["filename"] == "IMG_0000.jpg"]
    assert first == sorted(first)

    # And the batch is reported alongside.
    assert all(s["batch"]["files_total"] == 2 for s in live)


async def test_photos_and_videos_move_in_separate_lanes(rig, monkeypatch):
    """A 4 GB video used to occupy the whole feeder while photos waited."""
    from app import db, feeder, immich, settings

    settings.save({"photo_workers": 3, "video_workers": 1})
    db.upsert_assets([asset(i, size=1000) for i in range(6)]
                     + [asset(50 + i, size=1000, kind="VIDEO") for i in range(3)])

    peak = {"IMAGE": 0, "VIDEO": 0}
    real = fake_download()

    async def watched(asset_id):
        resp, client = await real(asset_id)
        original = resp.aiter_bytes

        async def spy(chunk):
            async for part in original(chunk):
                live = feeder.transfer_snapshot()["transfers"]
                for kind in peak:
                    n = sum(1 for t in live if t["kind"] == kind)
                    peak[kind] = max(peak[kind], n)
                await asyncio.sleep(0)
                yield part
        resp.aiter_bytes = spy
        return resp, client

    monkeypatch.setattr(immich, "stream_original", watched)
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 9

    assert peak["IMAGE"] <= 3, f"photo lane exceeded its width: {peak}"
    assert peak["VIDEO"] <= 1, f"video lane exceeded its width: {peak}"


async def test_a_lane_set_to_zero_still_moves(rig, monkeypatch):
    """Zero workers would silently stop that kind entirely."""
    from app import db, feeder, immich, settings

    settings.save({"photo_workers": 0, "video_workers": 0})
    assert settings.load().lanes == {"IMAGE": 1, "VIDEO": 1}

    db.upsert_assets([asset(0, size=100), asset(1, size=100, kind="VIDEO")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 2


async def test_progress_is_cleared_when_the_batch_ends(rig, monkeypatch):
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=1000)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    snap = feeder.transfer_snapshot()
    assert snap["transfers"] == [] and snap["batch"] is None


async def test_progress_is_none_when_idle(rig):
    from app import feeder
    assert feeder.transfer_snapshot() == {"transfers": [], "batch": None,
                                          "last_fill": None}


async def test_the_snapshot_carries_rate_and_eta(rig):
    from app import feeder

    feeder.TRANSFERS["a"] = {
        "filename": "x.jpg", "asset_id": "a", "kind": "IMAGE",
        "bytes": 500_000, "size": 1_000_000,
        "started": __import__("time").monotonic() - 2.0}
    try:
        s = feeder.transfer_snapshot()["transfers"][0]
        assert s["bytes_per_second"] > 0
        assert s["eta_seconds"] is not None and s["eta_seconds"] > 0
        assert "started" not in s, "the raw clock leaked into the payload"
    finally:
        feeder.TRANSFERS.clear()


async def test_progress_does_not_touch_the_ledger(rig):
    """It must not ride the event stream: one event per chunk would redraw
    every open dashboard hundreds of times per file."""
    from app import db, feeder

    feeder.TRANSFERS["a"] = {"filename": "x", "asset_id": "a", "kind": "IMAGE",
                             "bytes": 1, "size": 2,
                             "started": __import__("time").monotonic()}
    try:
        before = db.revision()
        for _ in range(50):
            feeder.transfer_snapshot()
        assert db.revision() == before, "reading progress bumped the ledger"
    finally:
        feeder.TRANSFERS.clear()


async def test_the_progress_endpoint_is_shaped_for_the_dashboard(rig):
    from app import main
    assert await main.progress() == {"transfers": [], "batch": None,
                                     "last_fill": None}


async def test_a_finished_fill_is_still_reported(rig, monkeypatch):
    """On a fast network a whole batch lands in under a second, so live bars
    are a blink. The panel must still be able to say what just happened
    instead of looking idle."""
    from app import db, feeder, immich

    db.upsert_assets([asset(i, size=1000) for i in range(3)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3

    snap = feeder.transfer_snapshot()
    assert snap["transfers"] == [] and snap["batch"] is None
    assert snap["last_fill"]["files"] == 3
    assert snap["last_fill"]["bytes"] == 3000
    assert snap["last_fill"]["at"]


async def test_a_fill_that_wrote_nothing_leaves_the_summary_alone(rig, monkeypatch):
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100)])

    async def boom(asset_id):
        raise RuntimeError("nope")
    monkeypatch.setattr(immich, "stream_original", boom)
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert feeder.transfer_snapshot()["last_fill"] is None


async def test_status_reports_the_running_build(rig):
    from app import config, main
    d = await main.status()
    assert d["app"]["version"] == config.APP_VERSION
    assert "revision" in d["app"] and "built_at" in d["app"]


async def test_healthz_reports_the_build_without_a_session(rig, monkeypatch):
    """So a deploy can be checked with curl; /healthz is outside the gate."""
    from app import config, immich, main

    async def no_ping():
        return False
    monkeypatch.setattr(immich, "ping", no_ping)

    d = await main.healthz()
    assert d["version"] == config.APP_VERSION
    assert d["ok"] is True


async def test_the_build_stamp_falls_back_to_dev(rig):
    from app import config
    assert config.APP_VERSION == "dev", "an unstamped build should say so"


# ---- the version has to mean something to a person ----------------------

@pytest.mark.parametrize("version,revision", [
    ("build 128", "9f3c1ab"),
    ("v1.4.0", "9f3c1ab"),
    ("dev", ""),
])
async def test_the_build_stamp_is_reported_verbatim(rig, monkeypatch, version, revision):
    """The server passes the stamp through; the dashboard does the wording."""
    from app import config, main
    monkeypatch.setattr(config, "APP_VERSION", version)
    monkeypatch.setattr(config, "APP_REVISION", revision)
    monkeypatch.setattr(config, "APP_BUILT_AT", "2026-08-30T09:15:00Z")

    d = await main.status()
    assert d["app"] == {"version": version, "revision": revision,
                        "built_at": "2026-08-30T09:15:00Z"}


async def test_a_long_commit_sha_is_shortened(rig, monkeypatch):
    from app import config, main
    monkeypatch.setattr(config, "APP_REVISION", "9f3c1ab7d2e5c4b1a0987654321fedcba9876543")
    d = await main.status()
    assert d["app"]["revision"] == "9f3c1ab"
    assert len(d["app"]["revision"]) == 7
