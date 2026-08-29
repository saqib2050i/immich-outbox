"""SQLite ledger.

The whole point of this service. Without it, an exporter re-sends whatever
is not currently on disk -- and since the outbox is meant to empty, that is
an infinite loop. The ledger remembers what has already made the trip.

State machine for every asset:

    pending    -> known to Immich, never sent
    queued     -> written to the outbox; Syncthing mirrors it to the Pixel
    confirmed  -> gone from the outbox. Only the phone deletes, and only
                  Smart Storage deletes there, and only for files Google
                  Photos has verified it holds. So absence is proof.
    failed     -> download kept erroring; retried up to 5 times
    skipped    -> deliberately excluded (oversized, or video when off)
""" 

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
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
CREATE INDEX IF NOT EXISTS idx_assets_state ON assets(state);
CREATE INDEX IF NOT EXISTS idx_assets_taken ON assets(taken_at);

-- Ids of motion-photo video components. They are never sent: the still
-- image already carries the embedded clip, so relaying the component too
-- puts a stray video in Google Photos next to the photo.
CREATE TABLE IF NOT EXISTS motion_parts (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL,
    msg  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def log(kind: str, msg: str) -> None:
    c = connect()
    with _lock:
        c.execute("INSERT INTO events (ts, kind, msg) VALUES (?,?,?)", (now(), kind, msg))
        c.execute(
            "DELETE FROM events WHERE id < (SELECT MAX(id) - 500 FROM events)"
        )
        c.commit()


def mark_motion_parts(ids: list[str]) -> int:
    """Record motion components and retire any already queued.

    The component can be scanned before the still that points at it, so this
    also demotes rows already sitting in the ledger. Anything already
    confirmed is left alone -- it is in Google Photos and rewriting history
    would not remove it.
    """
    if not ids:
        return 0
    c = connect()
    with _lock:
        c.executemany("INSERT OR IGNORE INTO motion_parts (id) VALUES (?)",
                      [(i,) for i in ids])
        cur = c.executemany(
            "UPDATE assets SET state='skipped' WHERE id=? AND state IN "
            "('pending','failed','queued')",
            [(i,) for i in ids],
        )
        c.commit()
    return cur.rowcount if cur else 0


def get_meta(k: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(k: str, v: str) -> None:
    c = connect()
    with _lock:
        c.execute("INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, v, v))
        c.commit()


def upsert_assets(rows: list[dict]) -> int:
    """Insert newly discovered Immich assets. Never touches existing rows,
    so a confirmed asset is never re-queued."""
    if not rows:
        return 0
    c = connect()
    with _lock:
        before = c.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
        c.executemany(
            """INSERT OR IGNORE INTO assets
               (id, filename, size, checksum, taken_at, kind, state, queued_at)
               VALUES (:id, :filename, :size, :checksum, :taken_at, :kind, :state, :queued_at)""",
            rows,
        )
        c.execute("""UPDATE assets SET state='skipped'
                     WHERE state='pending'
                       AND id IN (SELECT id FROM motion_parts)""")
        c.commit()
        after = c.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    return after - before


def claim_batch(budget_bytes: int, max_files: int, filt: dict,
                allow_oversize: bool = False) -> list[sqlite3.Row]:
    """Oldest-first selection of assets that are eligible and fit the budget.

    Eligibility is applied here rather than at scan time, so the ledger
    always holds the whole library and changing a date window releases
    assets on the next cycle instead of needing a rescan.

    allow_oversize is only true when the outbox is completely empty. It lets
    a single file larger than the whole cap through, so one huge video can
    still make progress instead of jamming the queue forever. At any other
    time the budget is respected strictly -- otherwise a 4 GB video would
    drop on top of an almost-full outbox and overflow the phone.
    """
    if not (filt["ongoing"] or filt["backfill"]):
        return []

    sql = """SELECT * FROM assets
             WHERE state IN ('pending','failed') AND attempts < 5
               AND id NOT IN (SELECT id FROM motion_parts)
               AND (kind = 'IMAGE' OR :include_video = 1)
               AND (size = 0 OR size <= :max_asset_bytes)
               AND (
                    (:ongoing = 1 AND substr(taken_at,1,10) >= :ongoing_from)
                 OR (:backfill = 1 AND substr(taken_at,1,10)
                       BETWEEN :backfill_start AND :backfill_end)
               )
             ORDER BY taken_at ASC LIMIT :scan_limit"""

    params = dict(filt)
    params["include_video"] = 1 if filt["include_video"] else 0
    params["ongoing"] = 1 if filt["ongoing"] else 0
    params["backfill"] = 1 if filt["backfill"] else 0
    params["scan_limit"] = max_files * 4

    rows = connect().execute(sql, params).fetchall()

    picked, total = [], 0
    for r in rows:
        if len(picked) >= max_files:
            break
        size = r["size"] or 0
        if total + size > budget_bytes:
            if picked or not allow_oversize:
                continue
        picked.append(r)
        total += size
    return picked


def window_progress(start: str, end: str) -> dict:
    """How far the current backfill month has got. This is the signal for
    whether it is safe to step the window forward."""
    rows = connect().execute(
        """SELECT state, COUNT(*) n FROM assets
           WHERE substr(taken_at,1,10) BETWEEN ? AND ? GROUP BY state""",
        (start, end),
    ).fetchall()
    out = {r["state"]: r["n"] for r in rows}
    total = sum(out.values())
    return {
        "total": total,
        "confirmed": out.get("confirmed", 0),
        "queued": out.get("queued", 0),
        "remaining": out.get("pending", 0) + out.get("failed", 0),
        "done": total > 0 and out.get("confirmed", 0) == total,
    }


def mark_queued(ids: list[str]) -> None:
    if not ids:
        return
    c = connect()
    with _lock:
        c.executemany(
            "UPDATE assets SET state='queued', sent_at=COALESCE(sent_at,?), seen_on_phone=1 WHERE id=?",
            [(now(), i) for i in ids],
        )
        c.commit()


def mark_present(ids: list[str]) -> None:
    """These files exist in the outbox right now.

    Motion components are excluded: one left over from before they were
    recognised would otherwise be promoted back to 'queued' every cycle and
    never stay retired.
    """
    if not ids:
        return
    c = connect()
    with _lock:
        c.executemany(
            "UPDATE assets SET seen_on_phone=1, state='queued', "
            "sent_at=COALESCE(sent_at,?) WHERE id=? "
            "AND id NOT IN (SELECT id FROM motion_parts)",
            [(now(), i) for i in ids],
        )
        c.commit()


def confirm_absent(present_ids: list[str]) -> int:
    """Anything written to the outbox that has since disappeared was deleted
    on the phone by Smart Storage -- which only removes copies Google Photos
    has confirmed it holds. That makes absence evidence, not a guess."""
    c = connect()
    with _lock:
        cur = c.execute(
            "SELECT id FROM assets WHERE state='queued' AND seen_on_phone=1"
        )
        gone = [r["id"] for r in cur.fetchall() if r["id"] not in set(present_ids)]
        if gone:
            c.executemany(
                "UPDATE assets SET state='confirmed', confirmed_at=? WHERE id=?",
                [(now(), i) for i in gone],
            )
            c.commit()
    return len(gone)


def is_motion_part(asset_id: str) -> bool:
    return connect().execute(
        "SELECT 1 FROM motion_parts WHERE id=?", (asset_id,)
    ).fetchone() is not None


def mark_failed(asset_id: str, error: str) -> None:
    c = connect()
    with _lock:
        c.execute(
            "UPDATE assets SET state='failed', attempts=attempts+1, last_error=? WHERE id=?",
            (error[:300], asset_id),
        )
        c.commit()


def requeue(asset_id: str) -> None:
    c = connect()
    with _lock:
        c.execute(
            """UPDATE assets SET state='pending', seen_on_phone=0, attempts=0,
               sent_at=NULL, confirmed_at=NULL, last_error=NULL WHERE id=?""",
            (asset_id,),
        )
        c.commit()


def counts() -> dict:
    c = connect()
    out = {r["state"]: r["n"] for r in c.execute(
        "SELECT state, COUNT(*) n FROM assets GROUP BY state")}
    for s in ("pending", "queued", "confirmed", "failed", "skipped"):
        out.setdefault(s, 0)
    out["outbox_bytes"] = c.execute(
        "SELECT COALESCE(SUM(size),0) b FROM assets WHERE state='queued'").fetchone()["b"]
    out["pending_bytes"] = c.execute(
        "SELECT COALESCE(SUM(size),0) b FROM assets WHERE state IN ('pending','failed')"
    ).fetchone()["b"]
    out["confirmed_bytes"] = c.execute(
        "SELECT COALESCE(SUM(size),0) b FROM assets WHERE state='confirmed'").fetchone()["b"]
    return out


def oldest_sent_at() -> str | None:
    row = connect().execute(
        "SELECT MIN(sent_at) t FROM assets WHERE state='queued'"
    ).fetchone()
    return row["t"] if row and row["t"] else None


def stuck(days: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return connect().execute(
        "SELECT * FROM assets WHERE state='queued' AND sent_at < ? ORDER BY sent_at ASC LIMIT 50",
        (cutoff,),
    ).fetchall()


def problems() -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM assets WHERE state='failed' ORDER BY sent_at DESC LIMIT 50"
    ).fetchall()


def recent_events(n: int = 25) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
