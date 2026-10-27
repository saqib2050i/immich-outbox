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
    assert phone["last_seen"] == "2026-09-20T10:00:00+00:00", \
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


# ---- the answer is used, not only drawn ---------------------------------

import json
import time
from datetime import datetime, timedelta, timezone


async def test_one_answer_is_shared_by_every_viewer(rig, monkeypatch):
    """`/rest/db/status` is documented by Syncthing as expensive, and every
    open dashboard tab called it every twenty seconds."""
    from app import syncthing
    configured()
    seen = []
    fake_syncthing(monkeypatch, WHOLE, seen)
    await syncthing.status()
    calls = len(seen)
    await syncthing.status()
    assert len(seen) == calls, "a second viewer asked Syncthing again"
    await syncthing.status(fresh=True)
    assert len(seen) == 2 * calls, "housekeeping must be able to force a refresh"
    assert syncthing.cached() is not None


@pytest.mark.parametrize("folder,says", [
    ({"type": "sendonly"}, "Send Only"),
    ({"type": "sendreceive", "ignoreDelete": True}, "Ignore Delete"),
])
async def test_a_folder_that_drops_the_phones_deletions_is_named(rig, monkeypatch,
                                                                  folder, says):
    """Confirmation is a file disappearing. A Send Only folder, or one
    ignoring deletes, never applies the phone's deletion, so nothing is
    ever confirmed and every figure looks like Google Photos being slow."""
    from app import alerts, syncthing
    configured()
    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/config/folders/abcd-1234": dict(folder, devices=[
            {"deviceID": ME}, {"deviceID": PHONE}])}))
    out = await syncthing.status()
    assert says in out["folder"]["problem"]
    assert "syncthing_folder" in {a["key"] for a in alerts.evaluate()}


async def test_a_send_and_receive_folder_is_not_a_problem(rig, monkeypatch):
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/config/folders/abcd-1234": {"type": "sendreceive", "devices": [
            {"deviceID": ME}, {"deviceID": PHONE}]}}))
    assert (await syncthing.status())["folder"]["problem"] is None


@pytest.mark.parametrize("raw,clean", [
    ("2026-10-26T10:00:00.123456789+05:00", "2026-10-26T10:00:00.123456+05:00"),
    ("1970-01-01T00:00:00Z", None),
    (None, None),
])
async def test_last_seen_is_a_time_python_and_browsers_can_read(rig, raw, clean):
    """Syncthing writes nanoseconds, and "never" as the epoch."""
    from app import syncthing
    assert syncthing._iso(raw) == clean
    if clean:
        datetime.fromisoformat(clean)


# ---- one file: does the phone have it? ------------------------------------

FILE = {"global": {"size": 10, "deleted": False}, "local": {"size": 10}}


async def test_availability_is_read_as_the_objects_syncthing_sends(rig, monkeypatch):
    """It is `[{"id": ..., "fromTemporary": ...}]`. Read as a list of
    strings it found nobody, so a trace said the phone did not have files
    the phone was holding."""
    from app import syncthing
    configured()
    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/db/file": dict(FILE, availability=[{"id": PHONE,
                                                   "fromTemporary": False}]),
        "/rest/db/remoteneed": {"files": [], "page": 1, "perpage": 5000}}))
    out = await syncthing.file_holders("PXL_1.jpg")
    assert out["other_devices_with_it"] == ["Pixel"]
    assert out["devices"][0]["has"] is True


async def test_an_offline_phone_still_gets_an_answer_from_its_index(rig, monkeypatch):
    """`availability` only names connected devices. A phone that is away is
    answered from what it last told Syncthing it needs."""
    from app import syncthing
    configured()
    offline = {"connections": {PHONE: {"connected": False}}}
    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/system/connections": offline,
        "/rest/db/file": dict(FILE, availability=[]),
        "/rest/db/remoteneed": {"files": [{"name": "PXL_1.jpg"}]}}))
    out = await syncthing.file_holders("PXL_1.jpg")
    assert out["devices"] == [{"name": "Pixel", "connected": False, "has": False}]

    fake_syncthing(monkeypatch, dict(WHOLE, **{
        "/rest/system/connections": offline,
        "/rest/db/file": dict(FILE, availability=[]),
        "/rest/db/remoteneed": {"files": [{"name": "other.jpg"}]}}))
    out = await syncthing.file_holders("PXL_1.jpg")
    assert out["devices"][0]["has"] is True


