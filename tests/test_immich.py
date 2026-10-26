"""The version-aware bits of the Immich client.

Before v3, omitting visibility from a search meant "timeline only". In v3 it
means *any* visibility, so a v1-era client silently starts relaying archived
and hidden photos to Google Photos.

The rest of this file is the record of two upgrades that changed no route
and broke things anyway: a duration that became a different unit, and a
page token that is on its way out. Both are the same class of fault --
nothing fails, a figure is simply wrong or a scan is simply short -- so
they are pinned here with the shapes a real server answered with.
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
        "duration": 83500,   # v3: milliseconds
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


async def test_asset_detail_keeps_the_three_dates_apart(rig, monkeypatch):
    """Immich holds three different answers and they are not interchangeable:
    localDateTime is the wall clock, fileCreatedAt the instant, timeZone
    often null. Flattening them is a five-hour error."""
    from app import immich

    class Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"id": "a", "type": "IMAGE",
                    "localDateTime": "2024-01-05T08:47:33.000Z",
                    "fileCreatedAt": "2024-01-05T03:47:33.000Z",
                    "exifInfo": {"timeZone": "Asia/Karachi", "make": "Google",
                                 "model": "Pixel 6 Pro",
                                 "dateTimeOriginal": "2024-01-05T03:47:33.000Z"}}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, path): return Resp()

    monkeypatch.setattr(immich, "_client", lambda *a, **k: Client())
    d = await immich.asset_detail("a")
    assert d["ok"] is True
    assert d["local_date_time"] == "2024-01-05T08:47:33.000Z"
    assert d["file_created_at"] == "2024-01-05T03:47:33.000Z"
    assert d["time_zone"] == "Asia/Karachi"
    assert d["model"] == "Pixel 6 Pro"


async def test_asset_detail_falls_back_to_the_older_route(rig, monkeypatch):
    """Immich called this /api/asset/{id} before 1.106."""
    from app import immich
    seen = []

    class Resp:
        def __init__(self, code): self.status_code = code
        @staticmethod
        def json(): return {"id": "a", "type": "IMAGE"}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, path):
            seen.append(path)
            return Resp(404 if path == "/api/assets/a" else 200)

    monkeypatch.setattr(immich, "_client", lambda *a, **k: Client())
    assert (await immich.asset_detail("a"))["ok"] is True
    assert seen == ["/api/assets/a", "/api/asset/a"]


async def test_asset_detail_never_takes_the_trace_down_with_it(rig, monkeypatch):
    """A trace that could not reach Immich still has two copies to compare."""
    from app import immich

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, path): raise RuntimeError("connection refused")

    monkeypatch.setattr(immich, "_client", lambda *a, **k: Client())
    d = await immich.asset_detail("a")
    assert d["ok"] is False and "connection refused" in d["error"]


async def test_duration_is_read_from_both_units_immich_has_served(rig):
    """A clock string up to v2, an integer of milliseconds from v3.0. The
    ledger column is seconds, and Library shows a sum of it, so the unit
    cannot be left to whichever server answered. Nothing is gated on it --
    which is exactly why reading only the old shape survived a whole major
    version without anybody noticing."""
    from app import immich
    for raw, want in (("00:01:23.500", 83.5),   # v2 and earlier
                      (83500, 83.5),            # v3.0 onwards
                      ("83500", 83.5),          # the same, serialised
                      (0, 0.0),
                      (None, None),             # a still photo
                      ("", None),
                      ("later", None),
                      (True, None)):
        row = immich._normalise({"id": "a", "type": "VIDEO", "duration": raw})
        if want is None:
            assert row["duration"] is None, raw
        else:
            assert row["duration"] == pytest.approx(want), raw


class _Answers:
    """A client that answers each search with the next payload, keeping the
    bodies it was asked with."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.sent: list[dict] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, path, json=None):
        self.sent.append(json)
        payload = (self.payloads[len(self.sent) - 1]
                   if len(self.sent) <= len(self.payloads)
                   else {"assets": {"items": []}})

        class Resp:
            status_code = 200
            @staticmethod
            def json(): return payload
        return Resp()


