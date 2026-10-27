"""Syncthing status, best-effort.

Without it, `queued` means three different things: not yet on the phone,
on the phone waiting to upload, and uploaded but not yet cleared. That
ambiguity is exactly what made the outbox hard to debug during setup.

The hop this service cannot see is the one that matters most: a file sits in
the outbox until Google Photos takes it off the phone, and it can only do
that if the phone has it. "9 files, 70 hours" reads identically whether the
phone is holding them and Photos will not upload, or the phone went offline
on Tuesday and never received them -- and those need opposite fixes. So the
question here is not how many devices are connected. It is whether *this*
device is connected and whether it has *this* folder, completely.

Entirely optional. If it is not configured or not reachable, everything else
works as before.

What it answers is now *used*, not only drawn. It was a panel on Overview
and nothing else: the alerts that exist for exactly this question -- "files
the phone is holding but has not backed up", "outbox full for days" -- went
on guessing which side of the hop had stopped while the answer sat in a
card beside them. So the last answer is kept here, in memory, for
`alerts.evaluate()` to read; housekeeping refreshes it whether or not a
dashboard is open, and every viewer shares one call instead of each making
its own. `/rest/db/status` is documented by Syncthing as expensive, and
every open tab used to call it every twenty seconds.
"""

import re
import time

import httpx

from . import settings

# One answer, shared. In memory rather than in the ledger: writing it there
# would bump the revision on every refresh and push a redraw to every open
# dashboard for a figure that had not changed.
_cache: dict = {"at": 0.0, "data": None}
FRESH_S = 25


def reset() -> None:
    _cache.update(at=0.0, data=None)


def cached(max_age: float = 900) -> dict | None:
    """The last answer, if it is recent enough to act on."""
    if _cache["data"] is None or time.monotonic() - _cache["at"] > max_age:
        return None
    return _cache["data"]


def _iso(ts):
    """Syncthing's timestamps carry nanoseconds and a local offset;
    `fromisoformat` and some browsers' `Date` take at most microseconds.
    Never-seen is the epoch, which is a fact ("never"), not a date."""
    if not isinstance(ts, str) or not ts or ts.startswith("1970-01-01"):
        return None
    return re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00")


def _client(cfg) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg.syncthing_url.rstrip("/"),
        headers={"X-API-Key": cfg.syncthing_api_key},
        timeout=10.0,
    )


async def _json(client: httpx.AsyncClient, path: str, **params):
    """One call, and never an exception. Every one of these is a nicety; a
    Syncthing that answers three of five questions is worth more than a
    panel that disappears because it could not answer the fourth."""
    try:
        r = await client.get(path, params=params or None)
    except Exception:  # noqa: BLE001
        return None
    return r.json() if r.status_code == 200 else None


async def _folder(client, folder: str) -> tuple[dict, list[str]]:
    """The folder's own config, and the devices it is shared with, without
    this one.

    Two shapes, because the config API changed: /rest/config/folders/<id>
    on anything recent, and the whole of /rest/system/config before that.
    """
    me = ((await _json(client, "/rest/system/status")) or {}).get("myID")
    cfg = await _json(client, f"/rest/config/folders/{folder}")
    if cfg is None:
        whole = await _json(client, "/rest/system/config") or {}
        cfg = next((f for f in whole.get("folders", [])
                    if f.get("id") == folder), {})
    cfg = cfg or {}
    return cfg, [d.get("deviceID") for d in cfg.get("devices", [])
                 if d.get("deviceID") and d.get("deviceID") != me]


def folder_problem(cfg: dict) -> str | None:
    """A folder set up so the phone's deletions never come back.

    Confirmation here *is* a file disappearing from the outbox, and the only
    thing that makes one disappear is Google Photos clearing it on the phone
    and Syncthing carrying that deletion home. A Send Only folder discards
    remote changes, and `ignoreDelete` discards exactly the one that matters
    -- either way the outbox fills, nothing is ever confirmed, and every
    other figure looks like Google Photos being slow.
    """
    if cfg.get("type") == "sendonly":
        return ("This folder is Send Only on the server, so the phone's "
                "deletions are never applied here. Nothing will ever be "
                "confirmed. Set it to Send & Receive in Syncthing.")
    if cfg.get("ignoreDelete"):
        return ("This folder has Ignore Delete switched on, so the phone's "
                "deletions are never applied here. Nothing will ever be "
                "confirmed. Turn it off in the folder's advanced settings.")
    return None


async def _names(client) -> dict:
    devices = await _json(client, "/rest/config/devices")
    if devices is None:
        devices = (await _json(client, "/rest/system/config") or {}).get("devices", [])
    return {d.get("deviceID"): d.get("name") or "" for d in (devices or [])}


async def status(fresh: bool = False) -> dict:
    """The current answer, shared by every caller for FRESH_S seconds."""
    if (not fresh and _cache["data"] is not None
            and time.monotonic() - _cache["at"] < FRESH_S):
        return _cache["data"]
    out = await _status()
    _cache.update(at=time.monotonic(), data=out)
    return out


