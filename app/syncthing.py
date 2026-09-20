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
"""

import httpx

from . import settings


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


async def _folder_devices(client, folder: str) -> list[str]:
    """The devices this folder is shared with, without this one.

    Two shapes, because the config API changed: /rest/config/folders/<id>
    on anything recent, and the whole of /rest/system/config before that.
    """
    me = ((await _json(client, "/rest/system/status")) or {}).get("myID")
    cfg = await _json(client, f"/rest/config/folders/{folder}")
    if cfg is None:
        whole = await _json(client, "/rest/system/config") or {}
        cfg = next((f for f in whole.get("folders", [])
                    if f.get("id") == folder), {})
    return [d.get("deviceID") for d in (cfg or {}).get("devices", [])
            if d.get("deviceID") and d.get("deviceID") != me]


async def _names(client) -> dict:
    devices = await _json(client, "/rest/config/devices")
    if devices is None:
        devices = (await _json(client, "/rest/system/config") or {}).get("devices", [])
    return {d.get("deviceID"): d.get("name") or "" for d in (devices or [])}


async def status() -> dict:
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
            for dev in (await _folder_devices(client, folder) if folder else []):
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
                    "last_seen": (stats.get(dev) or {}).get("lastSeen"),
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