# ---- the alerts that were guessing -----------------------------------------

def seed(devices, problem=None):
    from app import syncthing
    syncthing._cache.update(at=time.monotonic(), data={
        "configured": True, "ok": True, "devices": devices,
        "folder": {"problem": problem}})


def phone(**kw):
    d = {"id": PHONE, "name": "Pixel", "connected": True, "paused": False,
         "last_seen": None, "need_items": 0}
    d.update(kw)
    return d


def one_in_the_outbox():
    from app import db
    from conftest import asset
    db.upsert_assets([asset(0)])
    db.mark_queued(["asset-0"])


def unbacked(hours=6):
    from app import db, settings
    settings.save({"companion_enabled": True})
    db.set_meta("companion_unbacked", json.dumps({
        "count": 9, "since": (datetime.now(timezone.utc)
                              - timedelta(hours=hours)).isoformat()}))


def alert(key):
    from app import alerts
    return next((a for a in alerts.evaluate() if a["key"] == key), None)


async def test_files_the_phone_holds_point_at_the_media_scanner(rig):
    """Syncthing confirms the phone has every file and Photos still will
    not take them: that is Android's media scanner, and a restart is the
    one fix measured to work."""
    unbacked()
    seed([phone()])
    a = alert("companion_unbacked")
    assert a["title"] == "Files the phone is holding but has not backed up"
    assert "holds every file" in a["message"] and "media scanner" in a["message"]


async def test_files_the_phone_never_got_are_not_called_held(rig):
    """The title used to say "the phone is holding" without anybody having
    asked whether it was."""
    unbacked()
    seed([phone(need_items=9)])
    a = alert("companion_unbacked")
    assert a["title"] == "Files the phone never received"
    assert "missing 9" in a["message"] and "delivery, not backup" in a["message"]


async def test_without_syncthing_the_alert_says_what_would_settle_it(rig):
    unbacked()
    a = alert("companion_unbacked")
    assert a["title"] == "Files the phone is holding but has not backed up"
    assert "Connecting Syncthing" in a["message"]


async def test_a_phone_away_for_hours_is_its_own_alert(rig):
    one_in_the_outbox()
    seen = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    seed([phone(connected=False, last_seen=seen)])
    a = alert("syncthing_away")
    assert a and "Not seen for 8h" in a["message"]

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    seed([phone(connected=False, last_seen=recent)])
    assert alert("syncthing_away") is None, "an hour is Wi-Fi, not a fault"

    seed([phone(connected=False, paused=True, last_seen=seen)])
    assert alert("syncthing_away") is None, "pausing is a decision, not a fault"


async def test_a_phone_that_stops_receiving_is_named_once_it_has_stopped(rig):
    from app import db
    one_in_the_outbox()
    seed([phone(need_items=4)])
    assert alert("syncthing_behind") is None, "large files take a while"
    db.set_meta("sync_behind_since", (datetime.now(timezone.utc)
                                      - timedelta(hours=4)).isoformat())
    assert "missing 4" in alert("syncthing_behind")["message"]
    seed([phone()])
    alert("syncthing_behind")
    assert not db.get_meta("sync_behind_since"), "caught up means start again"


async def test_a_stuck_outbox_says_which_side_stopped(rig):
    from app import alerts
    seed([phone(connected=False, last_seen=None)])
    assert "has never been seen" in alerts._where_it_stops(alerts._phones())
    seed([phone()])
    assert "hold-up is Google Photos" in alerts._where_it_stops(alerts._phones())
    from app import syncthing
    syncthing.reset()
    assert alerts._where_it_stops(alerts._phones()) == ""


async def test_the_page_can_open_both_apps_without_seeing_a_key(rig):
    from fastapi.testclient import TestClient
    from app import auth, main, settings
    settings.save({"syncthing_url": "http://192.168.1.2:8384",
                   "syncthing_api_key": "secret-key"})
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.new_session())
    d = c.get("/api/status").json()
    assert d["syncthing"] == {"url": "http://192.168.1.2:8384"}
    assert "secret-key" not in json.dumps(d)
