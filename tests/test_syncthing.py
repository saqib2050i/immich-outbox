"""Whether the phone actually has the files.

The outbox empties only when Google Photos takes a file off the phone, and
it can only do that if the phone has it. "9 files, 70 hours" reads exactly
the same whether the phone is holding them and Photos will not upload, or
the phone went offline on Tuesday and never received them -- and those two
need opposite fixes. Counting connected devices answers neither.

Nothing here may become load-bearing: Syncthing is optional, and a Syncthing
that cannot be reached must cost nothing but this panel.
"""

import httpx
import pytest

pytestmark = pytest.mark.asyncio

PHONE = "AAAAAAA-BBBBBBB-CCCCCCC"
ME = "ZZZZZZZ-YYYYYYY-XXXXXXX"


def fake_syncthing(monkeypatch, routes, seen=None):
    """A Syncthing whose answers are whatever the test says they are."""
    from app import syncthing

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        for path, body in routes.items():
            if request.url.path == path:
                if isinstance(body, int):
                    return httpx.Response(body)
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    def client(cfg):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://syncthing.test")
    monkeypatch.setattr(syncthing, "_client", client)


def configured():
    from app import settings
    settings.save({"syncthing_url": "http://syncthing.test",
                   "syncthing_api_key": "key", "syncthing_folder": "abcd-1234"})


WHOLE = {
    "/rest/system/connections": {"connections": {
        PHONE: {"connected": True, "paused": False, "address": "192.168.1.9:22000"}}},
    "/rest/system/status": {"myID": ME},
    "/rest/config/folders/abcd-1234": {"devices": [{"deviceID": ME},
                                                   {"deviceID": PHONE}]},
    "/rest/config/devices": [{"deviceID": PHONE, "name": "Pixel"},
                             {"deviceID": ME, "name": "unraid"}],
    "/rest/stats/device": {PHONE: {"lastSeen": "2026-09-20T10:00:00Z"}},
    "/rest/db/status": {"state": "idle", "globalBytes": 1000, "needBytes": 0,
                        "errors": 0},
    "/rest/db/completion": {"completion": 100, "needBytes": 0, "needItems": 0},
}


async def test_the_phone_is_named_and_its_state_reported(rig, monkeypatch):
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, WHOLE)
    out = await syncthing.status()
    assert out["ok"] is True
    phone = out["devices"][0]
    assert phone["name"] == "Pixel" and phone["connected"] is True
    assert phone["completion"] == 100 and not phone["need_items"]


async def test_this_machine_is_not_listed_as_a_device_to_worry_about(rig, monkeypatch):
    """It is the one holding the outbox. Reporting it as a peer that might
    be behind is how a panel about the phone comes to have two rows."""
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, WHOLE)
    out = await syncthing.status()
    assert [d["id"] for d in out["devices"]] == [PHONE]


async def test_a_phone_that_is_behind_says_by_how_much(rig, monkeypatch):
    """The case that explains an outbox that will not drain."""
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/system/connections": {"connections": {
            PHONE: {"connected": False, "paused": False}}},
        "/rest/db/completion": {"completion": 61.5, "needBytes": 412000000,
                                "needItems": 9}}))
    out = await syncthing.status()
    phone = out["devices"][0]
    assert phone["connected"] is False
    assert phone["need_items"] == 9 and phone["completion"] == 61.5
    assert phone["last_seen"] == "2026-09-20T10:00:00Z", \
        "when it was last seen is the next question somebody asks"


async def test_an_older_syncthing_is_read_through_its_whole_config(rig, monkeypatch):
    """The config API changed. Falling back keeps this working rather than
    reporting a phone that is not there."""
    from app import syncthing
    configured()
    routes = {k: v for k, v in WHOLE.items()
              if k not in ("/rest/config/folders/abcd-1234", "/rest/config/devices")}
    routes["/rest/system/config"] = {
        "devices": [{"deviceID": PHONE, "name": "Pixel"}],
        "folders": [{"id": "abcd-1234",
                     "devices": [{"deviceID": ME}, {"deviceID": PHONE}]}]}
    fake_syncthing(monkeypatch, routes)
    out = await syncthing.status()
    assert [d["name"] for d in out["devices"]] == ["Pixel"]


async def test_a_wrong_folder_id_says_so_and_still_reports_the_rest(rig, monkeypatch):
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, dict(WHOLE, **{"/rest/db/status": 404}))
    out = await syncthing.status()
    assert out["ok"] is True
    assert "folder id" in out["error"]


async def test_a_rejected_key_is_not_reported_as_a_phone_problem(rig, monkeypatch):
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, {"/rest/system/connections": 403})
    out = await syncthing.status()
    assert out["ok"] is False and out["error"] == "API key rejected"
    assert out["devices"] == []


async def test_syncthing_being_down_costs_nothing_but_this_panel(rig, monkeypatch):
    """It is optional, and the relay works without it."""
    from app import syncthing

    def client(cfg):
        def boom(request):
            raise httpx.ConnectError("no route to host")
        return httpx.AsyncClient(transport=httpx.MockTransport(boom),
                                 base_url="http://syncthing.test")
    configured()
    monkeypatch.setattr(syncthing, "_client", client)
    out = await syncthing.status()
    assert out["ok"] is False and out["error"]


async def test_nothing_is_asked_when_it_is_not_configured(rig, monkeypatch):
    from app import settings, syncthing
    settings.save({"syncthing_url": "", "syncthing_api_key": ""})
    asked = []
    fake_syncthing(monkeypatch, WHOLE, seen=asked)
    assert await syncthing.status() == {"configured": False}
    assert asked == []