def _item(n):
    return {"id": f"a{n}", "type": "IMAGE", "originalFileName": f"{n}.jpg",
            "fileCreatedAt": "2026-01-01T00:00:00.000Z",
            "exifInfo": {"fileSizeInByte": 1024}}


async def _scan(immich, monkeypatch, answers):
    monkeypatch.setattr(immich, "_version", (3, 3, 5))
    monkeypatch.setattr(immich, "_client", lambda *a, **k: answers)
    return [row["id"] for page in [p async for p in immich.list_assets()]
            for row in page]


async def test_the_scan_follows_the_page_token_while_there_is_one(rig, monkeypatch):
    from app import immich
    answers = _Answers(
        {"assets": {"items": [_item(1)], "nextPage": "2"}},
        {"assets": {"items": [_item(2)], "nextPage": None, "nextCursor": None}},
    )
    assert await _scan(immich, monkeypatch, answers) == ["a1", "a2"]
    assert [b.get("page") for b in answers.sent] == [1, 2]
    assert not any("cursor" in b for b in answers.sent)


async def test_the_scan_follows_a_cursor_once_the_page_token_goes(rig, monkeypatch):
    """v3.2 deprecated nextPage in favour of nextCursor. The release that
    removes it would otherwise end every scan after one page, and a scan
    that stops early reports a smaller library rather than an error."""
    from app import immich
    answers = _Answers(
        {"assets": {"items": [_item(1)], "nextCursor": "c1"}},
        {"assets": {"items": [_item(2)], "nextCursor": "c2"}},
        {"assets": {"items": [_item(3)]}},
    )
    assert await _scan(immich, monkeypatch, answers) == ["a1", "a2", "a3"]
    # The cursor replaces the page; sending both would be two answers to the
    # same question.
    assert "cursor" not in answers.sent[0] and answers.sent[0]["page"] == 1
    assert [b.get("cursor") for b in answers.sent[1:]] == ["c1", "c2"]
    assert not any("page" in b for b in answers.sent[1:])


async def test_a_token_that_is_not_a_page_number_is_not_counted_with(rig, monkeypatch):
    """`nextPage` is documented as a token. If it ever stops being a number,
    int() would raise in the middle of a scan -- fall through to the cursor
    instead."""
    from app import immich
    answers = _Answers(
        {"assets": {"items": [_item(1)], "nextPage": "eyJpZCI6ICJhMSJ9",
                    "nextCursor": "c1"}},
        {"assets": {"items": [_item(2)]}},
    )
    assert await _scan(immich, monkeypatch, answers) == ["a1", "a2"]
    assert answers.sent[1].get("cursor") == "c1"


async def test_a_repeated_cursor_does_not_scan_for_ever(rig, monkeypatch):
    """Every row behind a repeated cursor is one the ledger already has, so
    the only thing a second pass can add is a loop."""
    from app import immich
    answers = _Answers(
        {"assets": {"items": [_item(1)], "nextCursor": "c1"}},
        {"assets": {"items": [_item(2)], "nextCursor": "c1"}},
        {"assets": {"items": [_item(3)], "nextCursor": "c1"}},
    )
    assert await _scan(immich, monkeypatch, answers) == ["a1", "a2"]
    assert len(answers.sent) == 2


async def test_the_search_body_still_asks_for_the_timeline_with_a_cursor(rig):
    """The filters are deprecated, not gone, and they are what has been
    exercised against a real server. Dropping visibility while changing
    pagination would relay archived, hidden and locked photos to a place
    nothing here can delete from."""
    from app import immich
    body = immich._build_body(1, "2026-09-01T00:00:00Z", major=3,
                              include_archived=False, use_visibility=True,
                              cursor="c1")
    assert body["cursor"] == "c1" and "page" not in body
    assert body["visibility"] == "timeline"
    assert body["takenAfter"] == "2026-09-01T00:00:00Z"
    assert body["withExif"] is True
