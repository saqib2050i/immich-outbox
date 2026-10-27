"""Alerting.

This system's most likely real failure is not a crash — it is going quiet.
Google Photos backup gets paused, Smart Storage gets switched off, Syncthing
stops. The dashboard still looks busy, because the outbox keeps filling; it
is just that nothing is draining. Left alone that costs you months.

Each alert fires once when it starts, and repeats only after a cooldown, so
a long outage does not become a stream of notifications.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx

from . import db, settings

COOLDOWN_HOURS = 24

# How long a phone may be away before it is worth saying. Short absences are
# Wi-Fi, a reboot, Doze; a quarter of a day is the phone being off the
# network, and every file in the outbox waits on it.
PHONE_AWAY_HOURS = 6
# Connected and still missing files: a large video takes a while, so this
# only fires once delivery has plainly stopped rather than merely being slow.
UNDELIVERED_HOURS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return None


def _phones() -> list[dict] | None:
    """The devices the outbox is shared with, as Syncthing last said -- or
    None when there is no recent answer to go on. Paused devices are left
    out: pausing is somebody's decision, not a fault."""
    from . import syncthing
    st = syncthing.cached()
    if not st or not st.get("ok"):
        return None
    return [d for d in (st.get("devices") or []) if not d.get("paused")]


def _where_it_stops(phones: list[dict] | None) -> str:
    """One sentence on which side of the hop has stopped, from Syncthing.

    "The outbox is not draining" has two causes with one appearance: the
    phone never got the files, or it has them and Google Photos will not
    take them. Every alert about a stuck outbox used to name both and let
    the reader guess; this is the answer it was guessing at.
    """
    if not phones:
        return ""
    for d in phones:
        if not d.get("connected"):
            seen = d.get("last_seen")
            return (f" Syncthing: {d['name']} is not connected"
                    + (f" (last seen {seen[:16].replace('T', ' ')})" if seen else
                       " and has never been seen")
                    + ", so the files have not reached the phone.")
    for d in phones:
        if d.get("need_items"):
            return (f" Syncthing: {d['name']} is connected but still missing "
                    f"{d['need_items']} file(s) of the outbox.")
    names = ", ".join(d["name"] for d in phones)
    return (f" Syncthing confirms {names} holds every file, so they reached "
            "the phone and the hold-up is Google Photos.")