async def _status() -> dict:
    cfg = settings.load()
    if not cfg.syncthing_url.strip() or not cfg.syncthing_api_key.strip():
        return {"configured": False}

    folder = cfg.syncthing_folder.strip()
    out: dict = {"configured": True, "ok": False, "devices": []}
    try:
        async with _client(cfg) as client:
            # Reached and authenticated, which is a different question from
            # whether the folder id is right.
            try:
                probe = await client.get("/rest/system/connections")
            except Exception as exc:  # noqa: BLE001
                return {**out, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}
            if probe.status_code in (401, 403):
                return {**out, "error": "API key rejected"}
            if probe.status_code != 200:
                return {**out, "error": f"HTTP {probe.status_code} from Syncthing"}
            out["ok"] = True
            conns = (probe.json().get("connections") or {})

            if folder:
                d = await _json(client, "/rest/db/status", folder=folder)
                if d is None:
                    out["error"] = "the folder id was not recognised"
                else:
                    glob, need = d.get("globalBytes") or 0, d.get("needBytes") or 0
                    out.update({
                        "state": d.get("state", "unknown"),
                        "global_bytes": glob,
                        "need_bytes": need,
                        "in_sync_pct": 100.0 if not glob
                                       else round((1 - need / glob) * 100, 1),
                        "errors": d.get("errors", 0),
                    })

            names = await _names(client)
            stats = await _json(client, "/rest/stats/device") or {}
            fcfg, shared = (await _folder(client, folder)) if folder else ({}, [])
            out["folder"] = {
                "label": fcfg.get("label") or folder,
                "type": fcfg.get("type"),
                "paused": bool(fcfg.get("paused")),
                "problem": folder_problem(fcfg),
            }
            for dev in shared:
                c = conns.get(dev) or {}
                # How much of *this folder* that device actually holds --
                # the only figure that answers "is it on the phone?".
                done = await _json(client, "/rest/db/completion",
                                   folder=folder, device=dev) or {}
                out["devices"].append({
                    "id": dev,
                    "short": dev.split("-")[0],
                    "name": names.get(dev) or dev.split("-")[0],
                    "connected": bool(c.get("connected")),
                    "paused": bool(c.get("paused")),
                    "address": c.get("address") or "",
                    "last_seen": _iso((stats.get(dev) or {}).get("lastSeen")),
                    "completion": done.get("completion"),
                    "need_bytes": done.get("needBytes"),
                    "need_items": done.get("needItems"),
                })
            out["devices_connected"] = sum(1 for d in out["devices"]
                                           if d["connected"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"

    # Nothing reads a stored copy of this, and writing one would bump the
    # ledger revision on every poll, pushing a pointless re-render to every
    # open dashboard.
    return out


async def file_holders(name: str) -> dict:
    """Which devices hold one file of the outbox, by Syncthing's account.

    Two questions, because one of them cannot answer for a phone that is
    offline. `/rest/db/file`'s `availability` lists devices that have the
    current version *and are connected*; it is a list of objects
    (`{"id": ..., "fromTemporary": ...}`), and reading it as a list of
    strings -- which is what this used to do -- found nobody, every time,
    so a trace said "no other device has it yet" about files sitting on the
    phone. `/rest/db/remoteneed` is per device and answers from that
    device's last index, connected or not: a file it does not list is a
    file it has.
    """
    cfg = settings.load()
    folder = cfg.syncthing_folder.strip()
    async with _client(cfg) as client:
        r = await client.get("/rest/db/file", params={"folder": folder, "file": name})
        if r.status_code != 200:
            return {"known": False,
                    "note": f"Syncthing answered HTTP {r.status_code} for this "
                            "file — check the folder id"}
        d = r.json()
        glob = d.get("global") or {}
        local = d.get("local") or {}
        names = await _names(client)
        conns = ((await _json(client, "/rest/system/connections")) or {}).get(
            "connections") or {}
        _, shared = await _folder(client, folder)
        available = {a.get("id") if isinstance(a, dict) else a
                     for a in (d.get("availability") or [])}

        devices = []
        for dev in shared:
            need = await _json(client, "/rest/db/remoteneed", folder=folder,
                               device=dev, page=1, perpage=5000)
            if glob.get("deleted"):
                has = False
            elif dev in available:
                has = True
            elif need is None:
                has = None
            else:
                has = not any(f.get("name") == name for f in need.get("files") or [])
            devices.append({"name": names.get(dev) or dev.split("-")[0],
                            "connected": bool((conns.get(dev) or {}).get("connected")),
                            "has": has})
        return {
            "known": True,
            "on_the_server": bool(local.get("size")) and not local.get("deleted"),
            "deleted_globally": bool(glob.get("deleted")),
            "size": glob.get("size"),
            "other_devices_with_it": [x["name"] for x in devices if x["has"]],
            "devices": devices,
        }
