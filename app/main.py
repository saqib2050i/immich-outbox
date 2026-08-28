import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import config, db, feeder, immich, settings, worker

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
        },
        "window": db.window_progress(cfg.backfill_start, cfg.backfill_end),
        "settings": cfg.as_dict(),
        "stuck": [dict(r) for r in db.stuck(config.STUCK_AFTER_DAYS)],
        "problems": [dict(r) for r in db.problems()],
        "events": [dict(r) for r in db.recent_events()],
        "stuck_after_days": config.STUCK_AFTER_DAYS,
    }


@app.get("/api/settings")
async def get_settings():
    return settings.load().as_dict()


@app.post("/api/settings")
async def post_settings(payload: dict):
    return settings.save(payload).as_dict()


@app.post("/api/window/advance")
async def window_advance(payload: dict | None = None):
    step = int((payload or {}).get("step", 1))
    cfg = settings.advance_window(step)
    return {
        "start": cfg.backfill_start,
        "end": cfg.backfill_end,
        "window": db.window_progress(cfg.backfill_start, cfg.backfill_end),
    }


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