def evaluate() -> list[dict]:
    """Current problems worth telling someone about."""
    cfg = settings.load()
    out: list[dict] = []
    counts = db.counts()
    phones = _phones()

    # 1. Immich unreachable for a while.
    conn_at = db.get_meta("immich_conn_at")
    try:
        conn = json.loads(db.get_meta("immich_conn") or "{}")
    except ValueError:
        conn = {}
    if conn and not conn.get("ok"):
        age = _age_hours(conn_at) or 0
        if age >= cfg.alert_immich_hours:
            out.append({"key": "immich",
                        "title": "Immich unreachable",
                        "message": f"{conn.get('summary','Not connected')} "
                                   f"(for {age:.0f}h)"})

    # 2. The outbox is not where it should be. Nothing is being confirmed
    #    while this is true, deliberately -- see feeder.outbox_ready().
    problem = db.get_meta("outbox_problem")
    if problem:
        out.append({"key": "outbox_missing",
                    "title": "Outbox unavailable",
                    "message": problem})

    # 3. Nothing coming back. Only meaningful once something is in flight:
    #    with an empty outbox there is nothing to confirm.
    if counts["queued"] > 0:
        # Before the first confirmation there is no last_confirm_at, so fall
        # back to when the oldest file went out. Without this a fresh install
        # reports "nothing confirmed ever" on its very first cycle.
        last_conf = db.get_meta("last_confirm_at") or db.oldest_sent_at()
        age = _age_hours(last_conf)
        days = (age / 24) if age is not None else None
        if days is None or days >= cfg.alert_stall_days:
            when = f"{days:.0f} days" if days is not None else "ever"
            out.append({"key": "stalled",
                        "title": "Nothing backed up recently",
                        "message": f"{counts['queued']} file(s) waiting and nothing "
                                   f"confirmed in {when}. Check Google Photos backup "
                                   f"is on and Smart Storage is set to remove backed-up "
                                   f"photos." + _where_it_stops(phones)})

    # 4. Outbox full and not moving -- the phone is not draining it.
    used = int(db.get_meta("outbox_used", "0"))
    cap = cfg.outbox_max_bytes
    if cap and used >= cap * 0.98:
        since = db.get_meta("outbox_full_since")
        if not since:
            db.set_meta("outbox_full_since", db.now())
        else:
            age = _age_hours(since) or 0
            if age / 24 >= cfg.alert_stall_days:
                out.append({"key": "outbox_full",
                            "title": "Outbox has been full for days",
                            "message": f"Full for {age/24:.0f} days. Syncthing or "
                                       f"Google Photos is not clearing it."
                                       + _where_it_stops(phones)})
    else:
        db.set_meta("outbox_full_since", "")

    # 5. The companion has stopped checking in. Worth saying, because the
    #    symptom is indistinguishable from Google Photos being slow: the
    #    outbox just sits full and nothing looks broken.
    if cfg.companion_enabled:
        from . import companion
        snap = companion.snapshot()
        if snap["state"] == "offline":
            out.append({"key": "companion_offline",
                        "title": "Phone companion has gone quiet",
                        "message": snap["line"] + " Nothing is freeing space "
                                   "on the phone, so the outbox will stay "
                                   "full. Check the app is still running and "
                                   "the phone is on the network."})
        # Google Photos says it is backing up, and has said the same number
        # for three runs running. A stopped backup and a slow one look
        # identical from here -- the count not moving is the difference,
        # and until the phone could read it there was nothing to compare.
        backup = snap.get("backup") or {}
        if backup.get("active"):
            stuck = _age_hours(backup.get("since"))
            window = max(1.0, cfg.companion_cooldown_minutes * 3 / 60)
            if stuck is not None and stuck >= window:
                out.append({"key": "companion_backup_stuck",
                            "title": "Google Photos has stopped uploading",
                            "message": (
                                f"The phone still reports "
                                f"{backup.get('remaining', 0)} to go and the "
                                f"figure has not moved in {stuck:.0f}h "
                                f"({backup.get('detail','')}). Nothing will "
                                "leave the outbox until it resumes — open "
                                "Google Photos on the phone, and check it is "
                                "not restricted by battery optimisation.")})
        # Google Photos keeps saying it has finished and the outbox keeps
        # not draining. One occurrence means nothing -- Photos' media
        # scanner lags Syncthing, so files that arrived minutes ago may not
        # have been looked at yet. The same pile surviving free-up after
        # free-up is a different thing.
        unbacked = snap.get("unbacked") or {}
        if unbacked.get("count"):
            held = _age_hours(unbacked.get("since"))
            window = max(3.0, cfg.companion_cooldown_minutes * 3 / 60)
            if held is not None and held >= window:
                # The title said "the phone is holding" without anybody
                # having asked whether it was. Syncthing knows.
                missing = [d for d in phones or []
                           if not d.get("connected") or d.get("need_items")]
                base = (f"{unbacked['count']} file(s) have stayed in the "
                        f"outbox for {held:.0f}h across several free-ups, "
                        "while Google Photos reported it had finished.")
                if phones and not missing:
                    out.append({"key": "companion_unbacked",
                                "title": "Files the phone is holding but has not backed up",
                                "message": base + _where_it_stops(phones) + (
                                    " Almost always Android's media scanner "
                                    "missed them as they arrived, and Google "
                                    "Photos only uploads what the scanner has "
                                    "indexed. Restarting the phone runs a full "
                                    "scan, which is the one fix known to work.")})
                elif missing:
                    out.append({"key": "companion_unbacked",
                                "title": "Files the phone never received",
                                "message": base + _where_it_stops(phones) +
                                    " Google Photos cannot back up what is not "
                                    "on the phone, so this is delivery, not "
                                    "backup."})
                else:
                    out.append({"key": "companion_unbacked",
                                "title": "Files the phone is holding but has not backed up",
                                "message": base + " Open Photos on the phone "
                                    "and check backup is on for the folder "
                                    "Syncthing writes to. Connecting Syncthing "
                                    "in Settings would say whether the phone "
                                    "has them at all."})

        if snap["state"] == "failed":
            out.append({"key": "companion_failed",
                        "title": "Phone could not free space",
                        "message": snap["line"] + " Google Photos may have "
                                   "renamed the button — the labels it looks "
                                   "for can be changed in Settings."})

    # 5b. The hop only Syncthing can see. Each of these makes the outbox sit
    #     full while every other figure looks like Google Photos being slow.
    from . import syncthing
    st = syncthing.cached()
    problem = ((st or {}).get("folder") or {}).get("problem")
    if problem:
        out.append({"key": "syncthing_folder",
                    "title": "The outbox folder ignores the phone's deletions",
                    "message": problem})
    behind = False
    if phones and counts["queued"] > 0:
        for d in phones:
            if not d.get("connected"):
                away = _age_hours(d.get("last_seen"))
                if away is None or away >= PHONE_AWAY_HOURS:
                    out.append({"key": "syncthing_away",
                                "title": f"{d['name']} is not connected to Syncthing",
                                "message": (
                                    (f"Not seen for {away:.0f}h. " if away is not None
                                     else "Syncthing has never seen it connect. ")
                                    + f"The {counts['queued']} file(s) in the "
                                    "outbox cannot reach the phone until it is "
                                    "back, so nothing will be backed up. Check "
                                    "the phone is on Wi-Fi and Syncthing is "
                                    "running on it.")})
            elif d.get("need_items"):
                behind = True
                since = db.get_meta("sync_behind_since")
                if not since:
                    db.set_meta("sync_behind_since", db.now())
                elif (_age_hours(since) or 0) >= UNDELIVERED_HOURS:
                    out.append({"key": "syncthing_behind",
                                "title": f"{d['name']} is not receiving the outbox",
                                "message": (
                                    f"Connected, but still missing "
                                    f"{d['need_items']} file(s) after "
                                    f"{_age_hours(since):.0f}h. Open Syncthing "
                                    "on the phone: it is usually out of space, "
                                    "or showing a folder error.")})
    if not behind and db.get_meta("sync_behind_since"):
        db.set_meta("sync_behind_since", "")

    # 6. Failures piling up. Say what kind, because the fix is different:
    #    a 404 lives in Immich's storage, not in this service's plumbing.
    if counts["failed"] >= cfg.alert_failed_count:
        kinds = {b["kind"]: b["total"] for b in db.failure_breakdown()}
        advice = []
        if kinds.get("missing"):
            advice.append(f"{kinds['missing']} originals are missing from "
                          "Immich itself (404) — check for an offline or "
                          "moved external library in Immich; no retry here "
                          "can fix those")
        if kinds.get("unreachable"):
            advice.append(f"{kinds['unreachable']} failed reaching Immich — "
                          "check the address and that the server is up")
        if kinds.get("truncated"):
            advice.append(f"{kinds['truncated']} arrived truncated — check "
                          "disk space on the outbox volume")
        if kinds.get("other"):
            advice.append(f"{kinds['other']} failed for other reasons — see "
                          "the file list for details")
        out.append({"key": "failures",
                    "title": f"{counts['failed']} downloads failed",
                    "message": ". ".join(advice) + "."})
    return out


