"""Opening a database written by an older version.

The ledger is never recreated from scratch -- losing it means re-uploading
the library as duplicates -- so every release has to open the previous
one's file. A startup failure here is a service that will not boot at all.
"""

import os
import sqlite3

import pytest

pytestmark = pytest.mark.asyncio

# The assets table as it was before width/height/duration/forced/outbox_name
# were added. Anything created by that release still looks like this.
ORIGINAL_SCHEMA = """
CREATE TABLE assets (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    checksum      TEXT,
    taken_at      TEXT,
    kind          TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',
    seen_on_phone INTEGER NOT NULL DEFAULT 0,
    queued_at     TEXT,
    sent_at       TEXT,
    confirmed_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
CREATE TABLE motion_parts (id TEXT PRIMARY KEY);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL,
    msg  TEXT NOT NULL
);
"""


def write_old_database(path, rows=3):
    # The rig has already opened a current-schema database here; replace it
    # wholesale with one written the way the old release wrote it.
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = sqlite3.connect(path)
    conn.executescript(ORIGINAL_SCHEMA)
    for i in range(rows):
        conn.execute(
            "INSERT INTO assets (id, filename, size, taken_at, kind, state,"
            " seen_on_phone, sent_at, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"old-{i}", f"IMG_{i}.jpg", 1000, "2020-05-05", "IMAGE",
             "confirmed", 1, "2020-05-05T00:00:00+00:00",
             "2020-06-05T00:00:00+00:00"))
    conn.execute("INSERT INTO meta (k, v) VALUES ('cfg_immich_url', 'http://old')")
    conn.commit()
    conn.close()


async def test_an_old_database_opens(rig):
    """The regression: startup died with 'no such column: outbox_name'.

    CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists, so
    the index on outbox_name was being built before the ALTER TABLE that
    adds the column had run.
    """
    from app import config, db

    db.close()
    write_old_database(config.DB_PATH)

    conn = db.connect()          # this used to raise OperationalError

    have = {r["name"] for r in conn.execute("PRAGMA table_info(assets)")}
    for col, _ in db.MIGRATIONS:
        assert col in have, f"{col} was not migrated in"


async def test_the_indexes_exist_after_migrating(rig):
    from app import config, db

    db.close()
    write_old_database(config.DB_PATH)
    conn = db.connect()

    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_assets_state", "idx_assets_taken",
            "idx_assets_outbox"} <= names


async def test_the_old_ledger_survives_intact(rig):
    """A migration that dropped history would re-send the library."""
    from app import config, db

    db.close()
    write_old_database(config.DB_PATH, rows=3)
    db.connect()

    assert db.counts()["confirmed"] == 3
    assert db.get_meta("cfg_immich_url") == "http://old"
    # The columns that did not exist are simply empty, not defaulted to
    # something that would change behaviour.
    row = db.connect().execute("SELECT * FROM assets WHERE id='old-0'").fetchone()
    assert row["outbox_name"] is None
    assert row["forced"] == 0


async def test_migrated_rows_still_confirm_by_absence(rig, monkeypatch):
    """An old row has no outbox_name, so it resolves by the legacy id
    prefix. That path has to keep working or its file's disappearance is
    never read as a backup."""
    from app import config, db, feeder

    db.close()
    write_old_database(config.DB_PATH, rows=0)
    db.connect()

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    db.connect().execute(
        "INSERT INTO assets (id, filename, size, taken_at, kind, state,"
        " seen_on_phone, sent_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid, "IMG_9.jpg", 10, "2020-05-05", "IMAGE", "queued", 1,
         "2020-05-05T00:00:00+00:00"))
    db.connect().commit()

    (rig.outbox / f"{uuid}__IMG_9.jpg").write_bytes(b"x" * 10)
    present, _ = feeder.reconcile()
    assert present == [uuid]

    (rig.outbox / f"{uuid}__IMG_9.jpg").unlink()
    feeder.reconcile()
    assert db.counts()["confirmed"] == 1


async def test_opening_a_current_database_is_unchanged(rig):
    from app import db

    conn = db.connect()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(assets)")}
    for col, _ in db.MIGRATIONS:
        assert col in have
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_assets_outbox" in names
