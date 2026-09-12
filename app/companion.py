"""The Pixel companion.

Google Photos frees space on its own schedule: Smart Storage clears a
backed-up file roughly thirty days after it is confirmed. That delay is the
whole pipeline's throughput limit, because the outbox cap is the only flow
control (invariant 2) -- the outbox mirrors the phone's queue folder, so
nothing new goes out until the phone lets go of what it already has.

A small app on the phone taps Google Photos' own "Free up space", which
turns that thirty-day wait into about a minute.

What it must never do is delete a file itself.

Absence is this system's proof of backup: a file that vanishes from the
outbox is a file Google Photos verified and cleared, and that is the only
evidence there is (see `feeder.outbox_ready`). Anything else deleting files
forges that proof, and the ledger would confirm assets that never left the
house. So deletion authority stays with Google Photos and the companion
only ever presses the button. The app declares no storage permission at
all, which makes this Android's guarantee rather than a promise in a
comment.

Nothing in this module confirms anything. Confirmation still happens where
it always did, in `feeder.reconcile()`, from files that are no longer on
disk.

The phone polls; the server never connects to the phone. A phone on DHCP
has no stable address, an inbound listener is a permanent open port on the
LAN, and Doze is far kinder to a scheduled outbound request than to a
socket held open. The cost is that "free up now" takes effect on the next
poll, so the server sets the interval and asks for a fast one exactly when
there is something worth waking up for.
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone

from . import config, db, settings

# Substrings, matched case-insensitively against the labels on screen. They
# are settings rather than constants because Google renames these without
# warning, and re-pointing the app at a new label should be a text field in
# the dashboard, not a new APK.
DEFAULT_LABELS = ("free up space on this device", "free up space",
                  "free up device storage")
DEFAULT_CONFIRM = ("free up", "allow", "delete", "ok", "continue")
# What marks the backup panel on Google Photos' own home screen. Read off a
# Pixel 1: collapsed it says "Backing up photos"; expanded it adds
# "Backing up 250 photos", "2 hours, 26 min remaining" and, in Google's own
# words, "Keep the app open for faster backup".
DEFAULT_BACKUP = ("backing up", "backup in progress", "uploading")

# A run the phone never reported back on. Long enough to cover a slow sweep
# of a large library, short enough that a wedged run does not block the next
# one all day.
RUN_TIMEOUT_MINUTES = 30


# The APK, so the phone can be updated from this server rather than from a
# cable and a laptop. CI builds it and drops it here; the directory sits
# next to app/ so a source checkout and the image find it the same way the
# VERSION file works.
DIST_DIR = os.getenv("DIST_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")
APK_NAME = "companion.apk"

_apk_cache: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def apk_path() -> str:
    return os.path.join(DIST_DIR, APK_NAME)


def apk_info() -> dict:
    """What is on offer, or why nothing is.

    The checksum is cached against the file's size and mtime: this is read
    on every dashboard poll, and hashing a few megabytes each time to
    answer "is there an app?" would be silly.
    """
    path = apk_path()
    try:
        st = os.stat(path)
    except OSError:
        return {"available": False, "version": "", "size": 0,
                "sha256": "", "signed": "", "built_at": ""}

    stamp = (st.st_size, int(st.st_mtime))
    if _apk_cache.get("stamp") != stamp:
        meta = {}
        try:
            with open(os.path.join(DIST_DIR, "companion.json")) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            pass
        if not meta.get("sha256"):
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            meta["sha256"] = h.hexdigest()
        _apk_cache.clear()
        _apk_cache.update(stamp=stamp, info={
            "available": True,
            # Built from the same VERSION file as this server, on purpose:
            # the two speak a protocol, so a pair that disagrees is a pair
            # nobody has tested together.
            "version": meta.get("version") or config.APP_VERSION,
            "size": st.st_size,
            "sha256": meta["sha256"],
            # "debug" means CI had no signing key, and Android will refuse
            # to install it over a copy signed with a different one.
            "signed": meta.get("signed", ""),
            "built_at": meta.get("built_at", ""),
        })
    return dict(_apk_cache["info"])


def _age_minutes(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 60
    except ValueError:
        return None


def _json(key: str) -> dict:
    try:
        return json.loads(db.get_meta(key) or "{}")
    except ValueError:
        return {}


# ---- pairing ------------------------------------------------------------

def ensure_token() -> str:
    """The shared secret, made on first use. Shown once in Settings and
    typed into the phone; the phone sends it on every request."""
    tok = db.get_meta("companion_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        db.set_meta("companion_token", tok)
    return tok


def rotate_token() -> str:
    db.set_meta("companion_token", "")
    db.log("companion", "pairing token rotated — the phone must be re-paired")
    return ensure_token()


def token_ok(raw: str | None) -> bool:
    # compare_digest so a wrong token cannot be found a character at a time.
    return bool(raw) and hmac.compare_digest(str(raw), ensure_token())


def _split(raw: str, fallback: tuple) -> list[str]:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return parts or list(fallback)


def labels(cfg) -> tuple[list[str], list[str]]:
    return (_split(cfg.companion_labels, DEFAULT_LABELS),
            _split(cfg.companion_confirm_labels, DEFAULT_CONFIRM))


def backup_labels(cfg) -> list[str]:
    return _split(cfg.companion_backup_labels, DEFAULT_BACKUP)


# ---- asking for a run ---------------------------------------------------

# What the phone is being asked to do. FREE walks Google Photos to the
# button and presses it; LOOK only opens Photos, waits, and reads what it
# says about its own backup. A look is seconds where a free-up is a minute
# of tapping, which is what makes it cheap enough to do on a schedule.
FREE = "free"
LOOK = "look"


def request(source: str = "manual", reason: str = "",
            action: str = FREE) -> dict:
    """Queue something for the phone. Picked up on its next poll."""
    pending = _json("companion_request")
    if pending.get("id"):
        return pending
    req = {"id": secrets.token_hex(8), "at": db.now(), "action": action,
           "source": source, "reason": reason}
    db.set_meta("companion_request", json.dumps(req))
    what = "free-up" if action == FREE else "a look at Google Photos"
    db.log("companion", f"{what} requested ({source})"
                        + (f": {reason}" if reason else ""))
    return req


def cancel_request() -> None:
    db.set_meta("companion_request", "")


def _worth_asking(cfg) -> tuple[bool, str]:
    """Would a free-up achieve anything?

    Two cases, and the second is the one this missed for a long time.

    The first is throughput: the outbox is full and files are waiting
    behind it, so nothing new goes out until the phone lets go of
    something. Free space reaching zero is not the test -- the next file
    no longer fitting is, which is the same thing the dashboard calls
    "full".

    The second is the tail, and it used to be answered "no". Once the
    library is fully queued there is nothing waiting behind the outbox at
    all, so the old test stopped at its first line and declined. But the
    files *in* the outbox are not backed up yet: in this system a file is
    backed up when it disappears, and only Google Photos clearing it off
    the phone makes it disappear. So the last outbox-full of every run sat
    there untouched until Smart Storage's thirty-day clock reached it --
    which is the exact wait this companion exists to remove. Measured
    once: 1,518 files, 15.3 GB, twelve hours, and not one request made.
    """
    # From the ledger, not the `outbox_files` meta: that is written by
    # reconcile() and so undercounts everything top_up() has added since.
    # This is the same figure the belt shows as "in the outbox".
    holding = db.counts().get("queued", 0)
    used = int(db.get_meta("outbox_used", "0"))
    if holding <= 0:
        return False, "the outbox is empty"

    free = max(cfg.outbox_max_bytes - used, 0)
    smallest = db.smallest_sendable(cfg.eligibility)
    if smallest is not None and free < smallest:
        waiting = sum(m["asked"] + m["eligible"] for m in db.waiting_breakdown())
        return True, f"outbox full, {waiting} file(s) waiting behind it"
    return True, (f"{holding} file(s) in the outbox with nothing behind them — "
                  "only a free-up will clear these")


def _idle_seconds(cfg, report: dict) -> int:
    """How long before the phone should ask again when there is nothing for
    it to do.

    This number *is* the latency of "free up now". The phone dials out and
    the server never dials in -- a phone on DHCP has no stable address and
    an inbound listener is a permanently open port -- so a command cannot
    reach a sleeping phone any sooner than the phone next asks for one.

    Which makes the interval a straight trade against the phone's battery,
    and charging the axis to make it on: a check-in is one small POST, and
    a phone on a charger can afford one a minute where a phone on battery
    cannot. The shelf phone this was built for is never off its cable, and
    was waiting up to half an hour to be told to do something.
    """
    minutes = (cfg.companion_charging_poll_minutes if report.get("charging")
               else cfg.companion_idle_poll_minutes)
    # The app floors this at 60 too. A misconfigured interval must not turn
    # into a hot loop on somebody's phone.
    return max(60, minutes * 60)


def _cooldown_left(cfg) -> float:
    last = db.get_meta("companion_last_run_at")
    age = _age_minutes(last)
    if age is None:
        return 0.0
    return max(0.0, cfg.companion_cooldown_minutes - age)


def _stale_after(cfg) -> int:
    """How old a reading of Google Photos' own screen may be before it is
    worth taking another. `companion_watch_minutes` set to zero switches off
    looking in when there is nothing else to do -- it does not mean the
    server should act on a reading from last Tuesday."""
    return max(5, cfg.companion_watch_minutes or 30)


def consider() -> dict | None:
    """Decide what to ask the phone for. Called from the feeder cycle.

    This used to live inside poll(), which runs only when the phone checks
    in -- so the condition was evaluated *by the phone asking*. A phone
    that had gone quiet meant it was never evaluated, request() was never
    called, and request() is what writes the log line. Twelve hours of a
    stalled pipeline therefore produced no entry of any kind, because the
    code that would have written one never ran. An absence is the worst
    thing to have to diagnose from, and it was the only thing on offer.

    Now the server decides on its own clock and the phone only collects.
    The one judgement left at poll time is the battery, because that is the
    only fact the phone knows and the server does not.
    """
    cfg = settings.load()
    if not cfg.companion_enabled or not cfg.companion_auto:
        return None
    # Already asked, or already running: either way, not again.
    if _json("companion_request").get("id") or _json("companion_inflight").get("id"):
        return None

    worth, why = _worth_asking(cfg)
    backup = _json("companion_backup")
    age = _age_minutes(backup.get("at"))
    stale = age is None or age >= _stale_after(cfg)

    if worth:
        # Freeing space while Google Photos is mid-upload clears whatever it
        # has finished and leaves the rest, which wakes the phone for a
        # fraction of the job. Worse, it makes the leftovers meaningless:
        # files still in the outbox afterwards could be unbacked, or could
        # simply be next in Photos' queue. Waiting until it says it is done
        # is what turns that remainder into a fact worth reporting.
        if cfg.companion_wait_for_backup:
            if stale:
                return request("auto", "checking whether Google Photos has "
                                       "finished uploading", LOOK)
            if backup.get("active"):
                # Not logged: this is the quiet, correct, common case, and a
                # line every cycle would bury everything else.
                return None
        if _cooldown_left(cfg) > 0:
            return None
        return request("auto", why, FREE)

    # Nothing in the outbox, so nothing to clear. Google Photos may still
    # have a queue of its own, and opening it is also what keeps it out of
    # the standby bucket an app sinks into when nobody opens it.
    if cfg.companion_watch_minutes and stale:
        return request("auto", "nothing in the outbox — looking in on "
                               "Google Photos", LOOK)
    return None


# ---- did the claim hold? -------------------------------------------------

# Long enough for Google Photos to have deleted, Syncthing to have
# propagated it, and reconcile() to have noticed. Checking any sooner
# measures the lag rather than the result.
AUDIT_DELAY_MINUTES = 10


def audit() -> None:
    """Compare what Google Photos claimed against what the outbox did.

    Photos saying "backup complete" is a claim about somebody else's cloud;
    the outbox emptying is the only proof this system has. When a free-up
    runs while Photos claims it has finished, everything on the phone should
    go -- so files still sitting in the outbox a while later are files the
    phone is holding that Google Photos has not actually taken, whatever its
    screen said.

    The honest caveat, and why this needs to persist before it means
    anything: Photos' media scanner lags Syncthing. Files that arrived
    minutes ago may not have been noticed yet, so "complete" can be true of
    everything Photos has looked at and still leave a pile behind. One
    remainder proves nothing. The same remainder, cycle after cycle, does.
    """
    note = _json("companion_audit")
    if not note.get("at"):
        return
    age = _age_minutes(note["at"])
    if age is None or age < AUDIT_DELAY_MINUTES:
        return
    db.set_meta("companion_audit", "")

    # Confirmations rather than the outbox count: top_up() adds files on
    # its own cycle, so an outbox that is the same size may still have
    # turned over completely. A confirmation is a file that genuinely left
    # the phone, and it is the thing that cannot be faked.
    now = db.counts()
    gained = now.get("confirmed", 0) - int(note.get("confirmed") or 0)
    left = now.get("queued", 0)
    was = _json("companion_unbacked")
    if gained > 0 or left <= 0:
        # Something left the phone. The claim held, at least in part.
        if was.get("count"):
            db.log("companion", "the outbox drained after Google Photos said "
                                "it had finished — the claim held")
        db.set_meta("companion_unbacked", "")
        return

    # Nothing left. Keep the clock running if it is the same pile as before.
    same = was.get("count") == left
    db.set_meta("companion_unbacked", json.dumps({
        "count": left,
        "since": was.get("since") if same else db.now(),
        "at": db.now(),
    }))
    if not same:
        db.log("companion", f"Google Photos said it had finished, but {left} "
                            f"file(s) stayed in the outbox — they are on the "
                            f"phone and not in the cloud, or Photos has not "
                            f"noticed them yet")


# ---- the phone checking in ----------------------------------------------

def poll(report: dict) -> dict:
    """A check-in. Records what the phone said, and answers with what to do
    and when to come back."""
    cfg = settings.load()

    device = {k: report.get(k) for k in
              ("device", "app_version", "photos_version", "battery",
               "charging", "free_bytes", "android",
               # What this build understands. Told rather than guessed from
               # the version string, so the dashboard can say whether an
               # instruction would land or be silently ignored.
               "features")}
    device["seen_at"] = db.now()
    db.set_meta("companion_device", json.dumps(device))
    db.set_meta("companion_seen_at", device["seen_at"])

    trigger, confirm = labels(cfg)
    apk = apk_info()
    answer = {
        # "free" walks Photos to the button; "look" only opens it and reads
        # what it says. `free_space` stays beside it for phones that predate
        # the distinction: to them a look reads as nothing to do, which is
        # the right thing for them to make of it.
        "action": "none",
        "free_space": False,
        "reason": "",
        "request_id": "",
        "labels": trigger,
        "confirm_labels": confirm,
        "next_poll_seconds": _idle_seconds(cfg, report),
        # What marks the backup panel, and how long to stand in front of it.
        # Zero means do not linger -- the phone never decides this.
        "backup_labels": backup_labels(cfg),
        "dwell_seconds": (cfg.companion_dwell_seconds
                          if cfg.companion_dwell_enabled else 0),
        # Told on every check-in, so the phone finds out about a new build
        # without anyone having to go looking. The app only ever reports
        # this to its own screen -- installing is the browser's job and the
        # user's decision, which is why the app needs no install permission.
        "latest_version": apk["version"] if apk["available"] else "",
    }

    if not cfg.companion_enabled:
        answer["reason"] = "companion disabled on the server"
        # Switched off is the one state worth being slow about: waking a
        # phone every minute to be told there is nothing to do, and never
        # will be, is the interval spent on nothing.
        answer["next_poll_seconds"] = max(60, cfg.companion_idle_poll_minutes * 60)
        return answer

    # A run that was handed out and never reported on. Clear it, or the
    # phone rebooting mid-run would block every later request forever.
    inflight = _json("companion_inflight")
    if inflight.get("id"):
        age = _age_minutes(inflight.get("at"))
        if age is not None and age < RUN_TIMEOUT_MINUTES:
            answer["reason"] = "a run is already in progress"
            answer["next_poll_seconds"] = 60
            return answer
        db.set_meta("companion_inflight", "")
        db.log("companion", "previous free-up never reported back — giving up on it")

    # Whatever the server queued on its own cycle. See consider().
    pending = _json("companion_request")
    if not pending.get("id"):
        left = _cooldown_left(cfg)
        answer["reason"] = (f"waiting {left:.0f} min before asking again"
                            if left > 0 else "nothing to do")
        return answer

    # The phone decides nothing; the server does the refusing, so the reason
    # ends up somewhere a person can read it.
    battery = report.get("battery")
    charging = bool(report.get("charging"))
    if (isinstance(battery, int) and not charging
            and battery < cfg.companion_min_battery):
        answer["reason"] = (f"battery {battery}% is below "
                            f"{cfg.companion_min_battery}% and not charging")
        answer["next_poll_seconds"] = 600
        return answer

    action = pending.get("action") or FREE
    db.set_meta("companion_inflight",
                json.dumps({"id": pending["id"], "at": db.now(),
                            "action": action}))
    cancel_request()
    answer.update(action=action,
                  free_space=(action == FREE),
                  request_id=pending["id"],
                  reason=pending.get("reason") or pending.get("source", ""),
                  next_poll_seconds=60)
    return answer


def _ago(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    if minutes < 60 * 36:
        return f"{minutes/60:.0f} hours"
    return f"{minutes/1440:.0f} days"


def _record_backup(raw) -> None:
    """What Google Photos said about its own backup — a note, never evidence.

    Read off Photos' own home screen while the phone is standing in front
    of it: "Backing up 250 photos", "2 hours, 26 min remaining". This is
    the thing that was missing when the pipeline went quiet for fifteen
    hours and nothing on the dashboard could say whether Google Photos was
    working, stopped, or had never started.

    It changes what the dashboard says and when the server bothers asking.
    It never changes what any asset's state is. Confirmation stays exactly
    where it has always been -- in feeder.reconcile(), derived from files
    that are no longer on disk (invariant 1). A string scraped off somebody
    else's screen that could mark an asset backed up would forge the only
    proof this system has, and a renamed label would do it silently.
    """
    if not isinstance(raw, dict):
        return
    was = _json("companion_backup")
    now = {
        "active": bool(raw.get("active")),
        "remaining": int(raw.get("remaining") or 0),
        "eta_minutes": int(raw.get("eta_minutes") or 0),
        "detail": str(raw.get("detail", ""))[:200],
        "at": db.now(),
    }
    # The count moving is what separates a slow backup from a stopped one,
    # so the clock restarts only when it does.
    moved = (not was.get("active")) or was.get("remaining") != now["remaining"]
    now["since"] = db.now() if moved else (was.get("since") or db.now())
    db.set_meta("companion_backup", json.dumps(now))

    if now["active"] and moved:
        db.log("companion", f"Google Photos: {now['detail'] or 'backing up'}")
    elif was.get("active") and not now["active"]:
        db.log("companion", "Google Photos has finished backing up what is "
                            "on the phone")


def record(result: dict) -> dict:
    """The phone reporting how a run went."""
    inflight = _json("companion_inflight")
    action = str(result.get("action") or inflight.get("action") or FREE)
    run = {
        "id": str(result.get("request_id") or inflight.get("id") or ""),
        "action": action,
        "ok": bool(result.get("ok")),
        "detail": str(result.get("detail", ""))[:500],
        "freed_bytes": int(result.get("freed_bytes") or 0),
        "items": int(result.get("items") or 0),
        # How long it actually stood in front of Google Photos. Reported
        # rather than assumed from the setting: whether the phone dwelled at
        # all used to be answerable only by reading its wake locks.
        "dwelled_seconds": int(result.get("dwelled_seconds") or 0),
        "at": db.now(),
    }
    db.set_meta("companion_inflight", "")
    db.set_meta("companion_last_run", json.dumps(run))
    # Only a free-up resets the clock. A look presses nothing, so letting it
    # start an hour's cooldown would be the watching preventing the work.
    if action == FREE:
        db.set_meta("companion_last_run_at", run["at"])
    _record_backup(result.get("backup"))

    # A free-up that ran while Google Photos claimed to be finished is the
    # one case where the outbox can check the claim. Record where things
    # stood; audit() does the comparing once the deletions have had time to
    # come back through Syncthing.
    backup = result.get("backup")
    if (action == FREE and isinstance(backup, dict)
            and not backup.get("active")):
        c = db.counts()
        db.set_meta("companion_audit", json.dumps({
            "at": run["at"], "confirmed": c.get("confirmed", 0),
            "queued": c.get("queued", 0)}))

    if run["ok"]:
        db.set_meta("companion_last_ok_at", run["at"])
        # Only ever a note. Files leaving the outbox is what confirms them,
        # and that is read off the disk, never off this report.
        db.log("companion", f"freed space on the phone: {run['detail']}")
    else:
        db.log("error", f"phone could not free space: {run['detail']}")
    return run


# ---- for the dashboard --------------------------------------------------

def snapshot() -> dict:
    cfg = settings.load()
    device = _json("companion_device")
    last = _json("companion_last_run")
    pending = _json("companion_request")
    inflight = _json("companion_inflight")
    seen_age = _age_minutes(db.get_meta("companion_seen_at"))

    if not cfg.companion_enabled:
        state, line = "off", "Not in use."
    elif seen_age is None:
        state, line = "waiting", "Waiting for the phone to check in for the first time."
    elif seen_age > cfg.companion_offline_minutes:
        state, line = "offline", ("The phone has not checked in for "
                                  f"{_ago(seen_age)}.")
    elif inflight.get("id"):
        state = "running"
        line = ("Looking in on Google Photos." if inflight.get("action") == LOOK
                else "Freeing space on the phone now.")
    elif pending.get("id"):
        state = "queued"
        line = ("Asked the phone to look in on Google Photos — waiting for it "
                "to pick it up." if pending.get("action") == LOOK else
                "Asked the phone to free space — waiting for it to pick it up.")
    elif last and not last.get("ok"):
        state, line = "failed", f"Last attempt failed: {last.get('detail','')}"
    else:
        state, line = "ok", f"Checked in {seen_age:.0f} min ago."

    apk = apk_info()
    running = (device or {}).get("app_version") or ""
    return {
        "apk": apk,
        # Only ever displayed. See _record_backup for why it can never be
        # allowed to mean more than that.
        "backup": _json("companion_backup"),
        # What the phone is set to do, and what it can do. The second used
        # to be a guess from the version string, so an instruction a phone
        # was too old to understand simply vanished.
        "dwell": {"enabled": cfg.companion_dwell_enabled,
                  "seconds": cfg.companion_dwell_seconds,
                  "watch_minutes": cfg.companion_watch_minutes,
                  "wait_for_backup": cfg.companion_wait_for_backup,
                  "understood": "look" in ((device or {}).get("features") or [])},
        # Files the outbox kept after Google Photos said it had finished.
        # See audit().
        "unbacked": _json("companion_unbacked"),
        "next_free_minutes": _cooldown_left(cfg),
        # A phone on an older build than the server is serving. Only ever a
        # note: nothing refuses to work across a version gap.
        "update_available": bool(apk["available"] and running
                                 and running != apk["version"]),
        "running_version": running,
        "enabled": cfg.companion_enabled,
        "auto": cfg.companion_auto,
        "state": state,
        "line": line,
        "device": device,
        "last_run": last,
        "pending": bool(pending.get("id")),
        "running": bool(inflight.get("id")),
        "seen_minutes": seen_age,
        "cooldown_minutes_left": _cooldown_left(cfg),
        "paired": bool(db.get_meta("companion_token")),
    }
