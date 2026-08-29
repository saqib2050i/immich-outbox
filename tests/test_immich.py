"""The version-aware bits of the Immich client.

Before v3, omitting visibility from a search meant "timeline only". In v3 it
means *any* visibility, so a v1-era client silently starts relaying archived
and hidden photos to Google Photos.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_v3_asks_for_the_timeline_explicitly(rig):
    from app import immich
    body = immich._build_body(1, None, major=3, include_archived=False,
                              use_visibility=True)
    assert body["visibility"] == "timeline"
    assert "isArchived" not in body


async def test_v3_can_be_told_to_include_archived(rig):
    from app import immich
    body = immich._build_body(1, None, major=3, include_archived=True,
                              use_visibility=True)
    assert "visibility" not in body


async def test_pre_v3_uses_the_old_flag(rig):
    from app import immich
    body = immich._build_body(1, None, major=1, include_archived=False,
                              use_visibility=True)
    assert body["isArchived"] is False
    assert "visibility" not in body


async def test_the_visibility_filter_can_be_dropped_if_renamed(rig):
    """If a later release renames the enum, say so and carry on rather than
    stalling every scan."""
    from app import immich
    body = immich._build_body(1, None, major=3, include_archived=False,
                              use_visibility=False)
    assert "visibility" not in body and "isArchived" not in body


async def test_an_unknown_version_is_assumed_modern(rig, monkeypatch):
    """Guessing old would send isArchived, which a v3 server may reject."""
    from app import immich

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, path): raise RuntimeError("no server here")

    monkeypatch.setattr(immich, "_version", None)
    monkeypatch.setattr(immich, "_client", lambda *a, **k: Client())
    assert (await immich.server_version())[0] == 3
    assert immich.version_text().startswith("unknown")


async def test_the_download_client_follows_redirects(rig):
    """v3 can redirect /assets/{id}/original. httpx does not follow
    redirects by default and raise_for_status() ignores 3xx, so without this
    a redirect stub gets written to disk and pushed to the phone as a photo."""
    from app import immich
    client = immich._client()
    try:
        assert client.follow_redirects is True
    finally:
        await client.aclose()


@pytest.mark.parametrize("payload,expected", [
    ({"message": "Not found"}, "Not found"),
    ({"message": ["a", "b"]}, "a; b"),
    ({"errors": [{"path": ["body", "visibility"], "message": "invalid"}]},
     "body.visibility: invalid"),
    ({"errors": [{"path": [], "message": "bare"}]}, "bare"),
])
async def test_both_error_shapes_stay_readable(rig, payload, expected):
    import httpx
    from app import immich
    resp = httpx.Response(400, json=payload)
    assert expected in immich.describe_error(resp)


async def test_an_unparseable_error_still_says_something(rig):
    import httpx
    from app import immich
    resp = httpx.Response(502, text="<html>bad gateway</html>")
    assert "502" in immich.describe_error(resp)


@pytest.mark.parametrize("key", ["livePhotoVideoId", "motionPhotoVideoId",
                                 "livePhotoVideoID"])
async def test_every_name_immich_has_used_for_the_motion_link(rig, key):
    from app import immich
    assert immich.motion_part_id({key: "clip-id"}) == "clip-id"
    assert immich.motion_part_id({key: None}) is None
    assert immich.motion_part_id({}) is None


async def test_normalise_maps_an_asset_to_a_ledger_row(rig):
    from app import immich
    row = immich._normalise({
        "id": "abc", "type": "image", "originalFileName": "IMG_1.jpg",
        "checksum": "sum", "fileCreatedAt": "2026-01-01T00:00:00Z",
        "duration": "00:01:23.500",
        "exifInfo": {"fileSizeInByte": 4096, "exifImageWidth": 6000,
                     "exifImageHeight": 4000},
    })
    assert row["id"] == "abc"
    assert row["kind"] == "IMAGE"
    assert row["size"] == 4096
    assert (row["width"], row["height"]) == (6000, 4000)
    assert row["duration"] == pytest.approx(83.5)
    assert row["state"] == "pending"


async def test_normalise_drops_what_is_not_media(rig):
    from app import immich
    assert immich._normalise({"id": "x", "type": "OTHER"}) is None


async def test_normalise_survives_a_missing_filename(rig):
    from app import immich
    row = immich._normalise({"id": "abc", "type": "VIDEO"})
    assert row["filename"] == "abc.bin"
    assert row["size"] == 0
