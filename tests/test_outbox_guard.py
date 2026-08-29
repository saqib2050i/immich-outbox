"""The outbox has to be really there before absence means anything.

Confirmation is derived from files disappearing. An outbox that vanishes --
a bind mount that did not come up, a wrong host path, an empty directory
created in its place -- looks exactly like Google Photos having verified
everything, and confirmed assets are never re-sent.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def test_marker_is_planted_on_a_fresh_outbox(rig):
    from app import feeder

    ok, why = feeder.outbox_ready()
    assert ok and why == ""
    assert (rig.outbox / feeder.MOUNT_MARKER).exists()
    # It is a dotfile, so it is never counted as a delivered photo.
    assert rig.files() == set()


async def test_a_vanished_outbox_confirms_nothing(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"max_batch_files": 3})
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3
    assert db.counts()["queued"] == 3

    # The mount goes away and something recreates the path, empty.
    for child in rig.outbox.iterdir():
        child.unlink()

    present, used = feeder.reconcile()
    assert present == [] and used == 0
    assert db.counts()["confirmed"] == 0, "an empty outbox was read as a backup"
    assert db.counts()["queued"] == 3
    assert "refusing" in db.get_meta("outbox_problem")


async def test_a_vanished_outbox_stops_new_downloads(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"max_batch_files": 1})
    db.upsert_assets([asset(i, size=100) for i in range(3)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    for child in rig.outbox.iterdir():
        child.unlink()

    # Nothing may be written into an outbox we cannot vouch for: the bytes
    # would go nowhere and the ledger would call them sent.
    assert await feeder.top_up(0) == 0


async def test_the_guard_lifts_when_the_mount_returns(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"max_batch_files": 2})
    db.upsert_assets([asset(i, size=100) for i in range(4)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    delivered = sorted(rig.files())
    marker = (rig.outbox / feeder.MOUNT_MARKER).read_bytes()

    # Mount lost.
    for child in rig.outbox.iterdir():
        child.unlink()
    feeder.reconcile()
    assert db.get_meta("outbox_problem")

    # Mount back, with the files and the marker exactly as they were.
    (rig.outbox / feeder.MOUNT_MARKER).write_bytes(marker)
    for name in delivered:
        (rig.outbox / name).write_bytes(b"x" * 100)

    present, used = feeder.reconcile()
    assert len(present) == 2
    assert db.get_meta("outbox_problem") == ""
    assert db.counts()["confirmed"] == 0

    # And normal confirmation still works from there.
    rig.deliver(2)
    feeder.reconcile()
    assert db.counts()["confirmed"] == 2


async def test_a_missing_directory_is_reported_not_created(rig):
    from app import config, db, feeder

    db.upsert_assets([asset(0, size=100)])
    db.mark_queued(["asset-0"])
    config.OUTBOX_DIR = str(rig.root / "never-mounted")

    ok, why = feeder.outbox_ready()
    assert not ok
    assert "refusing" in why
    # Crucially it did not helpfully create the directory and carry on.
    assert not (rig.root / "never-mounted").exists()


async def test_emptying_the_outbox_keeps_the_marker(rig, monkeypatch):
    """The reset button must not delete the thing that proves the mount.

    Removing it would make the very next cycle think the outbox had gone.
    """
    from app import db, feeder, immich

    db.upsert_assets([asset(i, size=100) for i in range(2)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    removed, ids = feeder.empty_outbox()
    assert removed == 2
    assert len(ids) == 2
    assert (rig.outbox / feeder.MOUNT_MARKER).exists()
    assert feeder.outbox_ready()[0]
