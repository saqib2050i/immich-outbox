"""Syncthing status, best-effort.

Without it, `queued` means three different things: not yet on the phone,
on the phone waiting to upload, and uploaded but not yet cleared. That
ambiguity is exactly what made the outbox hard to debug during setup.

Entirely optional. If it is not configured or not reachable, everything else
works as before.
"""

import httpx

from . import db, settings


def _client(cfg) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg.syncthing_url.rstrip("/"),
        headers={"X-API-Key": cfg.syncthing_api_key},
        timeout=10.0,
    )


async def status() -> dict:
    cfg = settings.load()
    if not cfg.syncthing_url.strip() or not cfg.syncthing_api_key.strip():
        return {"configured": False}

    out: dict = {"configured": True, "ok": False}
    try:
        async with _client(cfg) as client:
            if cfg.syncthing_folder.strip():
                r = await client.get("/rest/db/status",
                                     params={"folder": cfg.syncthing_folder.strip()})
                if r.status_code == 200:
                    d = r.json()
                    glob = d.get("globalBytes") or 0
                    need = d.get("needBytes") or 0
                    out.update({
                        "ok": True,
                        "state": d.get("state", "unknown"),
                        "global_bytes": glob,
                        "need_bytes": need,
                        "in_sync_pct": 100.0 if not glob else round((1 - need / glob) * 100, 1),
                        "errors": d.get("errors", 0),
                    })
                else:
                    out["error"] = f"HTTP {r.status_code} — check the folder id"

            r = await client.get("/rest/system/connections")
            if r.status_code == 200:
                conns = (r.json().get("connections") or {})
                online = [c for c in conns.values() if c.get("connected")]
                out["devices_connected"] = len(online)
                # Reached and authenticated. A folder-level error, if there
                # was one, is reported separately in out["error"].
                out["ok"] = True
            elif r.status_code in (401, 403):
                out["error"] = "API key rejected"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"

    # Nothing reads a stored copy of this, and writing one would bump the
    # ledger revision on every poll, pushing a pointless re-render to every
    # open dashboard.
    return out
