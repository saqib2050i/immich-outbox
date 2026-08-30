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
import time

from . import alerts, backup, config, db, immich, settings

# Held while a cycle runs, so a reset from the dashboard cannot land halfway
# through a download and leave the ledger disagreeing with the outbox.
CYCLE_LOCK = asyncio.Lock()

# Byte-level progress for the files being fetched right now.
#
# Deliberately in memory and NOT in the ledger. Every ledger write bumps the
# revision and pushes an event to every open dashboard, so recording progress
# there would turn one download into hundreds of broadcasts and redraws. This
# is read by a cheap polling endpoint instead, and costs nothing when nobody
# is looking.
TRANSFERS: dict = {}
BATCH: dict | None = None


def transfer_snapshot() -> dict:
    """Everything moving right now, plus how the whole claim is going."""
    out = []
    for t in list(TRANSFERS.values()):
        snap = dict(t)
        elapsed = max(time.monotonic() - snap.pop("started", 0.0), 0.001)
        snap["seconds"] = round(elapsed, 1)
        snap["bytes_per_second"] = snap["bytes"] / elapsed
        if snap["size"] and snap["bytes_per_second"] > 0:
            remaining = max(snap["size"] - snap["bytes"], 0)
            snap["eta_seconds"] = round(remaining / snap["bytes_per_second"])
        else:
            snap["eta_seconds"] = None
        out.append(snap)
    out.sort(key=lambda s: (s["kind"] != "VIDEO", s["filename"]))

    batch = None
    if BATCH:
        batch = dict(BATCH)
        batch["bytes_done"] = batch["bytes_done"] + sum(
            t["bytes"] for t in TRANSFERS.values())
    return {"transfers": out, "batch": batch}


# Proof that the outbox we are looking at is the real one. See outbox_ready().
MOUNT_MARKER = ".immich-outbox-mounted"


# Files written before the ledger tracked names carry an "<id>__" prefix.
# Still recognised on read so they keep resolving; nothing new uses it.


def _real_names() -> list[str]:
    """Everything in the outbox that is a delivered file.

    Excludes Syncthing's bookkeeping and its in-flight temporaries, our own
    dotfiles (the mount marker, download partials), and anything else
    hidden. One definition, so the listing, the purge and the emptier can
    never disagree about what counts as a file.
    """
    try:
        names = os.listdir(config.OUTBOX_DIR)
    except OSError:
        return []
    return [n for n in names
            if n not in config.IGNORED
            and not n.startswith(".")
            and not n.startswith("~syncthing~")
            and not n.endswith(".tmp")]