async def send(alert: dict) -> bool:
    cfg = settings.load()
    url = cfg.alert_webhook_url.strip()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            if cfg.alert_format == "ntfy":
                r = await client.post(
                    url,
                    content=alert["message"].encode(),
                    headers={"Title": f"Photo relay: {alert['title']}",
                             "Tags": "warning", "Priority": "default"},
                )
            else:
                r = await client.post(url, json={
                    "title": f"Photo relay: {alert['title']}",
                    "message": alert["message"],
                    "priority": 5,
                })
            return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        db.log("error", f"alert webhook failed: {exc}")
        return False


async def check_and_notify() -> dict:
    """Evaluate, and notify about anything newly wrong."""
    cfg = settings.load()
    active = evaluate()
    db.set_meta("alerts", json.dumps(active))

    if not cfg.alert_webhook_url.strip():
        return {"active": active, "sent": 0}

    try:
        state = json.loads(db.get_meta("alert_state") or "{}")
    except ValueError:
        state = {}

    sent = 0
    now_iso = db.now()
    for a in active:
        last = state.get(a["key"])
        age = _age_hours(last)
        if last and age is not None and age < COOLDOWN_HOURS:
            continue
        if await send(a):
            state[a["key"]] = now_iso
            sent += 1
            db.log("alert", f"{a['title']} — notified")

    # Forget cleared alerts so they notify again if they come back.
    live = {a["key"] for a in active}
    state = {k: v for k, v in state.items() if k in live}
    db.set_meta("alert_state", json.dumps(state))
    return {"active": active, "sent": sent}


async def send_test() -> bool:
    return await send({"title": "Test", "message":
                       "Alerts are wired up correctly. This is a test."})
