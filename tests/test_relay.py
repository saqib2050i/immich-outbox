"""The core loop: fill, drain, refill.

The property that matters is that a confirmed asset never goes out again.
Everything else in the service exists to protect it.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def cycle(monkeypatch, budget_files=None):
    from app import feeder, immich
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    return await feeder.top_up(used)


async def test_fill_drain_refill_never_resends(rig, monkeypatch):
    from app import db, settings

    # Three 100-byte files fit; the cap is what stops the fourth.
    rig.cap(300)
    settings.save({"max_batch_files": 3})
    db.upsert_assets([asset(i, size=100) for i in range(9)])

    added = await cycle(monkeypatch)
    assert added == 3
    first = rig.files()
    assert len(first) == 3

    # Smart Storage clears them: absence is the confirmation.
    rig.deliver(3)
    added = await cycle(monkeypatch)
    assert db.counts()["confirmed"] == 3
    assert added == 3

    second = rig.files()
    assert len(second) == 3
    assert first & second == set(), "a confirmed file was sent a second time"

    rig.deliver(3)
    await cycle(monkeypatch)
    third = rig.files()
    assert (first | second) & third == set()
    assert db.counts()["confirmed"] == 6
    assert db.counts()["pending"] == 0


async def test_cap_is_never_exceeded(rig, monkeypatch):
    from app import db, settings

    # A 1 GB cap and ten 200 MB files: four fit, the fifth must not.
    settings.save({"outbox_max_gb": 1, "max_batch_files": 40})
    mb = 1024 * 1024
    db.upsert_assets([asset(i, size=200 * mb) for i in range(10)])

    from app import feeder, immich
    # Sparse payloads, so the test does not actually write a gigabyte.
    monkeypatch.setattr(immich, "stream_original",
                        fake_download(lambda _id, size: b"x" * size))
    _, used = feeder.reconcile()
    added = await feeder.top_up(used)

    cap = settings.load().outbox_max_bytes
    assert added == 5           # 5 x 200 MB = 1000 MB, under 1024 MB
    assert rig.used() <= cap


async def test_oversize_only_moves_when_the_outbox_is_empty(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"outbox_max_gb": 1})
    mb = 1024 * 1024
    # One file larger than the entire cap. It would otherwise jam the queue
    # forever, so it is allowed through alone.
    db.upsert_assets([asset(0, size=2000 * mb)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1
    assert len(rig.files()) == 1

    # With that sitting there, nothing else may be added.
    db.upsert_assets([asset(1, size=10)])
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0


async def test_claim_batch_does_not_reserve(rig):
    """Why the cycle lock is load-bearing.

    claim_batch is a plain read: two callers running at once select the same
    rows and size their batches against the same free space. Nothing in the
    ledger stops them, so the serialisation has to come from the lock.
    """
    from app import db, settings

    settings.save({"outbox_max_gb": 1})
    db.upsert_assets([asset(i, size=100) for i in range(4)])
    cfg = settings.load()
    filt = {"include_video": True, "max_asset_bytes": cfg.max_asset_bytes,
            "ongoing": True, "ongoing_from": "1970-01-01", "backfill": False,
            "backfill_start": "2015-01-01", "backfill_end": "2015-01-31"}

    a = db.claim_batch(cfg.outbox_max_bytes, 40, filt)
    b = db.claim_batch(cfg.outbox_max_bytes, 40, filt)
    assert [r["id"] for r in a] == [r["id"] for r in b]


async def test_a_failed_download_leaves_nothing_behind(rig, monkeypatch):
    from app import db, feeder, immich

    async def boom(asset_id):
        raise RuntimeError("HTTP 500 upstream exploded")

    db.upsert_assets([asset(0, size=100)])
    monkeypatch.setattr(immich, "stream_original", boom)

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert rig.files() == set()
    row = db.connect().execute("SELECT * FROM assets WHERE id='asset-0'").fetchone()
    assert row["state"] == "failed"
    assert row["attempts"] == 1
    assert "exploded" in row["last_error"]


async def test_a_truncated_download_is_rejected(rig, monkeypatch):
    """A short body must never be renamed into place and called a photo.

    This is the shape of the v3 redirect bug: a small HTML stub arriving
    where a photo was expected.
    """
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=500_000)])
    monkeypatch.setattr(immich, "stream_original",
                        fake_download(lambda _id, _size: b"<html>nope</html>"))

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert rig.files() == set()
    assert db.counts()["failed"] == 1


async def test_filename_collisions_get_a_suffix(rig, monkeypatch):
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=10, name="IMG_0001.jpg"),
                      asset(1, size=10, name="IMG_0001.jpg")])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 2
    assert rig.files() == {"IMG_0001.jpg", "IMG_0001 (2).jpg"}


async def test_legacy_prefixed_names_still_resolve(rig, monkeypatch):
    """Files from the old <asset-id>__name scheme must keep confirming.

    Their disappearance is the only record that they were backed up.
    """
    from app import db, feeder

    uuid = "01234567-89ab-cdef-0123-456789abcdef"   # 36 chars, as Immich
    assert len(uuid) == 36
    rows = [asset(0, size=10)]
    rows[0]["id"] = uuid
    db.upsert_assets(rows)
    db.mark_queued([uuid])

    (rig.outbox / f"{uuid}__IMG_0000.jpg").write_bytes(b"x" * 10)
    present, _ = feeder.reconcile()
    assert present == [uuid]

    (rig.outbox / f"{uuid}__IMG_0000.jpg").unlink()
    feeder.reconcile()
    assert db.counts()["confirmed"] == 1


async def test_the_cap_fills_the_outbox_not_the_batch_size(rig, monkeypatch):
    """max_batch_files bounds a claim, not a cycle.

    With a 16 GB outbox and 4 MB photos, a hard limit of forty files put
    160 MB in it and left the rest idle until the next cycle ten minutes
    later. The cap is the flow control; it decides when to stop.
    """
    from app import db, feeder, immich, settings

    rig.cap(1000)                       # ten 100-byte files
    settings.save({"max_batch_files": 3})
    db.upsert_assets([asset(i, size=100) for i in range(25)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    _, used = feeder.reconcile()
    added = await feeder.top_up(used)

    assert added == 10, "stopped at the batch size instead of filling the cap"
    assert rig.used() <= 1000
    assert len(rig.files()) == 10


async def test_filling_still_never_exceeds_the_cap(rig, monkeypatch):
    from app import db, feeder, immich, settings

    rig.cap(1000)
    settings.save({"max_batch_files": 4})
    db.upsert_assets([asset(i, size=300) for i in range(20)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3        # 3 x 300 = 900, a fourth would be 1200
    assert rig.used() <= 1000


async def test_pausing_stops_a_long_fill_between_files(rig, monkeypatch):
    """A fill can now run for a whole outbox, so pause must land inside it
    rather than at the end."""
    from app import db, feeder, immich, settings

    rig.cap(10_000)
    settings.save({"max_batch_files": 50})
    db.upsert_assets([asset(i, size=100) for i in range(40)])

    real = fake_download()
    seen = {"n": 0}

    async def pause_after_three(asset_id):
        seen["n"] += 1
        if seen["n"] == 3:
            settings.save({"paused": True})
        return await real(asset_id)

    monkeypatch.setattr(immich, "stream_original", pause_after_three)
    _, used = feeder.reconcile()
    added = await feeder.top_up(used)

    assert added == 3, f"pause was not honoured mid-fill (wrote {added})"
    assert db.counts()["queued"] == 3


async def test_a_batch_that_writes_nothing_does_not_spin(rig, monkeypatch):
    """If every claim fails, keep claiming and it would loop forever."""
    from app import db, feeder, immich, settings

    rig.cap(10_000)
    settings.save({"max_batch_files": 5})
    db.upsert_assets([asset(i, size=100) for i in range(30)])

    calls = {"n": 0}

    async def always_fails(asset_id):
        calls["n"] += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(immich, "stream_original", always_fails)
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert calls["n"] == 5, "kept claiming after a claim that wrote nothing"
