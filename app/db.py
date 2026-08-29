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

# Incremented by every write. The event stream watches this, so the browser
# sees a change within a fraction of a second instead of on a poll timer.
_revision = 0


def revision() -> int:
    return _revision


def _bump() -> None:
    global _revision
    _revision += 1

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
    last_error    TEXT,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    forced        INTEGER NOT NULL DEFAULT 0
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
        # Existing databases predate the media columns. CREATE TABLE IF NOT
        # EXISTS will not add them, so patch them in.
        have = {r["name"] for r in _conn.execute("PRAGMA table_info(assets)")}
        for col, decl in (("width", "INTEGER"), ("height", "INTEGER"),
                          ("duration", "REAL"),
                          ("forced", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                _conn.execute(f"ALTER TABLE assets ADD COLUMN {col} {decl}")
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
        _bump()


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
        _bump()
    return cur.rowcount if cur else 0


def get_meta(k: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(k: str, v: str) -> None:
    c = connect()
    with _lock:
        c.execute("INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, v, v))
        c.commit()
        _bump()


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
               (id, filename, size, checksum, taken_at, kind, state, queued_at,
                width, height, duration)
               VALUES (:id, :filename, :size, :checksum, :taken_at, :kind, :state,
                       :queued_at, :width, :height, :duration)""",
            rows,
        )
        # Rows written by an older version carry no dimensions. Backfill them
        # without touching state, so statistics complete after one scan
        # instead of needing a reset.
        c.executemany(
            """UPDATE assets SET width=:width, height=:height, duration=:duration
               WHERE id=:id AND width IS NULL AND :width IS NOT NULL""",
            rows,
        )
        c.execute("""UPDATE assets SET state='skipped'
                     WHERE state='pending'
                       AND id IN (SELECT id FROM motion_parts)""")
        c.commit()
        _bump()
        after = c.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    return after - before


def claim_batch(budget_bytes: int, max_files: int, filt: dict,
                allow_oversize: bool = False) -> list[sqlite3.Row]:
    """Oldest-first selection of eligible assets that fit the budget.

    Eligibility is applied here rather than at scan time, so the ledger
    always holds the whole library and changing a date window releases
    assets on the next cycle instead of needing a rescan.

    Assets marked `forced` — chosen by hand or by resolution category —
    ignore the date windows entirely and go to the front of the queue.

    allow_oversize is only true when the outbox is empty. It lets a single
    file larger than the whole cap through, so one huge video can still make
    progress instead of jamming the queue forever. Otherwise the budget is
    strict: a 4 GB video must not drop onto an almost-full outbox.
    """
    sql = """SELECT * FROM assets
             WHERE state IN ('pending','failed') AND attempts < 5
               AND id NOT IN (SELECT id FROM motion_parts)
               AND (kind = 'IMAGE' OR :include_video = 1)
               AND (size = 0 OR size <= :max_asset_bytes)
               AND (
                    forced = 1
                 OR (:ongoing = 1 AND substr(taken_at,1,10) >= :ongoing_from)
                 OR (:backfill = 1 AND substr(taken_at,1,10)
                       BETWEEN :backfill_start AND :backfill_end)
               )
             ORDER BY forced DESC, taken_at ASC LIMIT :scan_limit"""

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
        _bump()


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
        _bump()


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
                "UPDATE assets SET state='confirmed', confirmed_at=?, forced=0 WHERE id=?",
                [(now(), i) for i in gone],
            )
            # The stall alert keys off this: "queued things exist but nothing
            # has come back" is the signature of a silent failure.
            c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('last_confirm_at', ?)",
                      (now(),))
            c.commit()
            _bump()
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
        _bump()


def requeue(asset_id: str) -> None:
    c = connect()
    with _lock:
        c.execute(
            """UPDATE assets SET state='pending', seen_on_phone=0, attempts=0,
               sent_at=NULL, confirmed_at=NULL, last_error=NULL WHERE id=?""",
            (asset_id,),
        )
        c.commit()
        _bump()


def retry_failed() -> int:
    """Put every failed asset back in the queue with a fresh attempt count."""
    c = connect()
    with _lock:
        cur = c.execute(
            "UPDATE assets SET state='pending', attempts=0, last_error=NULL "
            "WHERE state='failed'")
        c.commit()
        _bump()
    return cur.rowcount


def requeue_many(ids: list[str]) -> int:
    """Send these again. Used when outbox files are cleared deliberately, so
    their disappearance is not mistaken for a Google Photos confirmation."""
    if not ids:
        return 0
    c = connect()
    with _lock:
        cur = c.executemany(
            "UPDATE assets SET state='pending', seen_on_phone=0, attempts=0, "
            "sent_at=NULL, confirmed_at=NULL, last_error=NULL WHERE id=? "
            "AND id NOT IN (SELECT id FROM motion_parts)",
            [(i,) for i in ids])
        c.commit()
        _bump()
    return cur.rowcount if cur else 0


def reset(ledger: bool = False, motion: bool = False,
          events: bool = False, settings_too: bool = False) -> dict:
    """Wipe selected state. Settings live in the same database as the ledger,
    so they are cleared separately and only when explicitly asked for."""
    done = {}
    c = connect()
    with _lock:
        if ledger:
            done["assets"] = c.execute("DELETE FROM assets").rowcount
        if motion:
            done["motion_parts"] = c.execute("DELETE FROM motion_parts").rowcount
        if events:
            done["events"] = c.execute("DELETE FROM events").rowcount
        if settings_too:
            done["settings"] = c.execute(
                "DELETE FROM meta WHERE k LIKE 'cfg_%'").rowcount
        # Scan cursors must go with the ledger or nothing is re-discovered
        # until the next full scan comes round.
        if ledger:
            c.execute("DELETE FROM meta WHERE k IN "
                      "('last_full_scan','last_incremental_scan')")
        c.commit()
        _bump()
    return done


def reset_states() -> int:
    """Put every asset back to pending so the whole library is sent again.

    Motion components stay skipped -- that they are components is a fact
    about the library, not test state, so re-discovering it every time would
    just re-send stray clips.
    """
    c = connect()
    with _lock:
        cur = c.execute("""UPDATE assets
                           SET state='pending', seen_on_phone=0, attempts=0,
                               sent_at=NULL, confirmed_at=NULL, last_error=NULL
                           WHERE id NOT IN (SELECT id FROM motion_parts)""")
        c.commit()
        _bump()
    return cur.rowcount


def reset_ids(ids: list[str]) -> int:
    """Put specific assets back to pending."""
    if not ids:
        return 0
    c = connect()
    with _lock:
        c.executemany(
            """UPDATE assets SET state='pending', seen_on_phone=0, attempts=0,
               sent_at=NULL, confirmed_at=NULL, last_error=NULL
               WHERE id=? AND id NOT IN (SELECT id FROM motion_parts)""",
            [(i,) for i in ids],
        )
        c.commit()
        _bump()
    return len(ids)


def wipe_ledger(forget_motion_parts: bool = False) -> int:
    """Delete the ledger entirely. Settings in `meta` are untouched."""
    c = connect()
    with _lock:
        n = c.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
        c.execute("DELETE FROM assets")
        c.execute("DELETE FROM events")
        if forget_motion_parts:
            c.execute("DELETE FROM motion_parts")
        # Scan timestamps must go too, or the next cycle thinks it is current.
        c.execute("DELETE FROM meta WHERE k IN "
                  "('last_full_scan','last_incremental_scan','outbox_files','outbox_used')")
        c.commit()
        _bump()
    return n


# The bucket rule lives here once, so the breakdown, the file list and
# "send this category" can never disagree about what a bucket contains.
BUCKET_SQL = """
    CASE
      WHEN width IS NULL OR width = 0 OR height IS NULL OR height = 0
           THEN 'unknown'
      WHEN kind = 'VIDEO' AND MIN(width, height) >= 2160 THEN 'video_4k'
      WHEN kind = 'VIDEO' AND MIN(width, height) >= 1440 THEN 'video_1440'
      WHEN kind = 'VIDEO' AND MIN(width, height) >= 1080 THEN 'video_1080'
      WHEN kind = 'VIDEO'                                THEN 'video_sd'
      WHEN (width * height) / 1000000.0 > 16.0           THEN 'photo_big'
      ELSE 'photo_small'
    END
"""


def list_in_bucket(bucket: str, state: str = "all",
                   limit: int = 100, offset: int = 0) -> dict:
    """Files in one resolution bucket, largest first."""
    where = [f"{BUCKET_SQL} = ?", "state != 'skipped'"]
    params: list = [bucket]
    if state != "all":
        where.append("state = ?")
        params.append(state)
    clause = " AND ".join(where)

    total = connect().execute(
        f"SELECT COUNT(*) n FROM assets WHERE {clause}", params
    ).fetchone()["n"]

    rows = connect().execute(
        f"""SELECT id, filename, size, kind, state, taken_at, width, height,
                   duration, forced, last_error
            FROM assets WHERE {clause}
            ORDER BY size DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows],
            "offset": offset, "limit": limit}


def force_send(ids: list[str] | None = None, bucket: str | None = None) -> int:
    """Queue assets now, ignoring the date windows.

    Already-confirmed items are left alone: they are in Google Photos, and
    re-sending would just create a duplicate.
    """
    c = connect()
    with _lock:
        if bucket:
            cur = c.execute(
                f"""UPDATE assets SET forced=1, state='pending', attempts=0,
                                      last_error=NULL
                    WHERE {BUCKET_SQL} = ?
                      AND state IN ('pending','failed')
                      AND id NOT IN (SELECT id FROM motion_parts)""",
                (bucket,),
            )
        elif ids:
            marks = ",".join("?" * len(ids))
            cur = c.execute(
                f"""UPDATE assets SET forced=1, state='pending', attempts=0,
                                      last_error=NULL
                    WHERE id IN ({marks})
                      AND state IN ('pending','failed')
                      AND id NOT IN (SELECT id FROM motion_parts)""",
                ids,
            )
        else:
            return 0
        c.commit()
        _bump()
    return cur.rowcount


def media_breakdown() -> list[dict]:
    """Assets grouped by what they are and how much resolution they carry.

    The buckets are chosen around what Storage Saver would have cost you:
    it caps photos at 16 MP and video at 1080p, so anything above those
    lines is what this relay actually buys.

    Short side decides the video bucket, so portrait clips land correctly.
    """
    rows = connect().execute(f"""
        SELECT {BUCKET_SQL}                                          AS bucket,
               kind,
               COUNT(*)                                              AS total,
               COALESCE(SUM(size), 0)                                AS bytes,
               SUM(CASE WHEN state = 'confirmed' THEN 1 ELSE 0 END)  AS confirmed,
               COALESCE(SUM(CASE WHEN state = 'confirmed' THEN size ELSE 0 END), 0)
                                                                     AS confirmed_bytes,
               SUM(CASE WHEN state IN ('pending','failed') THEN 1 ELSE 0 END)
                                                                     AS remaining,
               COALESCE(SUM(duration), 0)                            AS seconds
        FROM assets
        WHERE state != 'skipped'
        GROUP BY bucket, kind
        ORDER BY bytes DESC
    """).fetchall()
    return [dict(r) for r in rows]


# Whether an item beats what Storage Saver would have done to it. Photos are
# capped at 16 MP and video at 1080p, so anything above those lines is what
# this relay actually buys you.
GAIN_SQL_A = """
    CASE
      WHEN a.width IS NULL OR a.width = 0 OR a.height IS NULL OR a.height = 0 THEN 0
      WHEN a.kind = 'VIDEO' THEN CASE WHEN MIN(a.width, a.height) > 1080 THEN 1 ELSE 0 END
      ELSE CASE WHEN (a.width * a.height) / 1000000.0 > 16.0 THEN 1 ELSE 0 END
    END
"""

GAIN_SQL = """
    CASE
      WHEN width IS NULL OR width = 0 OR height IS NULL OR height = 0 THEN 0
      WHEN kind = 'VIDEO' THEN CASE WHEN MIN(width, height) > 1080 THEN 1 ELSE 0 END
      ELSE CASE WHEN (width * height) / 1000000.0 > 16.0 THEN 1 ELSE 0 END
    END
"""


def month_detail(month: str) -> dict:
    """One month split into photos and videos, each by whether original
    quality actually gains anything over what Google already holds."""
    rows = connect().execute(f"""
        SELECT kind,
               {GAIN_SQL}                                            AS gains,
               COUNT(*)                                              AS total,
               COALESCE(SUM(size), 0)                                AS bytes,
               SUM(CASE WHEN state = 'confirmed' THEN 1 ELSE 0 END)  AS confirmed,
               SUM(CASE WHEN state = 'queued'    THEN 1 ELSE 0 END)  AS queued,
               SUM(CASE WHEN state IN ('pending','failed') THEN 1 ELSE 0 END)
                                                                     AS remaining,
               MIN(CASE WHEN width>0 AND height>0 THEN MIN(width,height) END) AS min_side,
               MAX(CASE WHEN width>0 AND height>0 THEN MAX(width,height) END) AS max_side
        FROM assets
        WHERE state != 'skipped' AND substr(taken_at, 1, 7) = ?
        GROUP BY kind, gains
    """, (month,)).fetchall()

    groups = []
    for r in rows:
        d = dict(r)
        video = d["kind"] == "VIDEO"
        if not d["min_side"]:
            label, why = ("Videos" if video else "Photos") + " — not yet measured", \
                         "dimensions arrive on the next full scan"
        elif video:
            label = "Video above 1080p" if d["gains"] else "Video at 1080p or below"
            why = ("original quality is kept — Storage Saver would cap these at 1080p"
                   if d["gains"] else "no gain — already at or under the cap")
        else:
            label = "Photos over 16 MP" if d["gains"] else "Photos 16 MP or less"
            why = ("original quality is kept — Storage Saver would resize these"
                   if d["gains"] else "no gain — already at or under the cap")
        d["label"], d["why"] = label, why
        d["group"] = ("video" if video else "photo") + ("_gain" if d["gains"] else "_nogain")
        groups.append(d)

    groups.sort(key=lambda g: (g["kind"] != "IMAGE", -g["gains"]))
    return {"month": month, "groups": groups}


def force_send_month(month: str, group: str | None = None) -> int:
    """Queue a month, or one of its four categories, ahead of everything else."""
    where = ["substr(taken_at,1,7) = ?", "state IN ('pending','failed')",
             "id NOT IN (SELECT id FROM motion_parts)"]
    params: list = [month]
    if group:
        kind = "IMAGE" if group.startswith("photo") else "VIDEO"
        where.append("kind = ?")
        params.append(kind)
        where.append(f"{GAIN_SQL} = ?")
        params.append(1 if group.endswith("_gain") else 0)

    c = connect()
    with _lock:
        cur = c.execute(
            f"""UPDATE assets SET forced=1, state='pending', attempts=0,
                                  last_error=NULL
                WHERE {' AND '.join(where)}""",
            params,
        )
        c.commit()
        _bump()
    return cur.rowcount


def reconciliation() -> list[dict]:
    """Everything not yet in Google Photos, grouped by why.

    With a curated library this should trend to empty; anything lingering is
    a real gap in the backup rather than something you chose to leave out.
    """
    from . import config, settings
    cfg = settings.load()
    rows = connect().execute(f"""
        SELECT
          CASE
            WHEN state = 'failed'  THEN 'failed'
            WHEN state = 'skipped' THEN 'skipped'
            WHEN state = 'queued'  THEN 'in_outbox'
            WHEN kind = 'VIDEO' AND :include_video = 0 THEN 'video_off'
            WHEN size > :max_bytes THEN 'too_big'
            WHEN (:ongoing = 1 AND substr(taken_at,1,10) >= :ongoing_from)
              OR (:backfill = 1 AND substr(taken_at,1,10)
                    BETWEEN :bstart AND :bend)
              OR forced = 1                             THEN 'queued_soon'
            ELSE 'outside_window'
          END AS reason,
          COUNT(*) AS total,
          COALESCE(SUM(size), 0) AS bytes
        FROM assets
        WHERE state != 'confirmed'
        GROUP BY reason
        ORDER BY total DESC
    """, {
        "include_video": 1 if cfg.include_video else 0,
        "max_bytes": cfg.max_asset_bytes,
        "ongoing": 1 if cfg.ongoing_enabled else 0,
        "ongoing_from": cfg.ongoing_from,
        "backfill": 1 if cfg.backfill_enabled else 0,
        "bstart": cfg.backfill_start,
        "bend": cfg.backfill_end,
    }).fetchall()
    return [dict(r) for r in rows]


def timeline() -> list[dict]:
    """Every month that actually holds media, newest first.

    Empty months are absent by construction -- the GROUP BY only produces
    months with rows -- so the UI never shows a year of nothing.
    """
    rows = connect().execute(f"""
        SELECT substr(a.taken_at, 1, 7)                               AS month,
               COUNT(*)                                               AS total,
               COALESCE(SUM(a.size), 0)                               AS bytes,
               SUM(CASE WHEN a.kind = 'IMAGE' THEN 1 ELSE 0 END)      AS photos,
               SUM(CASE WHEN a.kind = 'VIDEO' THEN 1 ELSE 0 END)      AS videos,
               SUM(CASE WHEN a.state = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
               SUM(CASE WHEN a.state = 'queued' THEN 1 ELSE 0 END)    AS queued,
               SUM(CASE WHEN a.state IN ('pending','failed') THEN 1 ELSE 0 END)
                                                                      AS remaining,
               COALESCE(SUM(CASE WHEN a.state IN ('pending','failed')
                                 THEN a.size ELSE 0 END), 0)          AS remaining_bytes,
               SUM(CASE WHEN {GAIN_SQL_A} = 1 THEN 1 ELSE 0 END)        AS gains,
               COALESCE(SUM(CASE WHEN {GAIN_SQL_A} = 1 AND a.state != 'confirmed'
                                 THEN a.size ELSE 0 END), 0)          AS gain_bytes
        FROM assets a
        WHERE a.state != 'skipped'
          AND a.taken_at IS NOT NULL AND a.taken_at != ''
        GROUP BY substr(a.taken_at, 1, 7)
        ORDER BY substr(a.taken_at, 1, 7) DESC
    """).fetchall()
    return [dict(r) for r in rows]


def monthly_breakdown() -> list[dict]:
    """Every month in the library, newest first.

    This is the map for the backfill: it shows which months are done, which
    are part-way, and how big each one is before you commit to clearing it
    out of Google Photos.
    """
    rows = connect().execute("""
        SELECT substr(taken_at, 1, 7)                                AS month,
               COUNT(*)                                              AS total,
               COALESCE(SUM(size), 0)                                AS bytes,
               SUM(CASE WHEN state = 'confirmed' THEN 1 ELSE 0 END)  AS confirmed,
               SUM(CASE WHEN state = 'queued'    THEN 1 ELSE 0 END)  AS queued,
               SUM(CASE WHEN kind  = 'IMAGE'     THEN 1 ELSE 0 END)  AS photos,
               SUM(CASE WHEN kind  = 'VIDEO'     THEN 1 ELSE 0 END)  AS videos
        FROM assets
        WHERE state != 'skipped' AND taken_at IS NOT NULL AND taken_at != ''
        GROUP BY month
        ORDER BY month DESC
    """).fetchall()
    return [dict(r) for r in rows]


def throughput(days: int = 30) -> dict:
    """What has actually been confirmed lately.

    Confirmations arrive in bursts -- Smart Storage clears a whole batch at
    once -- so a short measurement window turns one burst into an absurd
    daily rate. Below MIN_DAYS there is no honest estimate and we say so
    rather than inventing one.
    """
    MIN_DAYS = 7.0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = connect().execute(
        """SELECT COUNT(*) n, COALESCE(SUM(size),0) b
           FROM assets WHERE state='confirmed' AND confirmed_at >= ?""",
        (cutoff,),
    ).fetchone()
    first = connect().execute(
        "SELECT MIN(confirmed_at) t FROM assets WHERE state='confirmed'"
    ).fetchone()["t"]

    age = None
    if first:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(first)).total_seconds() / 86400
        except ValueError:
            age = None

    span = min(float(days), age) if age is not None else None
    enough = span is not None and span >= MIN_DAYS

    return {
        "days": days,
        "files": row["n"],
        "bytes": row["b"],
        "enough_data": enough,
        "min_days": MIN_DAYS,
        "bytes_per_day": (row["b"] / span) if enough and span else 0,
        "files_per_day": (row["n"] / span) if enough and span else 0,
        "measured_over_days": round(span, 1) if span is not None else 0,
        "since": first,
    }


def structural_rate(cap_bytes: int, hold_days: int = 30) -> float:
    """The ceiling the design imposes, regardless of measurement.

    Nothing leaves the phone until Smart Storage clears it, so at most one
    outbox-worth moves per hold period. This is the number that actually
    governs how long a backfill takes.
    """
    return cap_bytes / float(hold_days) if hold_days else 0.0


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
