"""Immich client, version-aware.

Immich v3.0 (July 2026) changed things that matter here. The endpoints
survived -- POST /api/search/metadata and GET /api/assets/{id}/original are
both still there -- but three details changed:

1. Search visibility. Before v3, omitting visibility meant "timeline only",
   so archived assets were excluded by default. In v3 omitting it means
   *any* visibility. Left alone that would silently start relaying archived
   and hidden photos to Google Photos, so on v3 we send an explicit
   visibility instead of the old isArchived flag.

2. The original-file route can redirect. httpx does not follow redirects by
   default and raise_for_status() ignores 3xx, so a redirect would have been
   written to disk as a short HTML body and pushed to the phone as a photo.
   follow_redirects is now on everywhere.

3. Validation moved to Zod and error responses changed shape. Both the old
   and new formats are parsed so failures stay readable.

If scans start failing after a server upgrade, this is the only file to
look at. Compare against <IMMICH_URL>/api/docs.
"""

import re
from typing import AsyncIterator

import httpx

SEARCH = "/api/search/metadata"
ORIGINAL = "/api/assets/{id}/original"

_version: tuple[int, int, int] | None = None
_version_text: str = "unknown"


def _headers() -> dict:
    from . import settings
    return {"x-api-key": settings.load().immich_api_key, "Accept": "application/json"}


def _client(timeout: float | None = 60.0) -> httpx.AsyncClient:
    from . import settings
    return httpx.AsyncClient(
        base_url=settings.load().immich_url,
        headers=_headers(),
        timeout=timeout,
        # v3 can redirect the original-file route; without this the body is
        # a redirect stub, not a photo.
        follow_redirects=True,
    )


def describe_error(resp: httpx.Response) -> str:
    """Readable message from either the pre-v3 or the v3 error shape."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code} {resp.text[:160]}"

    if isinstance(data, dict):
        # v3: {"message": "...", "errors": [{"path": [...], "message": "..."}]}
        if isinstance(data.get("errors"), list) and data["errors"]:
            bits = []
            for e in data["errors"][:4]:
                path = ".".join(str(p) for p in (e.get("path") or []))
                bits.append(f"{path}: {e.get('message')}" if path else str(e.get("message")))
            return f"HTTP {resp.status_code} " + "; ".join(bits)
        msg = data.get("message")
        if isinstance(msg, list):
            return f"HTTP {resp.status_code} " + "; ".join(str(m) for m in msg[:4])
        if msg:
            return f"HTTP {resp.status_code} {msg}"
    return f"HTTP {resp.status_code} {resp.text[:160]}"


async def server_version(refresh: bool = False) -> tuple[int, int, int]:
    global _version, _version_text
    if _version and not refresh:
        return _version

    async with _client(15.0) as client:
        for path in ("/api/server/version", "/api/server/about"):
            try:
                r = await client.get(path)
                if r.status_code != 200:
                    continue
                data = r.json()
                if all(k in data for k in ("major", "minor", "patch")):
                    _version = (int(data["major"]), int(data["minor"]), int(data["patch"]))
                    _version_text = "v{}.{}.{}".format(*_version)
                    return _version
                m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(data.get("version", "")))
                if m:
                    _version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    _version_text = str(data.get("version"))
                    return _version
            except Exception:  # noqa: BLE001
                continue

    # Unknown: assume modern. Guessing old would mean sending isArchived,
    # which a v3 server may reject outright.
    _version = (3, 0, 0)
    _version_text = "unknown (assuming v3)"
    return _version


def version_text() -> str:
    return _version_text


# Immich has used a few names for the link from a still to its motion clip.
_MOTION_KEYS = ("livePhotoVideoId", "motionPhotoVideoId", "livePhotoVideoID")


def motion_part_id(item: dict) -> str | None:
    """The id of this asset's motion component, if it has one."""
    for key in _MOTION_KEYS:
        value = item.get(key)
        if value:
            return str(value)
    return None


