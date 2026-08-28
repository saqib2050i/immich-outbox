"""Thin Immich API client.

Immich's API moves between releases. Every endpoint the bridge touches is
defined here, so if your server version differs, this is the only file to
adjust. Check your own server's schema at  <IMMICH_URL>/api/docs .

Verified against Immich v1.13x:
    POST /api/search/metadata      -> paginated asset list
    GET  /api/assets/{id}/original -> untouched original file
"""

from typing import AsyncIterator

import httpx

from . import config

SEARCH = "/api/search/metadata"
ORIGINAL = "/api/assets/{id}/original"


def _headers() -> dict:
    from . import settings
    return {"x-api-key": settings.load().immich_api_key, "Accept": "application/json"}


def _client(timeout: float = 60.0) -> httpx.AsyncClient:
    from . import settings
    return httpx.AsyncClient(base_url=settings.load().immich_url,
                             headers=_headers(), timeout=timeout)


def _normalise(item: dict) -> dict | None:
    """Map an Immich asset to a ledger row, or None if it should be ignored."""
    kind = (item.get("type") or "").upper()
    if kind not in ("IMAGE", "VIDEO"):
        return None

    exif = item.get("exifInfo") or {}
    size = int(exif.get("fileSizeInByte") or 0)

    return {
        "id": item["id"],
        "filename": item.get("originalFileName") or f"{item['id']}.bin",
        "size": size,
        "checksum": item.get("checksum"),
        "taken_at": item.get("fileCreatedAt") or item.get("createdAt") or "",
        "kind": kind,
        "state": "pending",
        "queued_at": None,
    }


async def list_assets(taken_after: str | None = None) -> AsyncIterator[list[dict]]:
    """Yield pages of normalised assets. taken_after=None means full scan."""
    page = 1
    async with _client() as client:
        while True:
            body: dict = {
                "page": page,
                "size": config.IMMICH_PAGE_SIZE,
                "withExif": True,
                "withDeleted": False,
                "isArchived": None if config.INCLUDE_ARCHIVED else False,
            }
            if taken_after:
                body["takenAfter"] = taken_after
            body = {k: v for k, v in body.items() if v is not None}

            resp = await client.post(SEARCH, json=body)
            resp.raise_for_status()
            data = resp.json()

            block = data.get("assets") or {}
            items = block.get("items") or []
            if not items:
                return

            rows = [r for r in (_normalise(i) for i in items) if r]
            if rows:
                yield rows

            nxt = block.get("nextPage")
            if not nxt:
                return
            page = int(nxt)


async def stream_original(asset_id: str):
    """Open a streaming response for the untouched original file.

    Returns (response, client). Caller must close both. The file is passed
    through byte-for-byte so EXIF, capture date and full resolution survive
    the hop to Google Photos.
    """
    client = _client(timeout=None)
    req = client.build_request("GET", ORIGINAL.format(id=asset_id))
    resp = await client.send(req, stream=True)
    resp.raise_for_status()
    return resp, client


async def ping() -> bool:
    try:
        async with _client(timeout=10.0) as client:
            r = await client.get("/api/server/ping")
            return r.status_code == 200
    except Exception:
        return False
