import asyncio
import json
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from . import alerts, backup, config, db, feeder, immich, settings, syncthing, worker

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    db.log("start", "outbox feeder started")
    tasks = [asyncio.create_task(worker.run()), asyncio.create_task(feeder.run())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Immich outbox feeder", lifespan=lifespan)


@app.get("/api/status")
async def status():
    cfg = settings.load()
    c = db.counts()
    return {
        "counts": c,
        "total": c["pending"] + c["queued"] + c["confirmed"] + c["failed"],
        "outbox": {
            "files": int(db.get_meta("outbox_files", "0")),
            "used_bytes": int(db.get_meta("outbox_used", "0")),
            "cap_bytes": cfg.outbox_max_bytes,
            "last_cycle": db.get_meta("last_cycle"),
            "path": config.OUTBOX_DIR,
            "paused": cfg.paused,
        },
        "immich": {
            "url": cfg.immich_url,
            "last_full_scan": db.get_meta("last_full_scan"),
            "last_incremental_scan": db.get_meta("last_incremental_scan"),
            "version": db.get_meta("immich_version", "not checked yet"),
            "connection": json.loads(db.get_meta("immich_conn") or '{"state":"checking","ok":false,"summary":"Checking connection…","checks":[]}'),
            "checked_at": db.get_meta("immich_conn_at"),
        },
        "window": db.window_progress(cfg.backfill_start, cfg.backfill_end),
        "settings": cfg.as_dict(),
        "stuck": [dict(r) for r in db.stuck(config.STUCK_AFTER_DAYS)],
        "problems": [dict(r) for r in db.problems()],
        "events": [dict(r) for r in db.recent_events()],
        "stuck_after_days": config.STUCK_AFTER_DAYS,
        "alerts": json.loads(db.get_meta("alerts") or "[]"),
        "last_backup": db.get_meta("last_backup"),
        "revision": db.revision(),
    }


@app.get("/api/stats")
async def stats():
    return {
        "breakdown": db.media_breakdown(),
        "monthly": db.monthly_breakdown(),
        "throughput": db.throughput(30),
        "structural_bytes_per_day": db.structural_rate(settings.load().outbox_max_bytes),
        "counts": db.counts(),
    }


@app.get("/api/bucket/{bucket}")
async def bucket_files(bucket: str, state: str = "all",
                       limit: int = 50, offset: int = 0):
    return db.list_in_bucket(bucket, state, min(limit, 200), offset)


@app.post("/api/send")
async def send(payload: dict):
    """Queue things now, ignoring the date windows.

    The outbox cap still applies, so this changes the order work is done in,
    not how much is in flight at once.
    """
    bucket = payload.get("bucket")
    ids = payload.get("ids")
    n = db.force_send(ids=ids, bucket=bucket)
    if n:
        what = f"category {bucket}" if bucket else f"{n} file(s)"
        db.log("send", f"{what} moved to the front of the queue ({n} asset(s))")
        _, used = feeder.reconcile()
        await feeder.top_up(used)
    return {"ok": True, "queued": n}


@app.get("/api/events")
async def events():
    """Server-sent events.

    The dashboard used to poll every five seconds, so an action could sit
    invisible for most of that. The ledger bumps a revision on every write;
    this watches it and pushes as soon as it moves, which in practice is
    within a fraction of a second of anything happening.
    """
    async def stream():
        last = -1
        idle = 0
        while True:
            rev = db.revision()
            if rev != last:
                last = rev
                idle = 0
                yield f"event: changed\ndata: {rev}\n\n"
            else:
                idle += 1
                if idle >= 40:      # ~20s keepalive through proxies
                    idle = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/timeline")
async def timeline():
    return {"months": db.timeline()}


@app.get("/api/month/{month}")
async def month_detail(month: str):
    return db.month_detail(month)


@app.post("/api/month/send")
async def month_send(payload: dict):
    month = str(payload.get("month", ""))
    group = payload.get("group")
    n = db.force_send_month(month, group)
    if n:
        db.log("send", f"{n} file(s) from {month}"
                       + (f" ({group})" if group else "") + " moved to the front")
        _, used = feeder.reconcile()
        await feeder.top_up(used)
    return {"ok": True, "queued": n}


@app.get("/api/reconciliation")
async def reconciliation():
    return {"groups": db.reconciliation(), "counts": db.counts()}


@app.get("/api/syncthing")
async def syncthing_status():
    return await syncthing.status()


@app.get("/api/alerts")
async def get_alerts():
    return await alerts.check_and_notify()


@app.post("/api/alerts/test")
async def test_alert():
    ok = await alerts.send_test()
    return {"ok": ok, "error": None if ok else "No webhook set, or it rejected the request"}


@app.get("/api/backups")
async def get_backups():
    return {"backups": backup.list_backups(), "last": db.get_meta("last_backup")}


@app.post("/api/backups")
async def make_backup():
    return backup.create()


@app.post("/api/backups/restore")
async def restore_backup(payload: dict):
    try:
        return backup.restore(str(payload.get("name", "")))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/backups/{name}")
async def download_backup(name: str):
    try:
        path = backup.path_for(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not Path(path).is_file():
        raise HTTPException(status_code=404, detail="no such backup")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@app.get("/api/settings")
async def get_settings():
    return settings.load().as_dict()


@app.post("/api/settings")
async def post_settings(payload: dict):
    return settings.save(payload).as_dict()


@app.post("/api/window/set")
async def window_set(payload: dict):
    """Jump the backfill window straight to a month, from the statistics
    table, instead of stepping through with Previous/Next."""
    month = str(payload.get("month", ""))   # "YYYY-MM"
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = date(year, mon, 1)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"bad month {month!r}"}

    nxt = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    end = date.fromordinal(nxt.toordinal() - 1)

    cfg = settings.save({
        "backfill_start": start.isoformat(),
        "backfill_end": end.isoformat(),
        "backfill_enabled": bool(payload.get("enable", False)),
    })
    return {"ok": True, "start": cfg.backfill_start, "end": cfg.backfill_end,
            "enabled": cfg.backfill_enabled,
            "window": db.window_progress(cfg.backfill_start, cfg.backfill_end)}


@app.post("/api/window/advance")
async def window_advance(payload: dict | None = None):
    step = int((payload or {}).get("step", 1))
    cfg = settings.advance_window(step)
    return {
        "start": cfg.backfill_start,
        "end": cfg.backfill_end,
        "window": db.window_progress(cfg.backfill_start, cfg.backfill_end),
    }


@app.post("/api/immich/test")
async def immich_test():
    return await feeder.refresh_connection()


@app.post("/api/rescan")
async def rescan():
    db.log("scan", "full rescan requested")
    asyncio.create_task(worker.full_scan())
    return {"ok": True, "scanning": True}


@app.post("/api/retry-failed")
async def retry_failed():
    n = db.retry_failed()
    if n:
        db.log("requeue", f"{n} failed asset(s) put back in the queue")
    return {"ok": True, "requeued": n}


@app.post("/api/reset")
async def reset(payload: dict):
    """Testing tools. Never touches Google Photos or Immich -- only this
    service's own ledger and the outbox folder."""
    scope = str(payload.get("scope", ""))

    if scope == "outbox":
        removed, ids = feeder.empty_outbox()
        db.reset_ids(ids)
        db.log("reset", f"emptied the outbox ({removed} file(s)) and re-queued them")
        result = {"removed": removed, "requeued": len(ids)}

    elif scope == "resend":
        removed, _ = feeder.empty_outbox()
        n = db.reset_states()
        db.log("reset", f"re-queued {n} asset(s) and emptied the outbox ({removed} file(s))")
        result = {"removed": removed, "requeued": n}

    elif scope == "fresh":
        removed, _ = feeder.empty_outbox()
        n = db.wipe_ledger(forget_motion_parts=bool(payload.get("forget_motion_parts")))
        db.log("reset", f"cleared the ledger ({n} asset(s)) and emptied the outbox "
                        f"({removed} file(s)) — rescanning Immich")
        # Without this the dashboard sits empty until the next scheduled scan.
        asyncio.create_task(worker.full_scan())
        result = {"removed": removed, "cleared": n, "rescanning": True}

    else:
        return {"ok": False, "error": f"unknown scope {scope!r}"}

    _, used = feeder.reconcile()
    result["ok"] = True
    result["outbox_used"] = used
    return result


@app.post("/api/requeue/{asset_id}")
async def requeue(asset_id: str):
    db.requeue(asset_id)
    db.log("requeue", f"{asset_id} put back in the queue")
    return {"ok": True}


@app.post("/api/refresh")
async def refresh():
    _, used = feeder.reconcile()
    added = await feeder.top_up(used)
    return {"ok": True, "added": added}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "immich": await immich.ping()}


@app.get("/")
async def dashboard():
    return FileResponse(STATIC / "dashboard.html")