def _normalise(item: dict) -> dict | None:
    """Map an Immich asset to a ledger row.

    Type and size filtering deliberately do not happen here -- the ledger
    holds the whole library and the live settings decide what is released.
    """
    kind = (item.get("type") or "").upper()
    if kind not in ("IMAGE", "VIDEO"):
        return None

    exif = item.get("exifInfo") or {}

    # Immich reports duration as "00:01:23.456"; store it as seconds.
    duration = None
    raw = item.get("duration")
    if isinstance(raw, str) and ":" in raw:
        try:
            h, m, sec = raw.split(":")
            duration = int(h) * 3600 + int(m) * 60 + float(sec)
        except (ValueError, TypeError):
            duration = None

    def _dim(*keys):
        for k in keys:
            v = exif.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return None

    return {
        "id": item["id"],
        "filename": item.get("originalFileName") or f"{item['id']}.bin",
        "size": int(exif.get("fileSizeInByte") or 0),
        "checksum": item.get("checksum"),
        "taken_at": item.get("fileCreatedAt") or item.get("createdAt") or "",
        "kind": kind,
        "state": "pending",
        "queued_at": None,
        "width": _dim("exifImageWidth", "imageWidth", "width"),
        "height": _dim("exifImageHeight", "imageHeight", "height"),
        "duration": duration,
    }


def _build_body(page: int, taken_after: str | None, major: int,
                include_archived: bool, use_visibility: bool) -> dict:
    from . import config
    body: dict = {
        "page": int(page),
        "size": int(config.IMMICH_PAGE_SIZE),
        "withExif": True,
    }
    if taken_after:
        body["takenAfter"] = taken_after

    if major >= 3:
        # v3 defaults to every visibility when omitted, which would sweep in
        # archived and hidden assets. Be explicit unless archives are wanted.
        if use_visibility and not include_archived:
            body["visibility"] = "timeline"
    else:
        if not include_archived:
            body["isArchived"] = False
    return body


async def list_assets(taken_after: str | None = None) -> AsyncIterator[list[dict]]:
    """Yield pages of normalised assets. taken_after=None means full scan."""
    from . import config, db

    major = (await server_version())[0]
    db.set_meta("immich_version", _version_text)

    include_archived = config.INCLUDE_ARCHIVED
    use_visibility = True
    page = 1

    async with _client() as client:
        while True:
            body = _build_body(page, taken_after, major, include_archived, use_visibility)
            resp = await client.post(SEARCH, json=body)

            if resp.status_code == 400 and use_visibility and major >= 3:
                # The visibility enum could be renamed in a later release.
                # Drop it once, say so, and carry on rather than stalling.
                db.log("error", "search rejected the visibility filter, retrying without "
                                f"it ({describe_error(resp)})")
                use_visibility = False
                continue

            if resp.status_code >= 400:
                raise RuntimeError(describe_error(resp))

            data = resp.json()
            block = data.get("assets") or {}
            items = block.get("items") or []
            if not items:
                return

            rows = [r for r in (_normalise(i) for i in items) if r]
            # A motion photo is two assets in Immich: the still, which still
            # contains the embedded clip, and an extracted video component.
            # Sending the component separately makes Google Photos show a
            # stray video next to the photo, so record and skip them.
            motion = [m for m in (motion_part_id(i) for i in items) if m]
            if motion:
                db.mark_motion_parts(motion)
            if rows:
                yield rows

            nxt = block.get("nextPage")
            if not nxt:
                return
            page = int(nxt)


async def stream_original(asset_id: str):
    """Open a streaming response for the untouched original file.

    Returns (response, client); the caller closes both. The file passes
    through byte for byte, so EXIF, capture date and full resolution survive
    the hop to Google Photos.
    """
    client = _client(timeout=None)
    req = client.build_request("GET", ORIGINAL.format(id=asset_id))
    resp = await client.send(req, stream=True)
    if resp.status_code >= 400:
        await resp.aread()
        msg = describe_error(resp)
        await resp.aclose()
        await client.aclose()
        raise RuntimeError(msg)
    return resp, client


