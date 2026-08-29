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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return None


def evaluate() -> list[dict]:
    """Current problems worth telling someone about."""
    cfg = settings.load()
    out: list[dict] = []
    counts = db.counts()

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
                                   f"photos."})

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
                                       f"Google Photos is not clearing it."})
    else:
        db.set_meta("outbox_full_since", "")

    # 5. Failures piling up.
    if counts["failed"] >= cfg.alert_failed_count:
        out.append({"key": "failures",
                    "title": f"{counts['failed']} downloads failed",
                    "message": "Check the Immich connection and the outbox "
                               "folder permissions."})
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
