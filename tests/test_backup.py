"""bridge.db is the only thing standing between you and re-uploading the
whole library as duplicates."""

import os

import pytest

from conftest import asset

pytestmark = pytest.mark.asyncio


async def test_backup_change_restore_round_trip(rig):
    from app import backup, db

    db.upsert_assets([asset(i) for i in range(5)])
    db.mark_queued(["asset-0", "asset-1"])
    db.confirm_absent([])
    before = db.counts()
    assert before["confirmed"] == 2

    snap = backup.create()
    assert snap["bytes"] > 0

    # Lose the ledger the worst possible way: everything back to pending.
    db.reset_states()
    assert db.counts()["confirmed"] == 0
    assert db.counts()["pending"] == 5

    backup.restore(snap["name"])
    after = db.counts()
    assert after["confirmed"] == 2
    assert after["pending"] == before["pending"]


async def test_a_restore_is_itself_undoable(rig):
    from app import backup, db

    db.upsert_assets([asset(0)])
    db.mark_queued(["asset-0"])
    db.confirm_absent([])
    old = backup.create()

    db.upsert_assets([asset(i) for i in range(1, 4)])
    assert db.counts()["pending"] == 3

    result = backup.restore(old["name"])
    assert db.counts()["pending"] == 0          # we went back in time

    # The safety copy taken during the restore holds the state we left.
    assert result["safety_copy"] != old["name"], \
        "the safety copy overwrote the backup being restored from"
    backup.restore(result["safety_copy"])
    assert db.counts()["pending"] == 3


async def test_two_backups_in_the_same_second_do_not_collide(rig):
    """Second resolution meant the safety copy taken during a restore
    overwrote the backup being restored from."""
    from app import backup, db

    db.upsert_assets([asset(0)])
    names = {backup.create()["name"] for _ in range(5)}
    assert len(names) == 5


async def test_restore_removes_the_wal_sidecars(rig):
    """Left behind, they replay over the restored data and undo it."""
    from app import backup, config, db

    db.upsert_assets([asset(0)])
    snap = backup.create()
    db.upsert_assets([asset(1)])
    assert os.path.exists(config.DB_PATH + "-wal")

    backup.restore(snap["name"])
    assert db.counts()["pending"] == 1


async def test_a_restore_signs_everyone_out(rig):
    from app import auth, backup, db

    auth.set_password("a-good-password")
    snap = backup.create()
    token = auth.new_session()
    assert auth.valid_session(token)

    backup.restore(snap["name"])
    assert not auth.valid_session(token), \
        "a session survived the password hash being replaced"


async def test_downloads_have_the_credentials_stripped(rig):
    """The ledger holds the Immich and Syncthing keys in plaintext, so
    handing the raw file to a browser hands over the keys."""
    import sqlite3
    from app import backup, db, settings

    settings.save({"immich_api_key": "immich-secret-key",
                   "syncthing_api_key": "syncthing-secret-key",
                   "alert_webhook_url": "https://ntfy.example/secret-topic"})
    db.upsert_assets([asset(0)])
    snap = backup.create()

    export = backup.export_sanitised(snap["name"])
    try:
        conn = sqlite3.connect(export)
        meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        conn.close()

        assert assets == 1, "the ledger itself must survive the sanitising"
        for key in ("cfg_immich_api_key", "cfg_syncthing_api_key",
                    "cfg_alert_webhook_url", "auth_hash"):
            assert key not in meta, key
        blob = open(export, "rb").read()
        for secret in (b"immich-secret-key", b"syncthing-secret-key",
                       b"secret-topic"):
            assert secret not in blob, secret
    finally:
        os.remove(export)

    # The on-disk backup keeps everything, or a restore would not work.
    conn = sqlite3.connect(backup.path_for(snap["name"]))
    assert dict(conn.execute("SELECT k, v FROM meta").fetchall())[
        "cfg_immich_api_key"] == "immich-secret-key"
    conn.close()


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "bridge-/../../etc/passwd", "passwd",
    "bridge-..\\..\\windows", "",
])
async def test_backup_names_cannot_escape_the_directory(rig, name):
    from app import backup
    with pytest.raises((ValueError, FileNotFoundError)):
        backup.path_for(name)
    with pytest.raises((ValueError, FileNotFoundError)):
        backup.restore(name)


async def test_pruning_keeps_the_newest(rig, monkeypatch):
    from app import backup, db

    monkeypatch.setattr(backup, "KEEP", 3)
    db.upsert_assets([asset(0)])
    made = [backup.create()["name"] for _ in range(6)]
    kept = [b["name"] for b in backup.list_backups()]
    assert len(kept) == 3
    assert set(kept) == set(sorted(made)[-3:])


async def test_a_scheduled_backup_is_due_once_a_day(rig):
    from app import backup, db

    assert backup.due() is True             # never taken
    backup.create()
    assert backup.due() is False
    db.set_meta("last_backup", "2020-01-01T00:00:00+00:00")
    assert backup.due() is True
