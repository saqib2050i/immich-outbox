"""Ledger backups.

`bridge.db` is the only thing standing between you and re-uploading the
whole library as duplicates: lose it and every asset goes back to `pending`,
Google Photos gains a second copy of everything, and the storage bill you
were trying to kill comes back.

Uses SQLite's own backup API rather than copying the file, so a snapshot
taken mid-write is still consistent. WAL mode makes a plain `cp` unsafe.
"""

import os
import shutil
import sqlite3
from datetime import datetime, timezone

from . import config, db

BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
KEEP = int(os.getenv("BACKUP_KEEP", "14"))


def _stamp() -> str:
    # Millisecond resolution, because restore() takes a safety copy first and
    # a second-resolution name would collide with — and overwrite — the very
    # backup being restored from.
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def list_backups() -> list[dict]:
    try:
        names = sorted(os.listdir(BACKUP_DIR), reverse=True)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.startswith("bridge-") or not name.endswith(".db"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "name": name,
            "bytes": st.st_size,
            "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        })
    return out


def create() -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"bridge-{_stamp()}.db")
    # Belt and braces: never clobber an existing backup.
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(BACKUP_DIR, f"bridge-{_stamp()}-{n}.db")
        n += 1

    target = sqlite3.connect(dest)
    try:
        db.connect().backup(target)
    finally:
        target.close()

    prune()
    size = os.path.getsize(dest)
    db.log("backup", f"ledger backed up ({size // 1024} KB)")
    db.set_meta("last_backup", db.now())
    return {"name": os.path.basename(dest), "bytes": size}


def prune() -> int:
    backups = list_backups()
    removed = 0
    for old in backups[KEEP:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old["name"]))
            removed += 1
        except OSError:
            continue
    return removed


def restore(name: str) -> dict:
    """Replace the live ledger with a backup.

    The current ledger is snapshotted first, so a restore chosen by mistake
    is itself undoable.
    """
    if "/" in name or "\\" in name or not name.startswith("bridge-"):
        raise ValueError("bad backup name")
    src = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(src):
        raise FileNotFoundError(name)

    safety = create()

    # Copying into the live connection with SQLite's backup API silently does
    # nothing, so close the connection and swap the file. The -wal and -shm
    # sidecars must go too, or their contents replay over the restored data
    # and undo the restore.
    with db._lock:  # noqa: SLF001 — must not race a write
        if db._conn is not None:  # noqa: SLF001
            db._conn.close()      # noqa: SLF001
            db._conn = None       # noqa: SLF001
        for suffix in ("", "-wal", "-shm"):
            stale = config.DB_PATH + suffix
            if os.path.exists(stale):
                os.remove(stale)
        shutil.copyfile(src, config.DB_PATH)

    db.connect()
    db._bump()  # noqa: SLF001
    db.log("backup", f"ledger restored from {name}")
    return {"restored": name, "safety_copy": safety["name"]}


def due(interval_hours: int = 24) -> bool:
    last = db.get_meta("last_backup")
    if not last:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    return age > interval_hours * 3600


def path_for(name: str) -> str:
    if "/" in name or "\\" in name or not name.startswith("bridge-"):
        raise ValueError("bad backup name")
    return os.path.join(BACKUP_DIR, name)


def export_sanitised(name: str) -> str:
    """A copy of a backup with the credentials stripped out.

    The ledger holds the Immich and Syncthing keys in plaintext, so handing
    the raw file to a browser hands over the keys. Downloads get this copy
    instead; the on-disk backup keeps everything so a restore still works.
    """
    src = path_for(name)
    if not os.path.isfile(src):
        raise FileNotFoundError(name)

    tmp = os.path.join(BACKUP_DIR, ".export-" + name)
    shutil.copyfile(src, tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("DELETE FROM meta WHERE k IN "
                     "('cfg_immich_api_key','cfg_syncthing_api_key',"
                     " 'auth_hash','alert_state')")
        conn.execute("DELETE FROM meta WHERE k LIKE 'cfg_alert_webhook%'")
        conn.commit()
        conn.execute("VACUUM")          # do not leave the values in free pages
        conn.commit()
    finally:
        conn.close()
    return tmp
