import asyncio
import json
import os
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)

from . import alerts, auth, backup, config, db, feeder, immich, settings, syncthing, worker

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    auth.ensure_initialised()
    db.log("start", "outbox feeder started")
    tasks = [asyncio.create_task(worker.run()),
             asyncio.create_task(feeder.run()),
             asyncio.create_task(feeder.watch())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Immich outbox feeder", lifespan=lifespan)

# Paths reachable without a session.
OPEN_PATHS = {"/login", "/api/login", "/healthz"}


@app.middleware("http")
async def gate(request: Request, call_next):
    if not auth.host_allowed(request.headers.get("host")):
        return Response("Unrecognised host name. Reach this by IP address, or "
                        "add the name to ALLOWED_HOSTS.",
                        status_code=421, media_type="text/plain")

    path = request.url.path
    if path in OPEN_PATHS:
        return await call_next(request)

    if not auth.valid_session(request.cookies.get(auth.COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    # Signed in, but still on the starting password: only the change-password
    # route works until it is replaced.
    if auth.must_change() and path not in ("/api/password", "/api/status", "/"):
        if path.startswith("/api/"):
            return JSONResponse({"error": "password change required",
                                 "must_change": True}, status_code=403)
        return RedirectResponse("/", status_code=302)

    return await call_next(request)


@app.post("/api/login")
def login(payload: dict, request: Request):
    # Deliberately a plain `def`: verifying a password is 240k PBKDF2
    # iterations of straight CPU, and awaiting it on the event loop stalls
    # the feeder mid-download. FastAPI runs sync routes in a threadpool.
    client = request.client.host if request.client else "unknown"

    wait = auth.throttle_for(client)
    if wait > 0:
        return JSONResponse(
            {"ok": False, "error": f"Too many attempts — wait {wait:.0f}s"},
            status_code=429, headers={"Retry-After": str(int(wait) + 1)})

    if not auth.verify(str(payload.get("password", ""))):
        auth.note_failure(client)
        db.log("auth", f"failed sign-in attempt from {client}")
        return JSONResponse({"ok": False, "error": "Wrong password"}, status_code=401)

    auth.note_success(client)
    token = auth.new_session()
    resp = JSONResponse({"ok": True, "must_change": auth.must_change()})
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_DAYS * 86400, path="/")
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/password")
def change_password(payload: dict, request: Request):
    # Sync for the same reason as /api/login: this hashes twice.
    current = str(payload.get("current", ""))
    new = str(payload.get("new", ""))

    # Skip the current-password check only while still on the default, so
    # first-time setup does not require typing "admin" twice.
    if not auth.must_change() and not auth.verify(current):
        return JSONResponse({"ok": False, "error": "Current password is wrong"},
                            status_code=400)
    if len(new) < 8:
        return JSONResponse({"ok": False, "error": "Use at least 8 characters"},
                            status_code=400)
    if new == auth.DEFAULT_PASSWORD:
        return JSONResponse({"ok": False, "error": "Pick something other than the default"},
                            status_code=400)

    auth.set_password(new)
    auth.end_all_sessions()
    db.log("auth", "password changed — all sessions signed out")
    token = auth.new_session()          # keep this browser signed in
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_DAYS * 86400, path="/")
    return resp


@app.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


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
            "problem": db.get_meta("outbox_problem") or "",
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
        "failure_kinds": db.failure_breakdown(),
        "events": [dict(r) for r in db.recent_events()],
        "stuck_after_days": config.STUCK_AFTER_DAYS,
        # Evaluated live rather than read from the meta snapshot: the
        # snapshot only refreshes on the housekeeping cycle, so a dismissed
        # failure kept its alert on screen for up to ten minutes.
        "alerts": alerts.evaluate(),
        "last_backup": db.get_meta("last_backup"),
        "revision": db.revision(),
        "auth": {"must_change": auth.must_change(),
                 "default_password": auth.is_default_password()},
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
        # Under the cycle lock, or this races the feeder's own top-up: both
        # would size their batch against the same free space and together
        # write past the cap.
        async with feeder.CYCLE_LOCK:
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


@app.get("/api/queue")
async def queue():
    """The live queue: what is in the outbox waiting on the phone."""
    cfg = settings.load()
    items = db.queue_contents()
    present = set()
    ready, problem = feeder.outbox_ready()
    if ready:
        present = set(feeder._real_names())

    for item in items:
        # A row can say 'queued' while the file is already gone -- the phone
        # cleared it and the next reconcile has not run yet. Showing that
        # honestly is better than showing a file that is not there.
        name = item.get("outbox_name")
        item["on_disk"] = bool(name and name in present) or any(
            n.startswith(f"{item['id']}__") for n in present)

    return {
        "items": items,
        "count": len(items),
        "bytes": sum(i["size"] or 0 for i in items),
        "paused": cfg.paused,
        "progress": db.progress(),
        "outbox": {
            "used_bytes": int(db.get_meta("outbox_used", "0")),
            "cap_bytes": cfg.outbox_max_bytes,
            "problem": problem if not ready else "",
        },
        "last_cycle": db.get_meta("last_cycle"),
    }


@app.post("/api/queue/cancel")
async def queue_cancel(payload: dict):
    """Take files back out of the outbox and return them to pending.

    {"all": true} cancels everything queued, resolved server-side — nobody
    should have to tick a hundred checkboxes to empty the queue, and a
    client-collected id list can go stale between render and click.
    """
    if payload.get("all"):
        ids = [item["id"] for item in db.queue_contents(limit=100000)]
    else:
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(status_code=400, detail="ids must be a list of strings")
    if not ids:
        return {"ok": True, "cancelled": 0, "removed": 0}

    # mode "later" returns them to the waiting pile; "skip" excludes them
    # from sending until they are asked for by name. The distinction exists
    # because cancel-all followed by a refresh otherwise refills the outbox
    # from the backlog, which reads as cancel not working.
    skip = payload.get("mode") == "skip"

    # Under the lock: cancelling mid-download would race the writer.
    async with feeder.CYCLE_LOCK:
        result = feeder.cancel(ids, skip=skip)
        _, used = feeder.reconcile()
    return {"ok": True, "outbox_used": used, "mode": "skip" if skip else "later",
            **result}


@app.get("/api/backlog")
async def backlog():
    """Everything waiting to be sent, by month — the pile behind the queue."""
    months = db.waiting_breakdown()
    return {
        "months": months,
        "total": sum(m["total"] for m in months),
        "bytes": sum(m["bytes"] for m in months),
        "failed": sum(m["failed"] for m in months),
        # The numbers that matter: what is actually on its way out, as
        # against what merely exists in the ledger and is going nowhere.
        "asked": sum(m["asked"] for m in months),
        "eligible": sum(m["eligible"] for m in months),
        "resting": sum(m["resting"] for m in months),
        "to_send_bytes": sum(m["to_send_bytes"] for m in months),
    }


@app.post("/api/backlog/dismiss")
async def backlog_dismiss(payload: dict):
    """Skip waiting assets: one month, some ids, or the whole backlog.

    Nothing is deleted and nothing is sent. Dismissed assets can be brought
    back with 'send this month' or by id once they exist in Immich again.
    """
    month = payload.get("month")
    ids = payload.get("ids")
    everything = bool(payload.get("all"))
    if ids is not None and (not isinstance(ids, list)
                            or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(status_code=400, detail="ids must be a list of strings")
    if not month and not ids and not everything:
        raise HTTPException(status_code=400,
                            detail="pass a month, some ids, or all=true")

    scope = payload.get("scope", "to_send")
    if scope not in ("to_send", "asked", "all"):
        raise HTTPException(status_code=400, detail=f"unknown scope {scope!r}")

    n = db.dismiss_waiting(month=month, ids=ids, everything=everything,
                           scope=scope)
    if n:
        what = f"{month}" if month else (
            {"to_send": "everything queued to send",
             "asked": "everything asked for by hand",
             "all": "the entire waiting list"}[scope] if everything
            else f"{len(ids or [])} file(s)")
        db.log("dismiss", f"{n} waiting file(s) dismissed from {what} — they "
                          "will not be sent unless asked for again")
    return {"ok": True, "dismissed": n}


@app.get("/api/dismissed")
async def dismissed():
    """What has been dismissed by hand, and can be put back."""
    months = db.dismissed_breakdown()
    return {"months": months,
            "total": sum(m["total"] for m in months),
            "bytes": sum(m["bytes"] for m in months)}


@app.post("/api/dismissed/restore")
async def dismissed_restore(payload: dict):
    """Undo a dismissal: one month, some ids, or everything."""
    month = payload.get("month")
    ids = payload.get("ids")
    everything = bool(payload.get("all"))
    if ids is not None and (not isinstance(ids, list)
                            or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(status_code=400, detail="ids must be a list of strings")
    if not month and not ids and not everything:
        raise HTTPException(status_code=400,
                            detail="pass a month, some ids, or all=true")

    n = db.restore_dismissed(month=month, ids=ids, everything=everything)
    if n:
        what = month or ("everything dismissed" if everything
                         else f"{len(ids or [])} file(s)")
        db.log("restore", f"{n} dismissed file(s) restored from {what} — "
                          "they are back in the waiting list")
    return {"ok": True, "restored": n}


@app.post("/api/failed/dismiss")
async def failed_dismiss(payload: dict | None = None):
    """Clear failures without resending. They move to 'skipped' — out of
    the queue and the alerts, recoverable from the library by name."""
    ids = (payload or {}).get("ids")
    if ids is not None and (not isinstance(ids, list)
                            or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(status_code=400, detail="ids must be a list of strings")
    n = db.dismiss_failed(ids)
    if n:
        db.log("dismiss", f"{n} failed file(s) dismissed — they will not be "
                          "retried or sent")
    return {"ok": True, "dismissed": n}


@app.post("/api/pause")
async def pause(payload: dict | None = None):
    """Stop or resume filling the outbox.

    Pausing only stops new files being added. Anything already in the outbox
    stays there and still goes to the phone -- the service cannot recall it,
    and deleting it would be read as a backup.
    """
    want = (payload or {}).get("paused")
    cfg = settings.load()
    paused = (not cfg.paused) if want is None else bool(want)
    cfg = settings.save({"paused": paused})
    db.log("pause", "sending paused" if paused else "sending resumed")

    added = 0
    if not paused:
        # Resume means resume now, not at the next cycle.
        async with feeder.CYCLE_LOCK:
            _, used = feeder.reconcile()
            added = await feeder.top_up(used)
    return {"ok": True, "paused": paused, "added": added}


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
        async with feeder.CYCLE_LOCK:
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
async def download_backup(name: str, background_tasks: BackgroundTasks):
    """Download a backup with the credentials removed."""
    try:
        path = backup.export_sanitised(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such backup")
    background_tasks.add_task(os.remove, path)
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        background=background_tasks)


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
    # Raise the flag rather than starting a second scanner: the worker loop
    # wakes on it within fifteen seconds. Spawning the scan here instead ran
    # it concurrently with the loop's own, paginating the whole library twice.
    db.set_meta("force_full_scan", "1")
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
    if scope not in ("outbox", "resend", "fresh"):
        return {"ok": False, "error": f"unknown scope {scope!r}"}

    # The whole reset happens under the cycle lock. Without it a reset can
    # land while the feeder is awaiting bytes: the download then finishes
    # into an outbox that has just been emptied and a ledger that no longer
    # has a row for it, leaving an orphan file that is re-downloaded later
    # under a new name — a duplicate in Google Photos.
    async with feeder.CYCLE_LOCK:
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

        else:  # fresh
            removed, _ = feeder.empty_outbox()
            n = db.wipe_ledger(forget_motion_parts=bool(payload.get("forget_motion_parts")))
            db.log("reset", f"cleared the ledger ({n} asset(s)) and emptied the outbox "
                            f"({removed} file(s)) — rescanning Immich")
            # Without this the dashboard sits empty until the next scan.
            db.set_meta("force_full_scan", "1")
            result = {"removed": removed, "cleared": n, "rescanning": True}

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
    async with feeder.CYCLE_LOCK:
        _, used = feeder.reconcile()
        added = await feeder.top_up(used)
    return {"ok": True, "added": added}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "immich": await immich.ping()}


@app.get("/")
async def dashboard():
    # no-cache means "revalidate, don't reuse blindly": without it browsers
    # apply heuristic caching and can keep serving a stale dashboard for
    # hours after a deploy. The ETag makes revalidation a cheap 304.
    return FileResponse(STATIC / "dashboard.html",
                        headers={"Cache-Control": "no-cache"})