async def ping() -> bool:
    try:
        async with _client(10.0) as client:
            r = await client.get("/api/server/ping")
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def check_connection() -> dict:
    """Exercise every call the relay actually makes, and report which fails.

    A wrong URL, a wrong key and a key missing one permission all produce an
    empty dashboard otherwise. This distinguishes them, and names the exact
    permission Immich refused.
    """
    from . import settings

    cfg = settings.load()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, needs: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "needs": needs})

    if not cfg.immich_url:
        add("Server address", False, "No address set")
        return {"ok": False, "state": "unconfigured",
                "summary": "No Immich address set", "checks": checks}
    if not cfg.immich_api_key:
        add("API key", False, "No key set")
        return {"ok": False, "state": "unconfigured",
                "summary": "No API key set", "checks": checks}

    # 1. Can we reach it at all?
    try:
        async with _client(10.0) as client:
            r = await client.get("/api/server/ping")
        if r.status_code == 200:
            add("Reachable", True, cfg.immich_url)
        else:
            add("Reachable", False, f"HTTP {r.status_code} from /api/server/ping")
            return {"ok": False, "state": "unreachable",
                    "summary": f"Server answered HTTP {r.status_code}", "checks": checks}
    except Exception as exc:  # noqa: BLE001
        add("Reachable", False, f"{type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "state": "unreachable",
                "summary": "Cannot reach the server — check the address and port",
                "checks": checks}

    # 2. Version, which also decides which request format to use.
    #    server_version() never raises -- it falls back to assuming v3 -- so
    #    check the text, or a rejected key would be reported as a pass.
    v = await server_version(refresh=True)
    guessed = _version_text.startswith("unknown")
    add("Version", not guessed,
        _version_text if not guessed
        else "could not read the version, assuming v3", "server.about")

    # 3. asset.read — the permission that lets us list the library.
    first_id = None
    try:
        async with _client(30.0) as client:
            body = _build_body(1, None, v[0], False, True)
            body["size"] = 1
            r = await client.post(SEARCH, json=body)
        if r.status_code in (401, 403):
            add("Read library", False, describe_error(r), "asset.read")
            # 401 means the key itself was rejected; 403 means the key is
            # valid but lacks the permission. Very different fixes.
            return {"ok": False, "state": "forbidden",
                    "summary": ("The API key was rejected — check it was copied in full"
                                if r.status_code == 401
                                else "The API key is missing the asset.read permission"),
                    "checks": checks}
        if r.status_code >= 400:
            add("Read library", False, describe_error(r), "asset.read")
            return {"ok": False, "state": "error",
                    "summary": "Search was rejected — see the detail below",
                    "checks": checks}
        items = ((r.json().get("assets") or {}).get("items") or [])
        first_id = items[0]["id"] if items else None
        add("Read library", True,
            "search works" + ("" if items else " (library looks empty)"), "asset.read")
    except Exception as exc:  # noqa: BLE001
        add("Read library", False, str(exc)[:120], "asset.read")
        return {"ok": False, "state": "error", "summary": "Search failed", "checks": checks}

    # 4. asset.download — checked with a one-byte range request rather than
    #    pulling a whole photo just to prove permission.
    if first_id:
        try:
            async with _client(30.0) as client:
                r = await client.get(ORIGINAL.format(id=first_id),
                                     headers={"Range": "bytes=0-0"})
            if r.status_code in (200, 206):
                add("Download originals", True, "originals are readable", "asset.download")
            elif r.status_code in (401, 403):
                add("Download originals", False, describe_error(r), "asset.download")
                return {"ok": False, "state": "forbidden",
                        "summary": ("The API key was rejected — check it was copied in full"
                                    if r.status_code == 401
                                    else "The API key is missing the asset.download permission"),
                        "checks": checks}
            else:
                add("Download originals", False, describe_error(r), "asset.download")
                return {"ok": False, "state": "error",
                        "summary": "Could not fetch an original", "checks": checks}
        except Exception as exc:  # noqa: BLE001
            add("Download originals", False, str(exc)[:120], "asset.download")
            return {"ok": False, "state": "error",
                    "summary": "Could not fetch an original", "checks": checks}
    else:
        add("Download originals", True, "skipped — no assets to test with", "asset.download")

    return {"ok": True, "state": "connected",
            "summary": f"Connected to Immich {_version_text}", "checks": checks}