def outbox_ready() -> tuple[bool, str]:
    """Is the outbox actually there?

    Confirmation is derived from files disappearing, so an outbox that
    quietly vanishes -- a bind mount that did not come up, a wrong host
    path, Docker creating an empty directory where the share should be --
    reads as "Google Photos has verified every one of these". The whole
    queue gets marked confirmed and, because confirmed assets are never
    re-sent, those originals never make the trip.

    The guard is a marker file living *inside* the outbox, so it disappears
    exactly when the outbox does. Absence is only treated as a fresh start
    when nothing is waiting to be confirmed; once the ledger says files
    should be here, an unmarked empty directory is the failure itself.
    """
    marker = os.path.join(config.OUTBOX_DIR, MOUNT_MARKER)
    if os.path.isdir(config.OUTBOX_DIR) and os.path.exists(marker):
        return True, ""

    queued = db.counts()["queued"]
    if queued and not _real_names():
        return False, (f"the outbox at {config.OUTBOX_DIR} is empty and unmarked "
                       f"while {queued} file(s) should be in it — refusing to read "
                       f"that as a backup. Check the volume is mounted.")

    # Safe to (re)claim it: either nothing is in flight, so nothing can be
    # falsely confirmed, or the files are visibly right here.
    try:
        os.makedirs(config.OUTBOX_DIR, exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("Created by immich-outbox. Do not delete: its absence is how\n"
                     "the service notices the outbox has gone missing.\n")
        os.chmod(marker, 0o664)
    except OSError as exc:
        return False, f"cannot write to {config.OUTBOX_DIR}: {exc}"
    return True, ""


def list_outbox() -> tuple[list[str], int]:
    """Real files in the outbox, plus the bytes they occupy."""
    names, total = [], 0
    for name in _real_names():
        path = os.path.join(config.OUTBOX_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
        names.append(name)
    return db.ids_for_outbox_names(names), total


def sweep_partials(max_age_hours: int = 6) -> int:
    """Remove temp files orphaned by a crash or restart."""
    import time
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for name in os.listdir(config.OUTBOX_DIR):
            if not (name.startswith(".partial-") and name.endswith(".part")):
                continue
            path = os.path.join(config.OUTBOX_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def purge_motion_parts() -> int:
    """Remove motion components left in the outbox by an earlier version.

    This is the one case where a file is deleted from the outbox. It is safe
    because it is never counted as backed up: components are 'skipped' in the
    ledger, so their disappearance is not read as confirmation. The still
    image carries the embedded clip, so nothing is lost.
    """
    names = _real_names()
    if not names:
        return 0
    # One pass, not two queries per file: on a full outbox the per-file
    # version cost thousands of round trips every cycle.
    parts = set(db.motion_parts_among(db.ids_for_outbox_names(names)))
    if not parts:
        return 0

    doomed = set(db.outbox_names_for(sorted(parts)).values())
    # Legacy files carry the id in the name instead of the ledger, so they
    # have to be matched the other way round.
    doomed |= {n for n in names if "__" in n and n.split("__", 1)[0] in parts}

    removed = 0
    for name in doomed & set(names):
        try:
            os.remove(os.path.join(config.OUTBOX_DIR, name))
            removed += 1
        except OSError:
            continue
    if removed:
        db.log("info", f"removed {removed} motion-photo clip(s) from the outbox — "
                       "the still image already contains them")
    return removed


def reconcile() -> tuple[list[str], int]:
    ready, why = outbox_ready()
    db.set_meta("outbox_problem", "" if ready else why)
    if not ready:
        # Nothing is confirmed, nothing is topped up, and the ledger is left
        # exactly as it was. The originals are all still in Immich.
        if db.get_meta("outbox_problem_logged") != why:
            db.log("error", why)
            db.set_meta("outbox_problem_logged", why)
        return [], 0
    db.set_meta("outbox_problem_logged", "")

    sweep_partials()
    purge_motion_parts()
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
    """Fill the outbox to its cap with assets that have never been sent.

    The cap is the flow control, so the cap -- not a file count -- decides
    when to stop. `max_batch_files` bounds how many are claimed at a time,
    not how many a cycle may send: with a 16 GB outbox and 4 MB photos, a
    hard limit of forty files filled 160 MB of it and left the rest idle
    until the next cycle.

    Paused is re-read between files, so pausing takes effect within one
    file rather than at the end of a long fill.
    """
    cfg = settings.load()
    if cfg.paused:
        return 0

    # Never write into an outbox we cannot vouch for: the files would go
    # nowhere and the ledger would call them sent.
    ready, _ = outbox_ready()
    if not ready:
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

    written: list[str] = []
    started_empty = used == 0

    while budget > 0:
        rows = db.claim_batch(budget, cfg.max_batch_files, filt,
                              allow_oversize=(started_empty and not written))
        if not rows:
            break

        spent, stopped = await _fetch_batch(rows, budget)
        budget -= spent
        written.extend(stopped["written"])
        if stopped["paused"] or not stopped["progressed"]:
            # Either the user pressed pause, or nothing in that claim could
            # be written -- keep claiming and we would spin on the same rows.
            break

    if written:
        # One log line per cycle: the per-file signal is the state change
        # itself, and forty lines an hour would bury everything else.
        db.log("queue", f"added {len(written)} file(s) to the outbox")
    return len(written)


async def _fetch_batch(rows, budget: int) -> tuple[int, dict]:
    """Download one claim's worth, several at a time.

    Photos and videos get separate lanes. One 4 GB video used to occupy the
    whole feeder while forty photos waited behind it; with its own lane it
    proceeds without blocking them.

    Concurrency is safe against the cap because claim_batch already picked a
    set that fits the remaining budget -- the whole claim fits, so the order
    it is fetched in cannot exceed it. A new claim is only made once this one
    has finished.
    """
    global BATCH

    cfg = settings.load()
    lanes = cfg.lanes
    sems = {kind: asyncio.Semaphore(n) for kind, n in lanes.items()}

    written: list[str] = []
    spent = 0
    paused = False
    guard = asyncio.Lock()

    BATCH = {"files_total": len(rows), "files_done": 0,
             "bytes_total": sum(r["size"] or 0 for r in rows),
             "bytes_done": 0}

    async def fetch_one(row) -> None:
        nonlocal spent, paused
        kind = (row["kind"] or "IMAGE").upper()
        async with sems.get(kind, sems["IMAGE"]):
            # Read inside the lane: a long fill should stop soon after the
            # button is pressed, not when the whole claim is done.
            if settings.load().paused:
                paused = True
                return

            asset_id, filename = row["id"], row["filename"]
            # Reserve the name first, so a collision is resolved before any
            # bytes move and the ledger always knows what is on disk.
            dest = os.path.join(config.OUTBOX_DIR,
                                db.reserve_outbox_name(asset_id, filename))
            if os.path.exists(dest):
                # Already on disk from an earlier run. The ledger has to be
                # told, or the row stays pending and the next claim hands it
                # straight back -- which with a fill loop is forever.
                db.mark_queued([asset_id])
                try:
                    existing = os.path.getsize(dest)
                except OSError:
                    existing = row["size"] or 0
                async with guard:
                    written.append(asset_id)
                    spent += existing
                    if BATCH:
                        BATCH["files_done"] += 1
                        BATCH["bytes_done"] += existing
                return

            TRANSFERS[asset_id] = {
                "filename": filename, "asset_id": asset_id, "kind": kind,
                "bytes": 0, "size": row["size"] or 0,
                "started": time.monotonic(),
            }
            tmp = None
            try:
                # The temp file goes INSIDE the outbox, hidden and
                # .part-suffixed. A rename within one directory is atomic, so
                # Syncthing never sees a half-written file; the outbox listing
                # skips dotfiles anyway.
                #
                # It cannot live in a separate folder: on Unraid /mnt/user is
                # a FUSE overlay, so two directories in the same share may sit
                # on different disks and rename() fails with EXDEV.
                resp, client = await immich.stream_original(asset_id)
                try:
                    fd, tmp = tempfile.mkstemp(dir=config.OUTBOX_DIR,
                                               prefix=".partial-", suffix=".part")
                    with os.fdopen(fd, "wb") as fh:
                        async for chunk in resp.aiter_bytes(1024 * 512):
                            fh.write(chunk)
                            entry = TRANSFERS.get(asset_id)
                            if entry is not None:
                                entry["bytes"] += len(chunk)
                finally:
                    await resp.aclose()
                    await client.aclose()

                size = os.path.getsize(tmp)
                if row["size"] and abs(size - row["size"]) > 1024:
                    raise IOError(
                        f"size mismatch: got {size}, expected {row['size']}")

                os.replace(tmp, dest)
                os.chmod(dest, 0o664)
                tmp = None
                # Marked the moment the file lands, not once the whole batch
                # is done, so the queue grows a file at a time on screen.
                db.mark_queued([asset_id])
                async with guard:
                    written.append(asset_id)
                    spent += size
                    if BATCH:
                        BATCH["files_done"] += 1
                        BATCH["bytes_done"] += size
            except immich.OriginalMissing as exc:
                # Retrying cannot conjure the file back; spend the whole retry
                # budget now instead of failing identically five times.
                db.mark_failed(asset_id, str(exc), permanent=True)
                db.log("error", f"{filename}: {exc}")
            except Exception as exc:  # noqa: BLE001
                db.mark_failed(asset_id, str(exc))
                db.log("error", f"{filename}: {exc}")
            finally:
                TRANSFERS.pop(asset_id, None)
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)

    await asyncio.gather(*(fetch_one(r) for r in rows))

    BATCH = None
    return spent, {"written": written, "paused": paused,
                   "progressed": bool(written)}


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


def empty_outbox() -> tuple[int, list[str]]:
    """Delete every relayed file from the outbox.

    Syncthing propagates these deletions to the phone, which is what makes
    the next run a genuinely clean test. Returns the count and the asset ids
    that were removed, so the caller can put them back to pending.
    """
    removed, gone = 0, []
    # _real_names() already drops Syncthing's bookkeeping, our own mount
    # marker and any in-flight temporary: deleting the marker would make the
    # very next cycle think the outbox had gone missing.
    for name in _real_names():
        path = os.path.join(config.OUTBOX_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
            gone.append(name)
        except OSError:
            continue
    return removed, db.ids_for_outbox_names(gone)


# How often the outbox is re-read looking for files the phone has cleared.
# The feed cycle is minutes apart because it talks to Immich; noticing a
# departure costs one listdir, so it can happen far more often.
WATCH_INTERVAL_SECONDS = 15


async def watch() -> None:
    """Notice files leaving the outbox promptly.

    Confirmation used to arrive only on the feed cycle, so a phone that
    cleared a batch two minutes after the cycle ran left the dashboard
    showing stale numbers for the next eight. This reads the directory and
    nothing else -- no Immich, no downloads -- so it is cheap enough to run
    continuously.

    It never waits for the cycle lock. If a feed is in progress it simply
    skips this tick: that feed is already publishing per-file updates, and
    blocking here would only queue up redundant passes behind it.
    """
    while True:
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)
        try:
            if CYCLE_LOCK.locked():
                continue
            async with CYCLE_LOCK:
                reconcile()
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"outbox watch error: {exc}")


async def housekeeping() -> None:
    """The chores that have to happen whether or not anyone is looking.

    Both of these used to exist only as buttons and endpoints. That is the
    wrong place for them: the failure this system actually has is going
    quiet, and it goes quiet precisely when nobody has the dashboard open.
    An alert that only fires while you are watching is not an alert, and a
    backup that only happens when you remember is not a backup.
    """
    try:
        await alerts.check_and_notify()
    except Exception as exc:  # noqa: BLE001
        db.log("error", f"alert check failed: {exc}")

    try:
        if settings.load().backup_enabled and backup.due():
            backup.create()
    except Exception as exc:  # noqa: BLE001
        db.log("error", f"scheduled backup failed: {exc}")


def cancel(ids: list[str], skip: bool = False) -> dict:
    """Pull specific files back out of the outbox.

    This deletes from the outbox, which the service otherwise never does.
    It is safe for the same reason `empty_outbox()` is: the ledger rows go
    back to `pending` in the same breath, so the files' absence is never
    read as a Google Photos confirmation. The order matters -- ledger first,
    then the files -- because a crash between the two must leave rows that
    say 'not sent' rather than rows that say 'sent' with nothing on disk.

    The caveat this cannot solve: if Google Photos has already taken a copy
    from the phone, cancelling it here does not remove it from there, and
    the asset will be sent again later as a duplicate. Cancelling is for
    files still waiting, not for undoing an upload.
    """
    if not ids:
        return {"cancelled": 0, "removed": 0}

    names = db.outbox_names_for(ids)
    cancelled = db.cancel_queued(ids, skip=skip)

    removed = 0
    for asset_id in ids:
        candidates = [names[asset_id]] if asset_id in names else []
        # Legacy files carry the id in the name instead of the ledger.
        candidates += [n for n in _real_names()
                       if n.startswith(f"{asset_id}__")]
        for name in candidates:
            try:
                os.remove(os.path.join(config.OUTBOX_DIR, name))
                removed += 1
            except OSError:
                continue

    if cancelled:
        db.log("cancel", f"{cancelled} file(s) taken out of the queue — "
                         + ("they will not be sent unless asked for again"
                            if skip else "they rejoin the waiting list"))
    return {"cancelled": cancelled, "removed": removed}


async def run() -> None:
    was_ok = None
    while True:
        try:
            async with CYCLE_LOCK:
                conn = await refresh_connection()
            if conn["ok"] != was_ok:
                # Only log on change, so a long outage does not flood the log.
                db.log("info" if conn["ok"] else "error", conn["summary"])
                was_ok = conn["ok"]

            if conn["ok"]:
                async with CYCLE_LOCK:
                    _, used = reconcile()
                    await top_up(used)

            # Runs even while Immich is unreachable -- that is itself one of
            # the things worth being told about.
            await housekeeping()
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"feeder error: {exc}")
        await asyncio.sleep(config.FEED_INTERVAL_MIN * 60)
