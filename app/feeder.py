"""The feeder.

One job: keep the outbox topped up to its cap with assets that have never
been sent, and notice when files leave.

    reconcile  read the outbox. Files that were there and are now gone were
               deleted on the phone by Smart Storage, which only removes
               copies Google Photos has verified -- so mark them confirmed.
    top up     download enough never-sent assets to refill the cap.

That is the entire flow control. The outbox mirrors the phone's queue
folder, so capping the outbox caps the phone. If uploads stall, the outbox
stops draining, the top-up finds no room, and everything waits. Nothing is
lost, because Immich still holds every original.

The service never deletes a file from the outbox. Only the phone does.
"""

import asyncio
import os
import tempfile

from . import config, db, immich, settings

SEP = "__"


def _safe_name(asset_id: str, filename: str) -> str:
    clean = "".join(ch for ch in filename if ch.isalnum() or ch in "._-")
    return f"{asset_id}{SEP}{clean or 'file.bin'}"


def list_outbox() -> tuple[list[str], int]:
    """Real files in the outbox, plus the bytes they occupy."""
    os.makedirs(config.OUTBOX_DIR, exist_ok=True)
    ids, total = [], 0
    for name in os.listdir(config.OUTBOX_DIR):
        if name in config.IGNORED or name.startswith("."):
            continue
        path = os.path.join(config.OUTBOX_DIR, name)
        if not os.path.isfile(path):
            continue
        # Syncthing's in-flight temporaries are not delivered photos yet.
        if name.endswith(".tmp") or name.startswith("~syncthing~"):
            continue
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
        if SEP in name:
            ids.append(name.split(SEP, 1)[0])
    return ids, total


def reconcile() -> tuple[list[str], int]:
    present, used = list_outbox()
    db.mark_present(present)
    confirmed = db.confirm_absent(present)
    if confirmed:
        db.log("confirm", f"{confirmed} file(s) cleared from the phone — backed up")
    db.set_meta("outbox_files", str(len(present)))
    db.set_meta("outbox_used", str(used))
    db.set_meta("last_cycle", db.now())
    return present, used


async def top_up(used: int) -> int:
    cfg = settings.load()
    if cfg.paused:
        return 0

    budget = cfg.outbox_max_bytes - used
    if budget <= 0:
        return 0

    filt = {
        "include_video": cfg.include_video,
        "max_asset_bytes": cfg.max_asset_bytes,
        "ongoing": cfg.ongoing_enabled,
        "ongoing_from": cfg.ongoing_from,
        "backfill": cfg.backfill_enabled,
        "backfill_start": cfg.backfill_start,
        "backfill_end": cfg.backfill_end,
    }
    rows = db.claim_batch(budget, cfg.max_batch_files, filt,
                          allow_oversize=(used == 0))
    if not rows:
        return 0

    os.makedirs(config.SPOOL_DIR, exist_ok=True)
    written = []

    for row in rows:
        asset_id, filename = row["id"], row["filename"]
        dest = os.path.join(config.OUTBOX_DIR, _safe_name(asset_id, filename))
        if os.path.exists(dest):
            written.append(asset_id)
            continue

        tmp = None
        try:
            # Download outside the synced folder, then move in. A partial
            # file inside the outbox would be picked up by Syncthing and
            # handed to Google Photos half-written.
            resp, client = await immich.stream_original(asset_id)
            try:
                fd, tmp = tempfile.mkstemp(dir=config.SPOOL_DIR, suffix=".part")
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 512):
                        fh.write(chunk)
            finally:
                await resp.aclose()
                await client.aclose()

            size = os.path.getsize(tmp)
            if row["size"] and abs(size - row["size"]) > 1024:
                raise IOError(f"size mismatch: got {size}, expected {row['size']}")

            os.replace(tmp, dest)
            os.chmod(dest, 0o664)
            tmp = None
            written.append(asset_id)
        except Exception as exc:  # noqa: BLE001
            db.mark_failed(asset_id, str(exc))
            db.log("error", f"{filename}: {exc}")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    if written:
        db.mark_queued(written)
        db.log("queue", f"added {len(written)} file(s) to the outbox")
    return len(written)


async def refresh_connection() -> dict:
    """Check Immich and store the result for the dashboard."""
    import json as _json
    try:
        result = await immich.check_connection()
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "state": "error",
                  "summary": f"Connection check failed: {exc}", "checks": []}
    db.set_meta("immich_conn", _json.dumps(result))
    db.set_meta("immich_conn_at", db.now())
    return result


async def run() -> None:
    was_ok = None
    while True:
        try:
            conn = await refresh_connection()
            if conn["ok"] != was_ok:
                # Only log on change, so a long outage does not flood the log.
                db.log("info" if conn["ok"] else "error", conn["summary"])
                was_ok = conn["ok"]
            if not conn["ok"]:
                await asyncio.sleep(config.FEED_INTERVAL_MIN * 60)
                continue

            _, used = reconcile()
            await top_up(used)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"feeder error: {exc}")
        await asyncio.sleep(config.FEED_INTERVAL_MIN * 60)
