"""The Pixel companion, server side.

The companion exists to press a button on the phone. The thing these tests
are really guarding is that it never grows past that: absence of a file is
this system's only proof of backup, so a component whose whole job is to
make files disappear faster must never itself be able to make one
disappear, and must never be believed about whether one was backed up.
"""

import json

import pytest

from conftest import asset, fake_download


def enable(**over):
    from app import settings
    values = {"companion_enabled": True, "companion_auto": True}
    values.update(over)
    return settings.save(values)


def photos(active: bool, remaining: int = 0):
    """A fresh reading of what Google Photos says about itself.

    consider() looks before it frees, so without one of these it asks for a
    look rather than a free-up -- which is the point of the feature, and a
    nuisance in tests that are about something else.
    """
    from app import db
    db.set_meta("companion_backup", json.dumps({
        "active": active, "remaining": remaining, "eta_minutes": 0,
        "detail": "seeded", "at": db.now(), "since": db.now()}))


async def fill_to_cap(monkeypatch, n=3, size=100):
    """Put files in the outbox and cap it so nothing more fits."""
    from app import db, feeder, immich
    db.upsert_assets([asset(i, size=size) for i in range(n)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    return await feeder.top_up(used)


# ---- the invariant ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_successful_run_confirms_nothing(rig, monkeypatch):
    """The report is a note, not evidence.

    If the phone saying "I freed space" could confirm assets, a companion
    that lied -- or simply ran while Google Photos backup was paused --
    would mark the queue backed up with the files still sitting there.
    """
    from app import companion, db

    enable()
    await fill_to_cap(monkeypatch, n=3)
    before = db.counts()

    companion.record({"ok": True, "freed_bytes": 10 ** 9, "items": 3,
                      "detail": "freed 3 items"})

    after = db.counts()
    assert after["confirmed"] == before["confirmed"] == 0
    assert after["queued"] == before["queued"] == 3
    assert len(rig.files()) == 3, "the report must not remove a single file"


@pytest.mark.asyncio
async def test_the_module_never_deletes(rig, monkeypatch):
    """A structural check: nothing here may touch the filesystem."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "companion.py").read_text()
    for forbidden in ("os.remove", "os.unlink", "shutil.rmtree",
                      "unlink(", "empty_outbox", "mark_confirmed"):
        assert forbidden not in src, f"companion.py must not call {forbidden}"


# ---- pairing ------------------------------------------------------------

def test_the_token_is_made_once_and_kept(rig):
    from app import companion
    first = companion.ensure_token()
    assert first and companion.ensure_token() == first


def test_a_rotated_token_stops_working(rig):
    from app import companion
    old = companion.ensure_token()
    new = companion.rotate_token()
    assert new != old
    assert companion.token_ok(new)
    assert not companion.token_ok(old)


@pytest.mark.parametrize("bad", [None, "", "wrong", "x" * 43])
def test_a_wrong_token_is_refused(rig, bad):
    from app import companion
    companion.ensure_token()
    assert not companion.token_ok(bad)


def test_the_phone_endpoints_are_gated_by_token(rig):
    from fastapi.testclient import TestClient
    from app import companion
    from app.main import app

    companion.ensure_token()
    c = TestClient(app)

    assert c.post("/api/companion/poll", json={}).status_code == 401
    r = c.post("/api/companion/poll", json={},
               headers={"X-Companion-Token": companion.ensure_token()})
    assert r.status_code == 200


def test_the_dashboard_endpoints_still_need_a_session(rig):
    from fastapi.testclient import TestClient
    from app import companion
    from app.main import app

    c = TestClient(app)
    # A phone token must not buy access to anything but the phone's routes.
    r = c.post("/api/companion/free",
               headers={"X-Companion-Token": companion.ensure_token()})
    assert r.status_code == 401


def test_the_token_is_stripped_from_a_downloaded_backup(rig):
    """It is the phone's entire authority, so it leaves with the keys."""
    import sqlite3
    from app import backup, companion

    companion.ensure_token()
    name = backup.create()["name"]
    export = backup.export_sanitised(name)

    conn = sqlite3.connect(export)
    try:
        row = conn.execute(
            "SELECT v FROM meta WHERE k='companion_token'").fetchone()
    finally:
        conn.close()
    assert row is None


# ---- deciding whether to ask --------------------------------------------

def test_a_disabled_companion_is_never_asked(rig):
    from app import companion
    companion.request("manual")
    answer = companion.poll({"device": "pixel"})
    assert answer["free_space"] is False
    assert "disabled" in answer["reason"]


def test_nothing_waiting_means_nothing_to_do(rig):
    from app import companion
    enable()
    answer = companion.poll({"device": "pixel"})
    assert answer["free_space"] is False
    assert answer["reason"] == "nothing to do"


@pytest.mark.asyncio
async def test_a_full_outbox_with_work_behind_it_asks_by_itself(rig, monkeypatch):
    from app import companion, db

    enable()
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    # More waiting than will fit.
    db.upsert_assets([asset(i, size=100) for i in range(3, 8)])
    db.set_meta("outbox_used", str(rig.used()))

    # The server decides on its own cycle; the phone only collects.
    photos(active=False)
    companion.consider()
    answer = companion.poll({"device": "pixel", "battery": 90, "charging": True})
    assert answer["free_space"] is True
    assert "outbox full" in answer["reason"]
    assert answer["next_poll_seconds"] == 60


@pytest.mark.asyncio
async def test_the_tail_of_a_finished_library_still_gets_asked_for(rig, monkeypatch):
    """The case this declined for a long time, and the expensive one.

    Once everything is queued there is nothing waiting *behind* the outbox,
    so the old test stopped at its first line and said no. But the files in
    the outbox are not backed up until they disappear, and only a free-up
    makes them disappear -- so the last batch of every run sat on the phone
    until Smart Storage's thirty-day clock reached it.
    """
    from app import companion, db

    enable()
    rig.cap(10_000)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.set_meta("outbox_used", str(rig.used()))
    # Nothing at all is waiting to be sent: the library is fully queued.
    assert db.smallest_sendable(__import__("app").settings.load().eligibility) is None

    photos(active=False)
    companion.consider()
    answer = companion.poll({"device": "pixel", "battery": 90, "charging": True})
    assert answer["free_space"] is True
    assert "nothing behind them" in answer["reason"]


def test_an_empty_outbox_is_never_a_free_up(rig):
    """The cooldown stops it thrashing; this stops it pressing for nothing."""
    from app import companion

    enable(companion_watch_minutes=0)
    assert companion.consider() is None
    assert companion.poll({"device": "pixel"})["free_space"] is False


def test_an_empty_outbox_is_still_looked_in_on(rig):
    """Nothing to clear does not mean nothing to know. Google Photos may
    still have a queue of its own -- and opening it is what keeps it out of
    the standby bucket an app sinks into when nobody opens it."""
    from app import companion

    enable(companion_watch_minutes=30)
    req = companion.consider()
    assert req and req["action"] == companion.LOOK
    answer = companion.poll({"device": "pixel"})
    assert answer["action"] == "look"
    assert answer["free_space"] is False, "a look must never read as a free-up"


@pytest.mark.asyncio
async def test_auto_can_be_switched_off(rig, monkeypatch):
    from app import companion, db

    enable(companion_auto=False)
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.upsert_assets([asset(i, size=100) for i in range(3, 8)])
    db.set_meta("outbox_used", str(rig.used()))

    companion.consider()
    assert companion.poll({"device": "pixel"})["free_space"] is False


@pytest.mark.asyncio
async def test_it_waits_out_the_cooldown_before_asking_again(rig, monkeypatch):
    """One free-up unblocks about one outbox. Asking again immediately just
    wakes the phone for files Google Photos has not uploaded yet."""
    from app import companion, db

    enable(companion_cooldown_minutes=60)
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.upsert_assets([asset(i, size=100) for i in range(3, 8)])
    db.set_meta("outbox_used", str(rig.used()))

    photos(active=False)
    companion.consider()
    assert companion.poll({"device": "pixel"})["free_space"] is True
    companion.record({"ok": True, "detail": "done"})

    assert companion.consider() is None
    again = companion.poll({"device": "pixel"})
    assert again["free_space"] is False
    assert "waiting" in again["reason"]


@pytest.mark.asyncio
async def test_a_flat_battery_is_refused_with_a_reason(rig, monkeypatch):
    from app import companion

    enable(companion_min_battery=30)
    companion.request("manual")

    answer = companion.poll({"device": "pixel", "battery": 12, "charging": False})
    assert answer["free_space"] is False
    assert "12%" in answer["reason"]


@pytest.mark.asyncio
async def test_a_charging_phone_ignores_the_battery_floor(rig):
    from app import companion

    enable(companion_min_battery=30)
    companion.request("manual")
    answer = companion.poll({"device": "pixel", "battery": 12, "charging": True})
    assert answer["free_space"] is True


# ---- handing out one run at a time --------------------------------------

def test_a_manual_request_is_handed_out_once(rig):
    from app import companion

    enable(companion_auto=False)
    companion.request("manual")

    first = companion.poll({"device": "pixel"})
    assert first["free_space"] is True

    second = companion.poll({"device": "pixel"})
    assert second["free_space"] is False, "two phones must not both run it"
    assert "in progress" in second["reason"]


def test_an_abandoned_run_does_not_block_forever(rig):
    """The phone can reboot mid-run. Without a timeout the in-flight marker
    would sit there and every later request would be refused."""
    import json
    from datetime import datetime, timedelta, timezone
    from app import companion, db

    enable(companion_auto=False)
    companion.request("manual")
    assert companion.poll({"device": "pixel"})["free_space"] is True

    stale = (datetime.now(timezone.utc)
             - timedelta(minutes=companion.RUN_TIMEOUT_MINUTES + 1))
    db.set_meta("companion_inflight",
                json.dumps({"id": "abc", "at": stale.isoformat()}))

    companion.request("manual")
    assert companion.poll({"device": "pixel"})["free_space"] is True


def test_reporting_clears_the_run(rig):
    from app import companion

    enable(companion_auto=False)
    companion.request("manual")
    handed = companion.poll({"device": "pixel"})
    companion.record({"request_id": handed["request_id"], "ok": True,
                      "items": 12, "freed_bytes": 5000, "detail": "freed 12"})

    snap = companion.snapshot()
    assert snap["running"] is False
    assert snap["last_run"]["items"] == 12
    assert snap["state"] == "ok"


# ---- what the phone is told ---------------------------------------------

def test_the_labels_fall_back_to_the_built_in_list(rig):
    from app import companion
    enable(companion_labels="", companion_confirm_labels="")
    answer = companion.poll({"device": "pixel"})
    assert answer["labels"] == list(companion.DEFAULT_LABELS)
    assert answer["confirm_labels"] == list(companion.DEFAULT_CONFIRM)


def test_custom_labels_reach_the_phone(rig):
    """So a Photos rename is a settings change, not a new APK."""
    from app import companion
    enable(companion_labels="Speicher freigeben, Free up space",
           companion_confirm_labels="Freigeben")
    answer = companion.poll({"device": "pixel"})
    assert answer["labels"] == ["speicher freigeben", "free up space"]
    assert answer["confirm_labels"] == ["freigeben"]


def test_an_idle_phone_is_told_to_come_back_later(rig):
    from app import companion
    enable(companion_idle_poll_minutes=30)
    assert companion.poll({"device": "pixel"})["next_poll_seconds"] == 1800


def test_a_phone_on_the_charger_is_asked_to_come_back_sooner(rig):
    """This interval is the latency of "free up now".

    The phone dials out and the relay never dials in, so a command waits
    for the phone to ask. A plugged-in phone can afford to ask often, and
    the shelf phone this was built for is always plugged in -- it was
    waiting up to half an hour to be told to do something.
    """
    from app import companion
    enable(companion_idle_poll_minutes=30, companion_charging_poll_minutes=1)
    answer = companion.poll({"device": "pixel", "charging": True})
    assert answer["next_poll_seconds"] == 60


def test_a_phone_on_battery_keeps_the_slower_interval(rig):
    """The fast interval is bought with the phone's battery, so it is only
    spent while something else is paying."""
    from app import companion
    enable(companion_idle_poll_minutes=30, companion_charging_poll_minutes=1)
    answer = companion.poll({"device": "pixel", "charging": False})
    assert answer["next_poll_seconds"] == 1800


def test_a_switched_off_companion_does_not_wake_the_phone_every_minute(rig):
    """Off is the one state worth being slow about: there is nothing to
    hear, and there will not be until somebody changes a setting."""
    from app import companion
    from app import settings
    settings.save({"companion_enabled": False,
                   "companion_idle_poll_minutes": 30,
                   "companion_charging_poll_minutes": 1})
    answer = companion.poll({"device": "pixel", "charging": True})
    assert answer["next_poll_seconds"] == 1800


def test_the_interval_is_never_faster_than_a_minute(rig):
    """A phone is not a thing to put in a hot loop by typing a zero."""
    from app import companion
    enable(companion_charging_poll_minutes=0, companion_idle_poll_minutes=0)
    assert companion.poll({"device": "p", "charging": True})["next_poll_seconds"] == 60
    assert companion.poll({"device": "p", "charging": False})["next_poll_seconds"] == 60


@pytest.mark.asyncio
async def test_the_ask_is_made_and_logged_without_the_phone_saying_anything(
        rig, monkeypatch):
    """The failure that made this unreadable.

    The decision used to live inside poll(), so it was evaluated *by the
    phone asking*. A phone that had gone quiet meant the condition was
    never tested, request() was never called -- and request() is what
    writes the log line. Fifteen hours of a stalled pipeline produced no
    entry of any kind, so there was nothing to diagnose from but an
    absence.
    """
    from app import companion, db

    enable()
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.set_meta("outbox_used", str(rig.used()))

    # No poll(). The phone is asleep and says nothing at all.
    photos(active=False)
    assert companion.consider() is not None
    lines = [e["msg"] for e in db.recent_events(50)]
    assert any("free-up requested" in m for m in lines), lines


def test_asking_twice_over_is_not_a_thing(rig):
    """consider() runs every feeder cycle -- a minute or two apart."""
    from app import companion

    enable()
    companion.request("manual")
    assert companion.consider() is None


def test_the_phone_is_not_told_to_linger_unless_the_server_says_so(rig):
    from app import companion

    enable()
    assert companion.poll({"device": "pixel"})["dwell_seconds"] == 0

    enable(companion_dwell_enabled=True, companion_dwell_seconds=90)
    assert companion.poll({"device": "pixel"})["dwell_seconds"] == 90

    # Switched off again mid-flight: the next check-in stops lingering.
    enable(companion_dwell_enabled=False, companion_dwell_seconds=90)
    assert companion.poll({"device": "pixel"})["dwell_seconds"] == 0


def test_the_backup_panel_labels_reach_the_phone(rig):
    from app import companion

    enable()
    assert companion.poll({"device": "p"})["backup_labels"] == list(
        companion.DEFAULT_BACKUP)
    enable(companion_backup_labels="Sichern, Hochladen")
    assert companion.poll({"device": "p"})["backup_labels"] == [
        "sichern", "hochladen"]


@pytest.mark.asyncio
async def test_what_google_photos_says_about_itself_confirms_nothing(
        rig, monkeypatch):
    """The tempting one, and the one that would forge the proof.

    "Backing up" and its absence are read off somebody else's screen. If a
    string like that could mark an asset backed up, a renamed label would
    silently confirm a library that never left the house.
    """
    from app import companion, db

    enable()
    await fill_to_cap(monkeypatch, n=3, size=100)
    before = db.counts()

    companion.record({"ok": True, "detail": "done", "backup": {
        "active": False, "remaining": 0, "detail": "Backup complete"}})

    assert db.counts() == before
    assert companion.snapshot()["backup"]["active"] is False


def test_a_backup_that_stops_moving_is_reported_as_stuck(rig):
    """A slow backup and a stopped one look identical from the server. The
    count not moving between runs is the only difference there is."""
    from datetime import datetime, timedelta, timezone
    from app import alerts, companion, db

    enable(companion_cooldown_minutes=60)
    companion.poll({"device": "pixel"})
    for _ in range(2):
        companion.record({"ok": True, "detail": "x", "backup": {
            "active": True, "remaining": 250, "eta_minutes": 146,
            "detail": "Backing up 250 photos"}})

    keys = {a["key"] for a in alerts.evaluate()}
    assert "companion_backup_stuck" not in keys, "not stuck yet — give it time"

    stuck = json.loads(db.get_meta("companion_backup"))
    stuck["since"] = (datetime.now(timezone.utc)
                      - timedelta(hours=5)).isoformat()
    db.set_meta("companion_backup", json.dumps(stuck))
    assert "companion_backup_stuck" in {a["key"] for a in alerts.evaluate()}


def test_a_backup_still_moving_is_not_reported_as_stuck(rig):
    from app import alerts, companion

    enable()
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "x", "backup": {
        "active": True, "remaining": 250, "detail": "Backing up 250 photos"}})
    companion.record({"ok": True, "detail": "x", "backup": {
        "active": True, "remaining": 180, "detail": "Backing up 180 photos"}})

    assert "companion_backup_stuck" not in {a["key"] for a in alerts.evaluate()}


# ---- look before you press ----------------------------------------------

@pytest.mark.asyncio
async def test_it_looks_before_it_presses(rig, monkeypatch):
    """With no idea what Google Photos is doing, find out first.

    A free-up run while Photos is mid-upload clears what it has got through
    and leaves the rest. That is not harmful, but it wakes the phone for a
    fraction of the job -- and it makes the leftovers meaningless, because
    they could be unbacked files or simply the next ones in Photos' queue.
    """
    from app import companion, db

    enable()
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.set_meta("outbox_used", str(rig.used()))

    req = companion.consider()
    assert req and req["action"] == companion.LOOK


@pytest.mark.asyncio
async def test_a_backup_in_flight_holds_the_free_up_back(rig, monkeypatch):
    from app import companion, db

    enable()
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.set_meta("outbox_used", str(rig.used()))

    photos(active=True, remaining=250)
    assert companion.consider() is None, "pressed the button mid-upload"

    photos(active=False)
    req = companion.consider()
    assert req and req["action"] == companion.FREE


def test_a_queued_look_does_not_announce_itself_as_a_free_up(rig):
    """Two instructions now, and the dashboard has to say which is coming."""
    from app import companion

    enable()
    companion.poll({"device": "pixel"})     # so it is not "never seen" instead
    companion.request("manual", "", companion.LOOK)
    assert "look in on" in companion.snapshot()["line"]

    companion.cancel_request()
    companion.request("manual")
    assert "free space" in companion.snapshot()["line"]


def test_a_look_does_not_start_the_cooldown(rig):
    """Otherwise the watching would be what stops the work: every look
    would buy Google Photos another hour of not being asked."""
    from app import companion

    enable(companion_cooldown_minutes=60)
    companion.request("manual", "", companion.LOOK)
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "looked", "action": "look"})

    assert companion.snapshot()["next_free_minutes"] == 0


def test_how_long_it_stayed_is_reported_not_assumed(rig):
    """Whether the phone dwelled at all used to be answerable only by
    reading its wake locks over adb."""
    from app import companion

    enable(companion_dwell_enabled=True, companion_dwell_seconds=120)
    companion.request("manual")
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "done", "freed_bytes": 10,
                      "dwelled_seconds": 118})

    assert companion.snapshot()["last_run"]["dwelled_seconds"] == 118


def test_the_dashboard_is_told_whether_the_phone_understands_the_instruction(rig):
    """It was inferred from the version string, so an instruction a phone
    was too old for simply vanished."""
    from app import companion

    enable(companion_dwell_enabled=True)
    companion.poll({"device": "pixel", "app_version": "2.3.0"})
    assert companion.snapshot()["dwell"]["understood"] is False

    companion.poll({"device": "pixel", "app_version": "2.5.0",
                    "features": ["look", "dwell", "backup"]})
    assert companion.snapshot()["dwell"]["understood"] is True


# ---- did the claim hold? ------------------------------------------------

def _age(meta_key, **delta):
    """Push a stored timestamp back, so the delay does not have to be
    waited out."""
    from datetime import datetime, timedelta, timezone
    from app import db
    d = json.loads(db.get_meta(meta_key))
    d["at"] = (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()
    db.set_meta(meta_key, json.dumps(d))


@pytest.mark.asyncio
async def test_a_claim_that_does_not_hold_is_reported(rig, monkeypatch):
    """Google Photos said it had finished, the button was pressed, and
    nothing at all was confirmed afterwards. Those files are on the phone
    and not in the cloud, whatever its screen said."""
    from app import companion, db

    enable()
    await fill_to_cap(monkeypatch, n=3, size=100)
    companion.request("manual")
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "nothing to free up", "backup":
                      {"active": False, "detail": "Backup complete"}})

    _age("companion_audit", minutes=companion.AUDIT_DELAY_MINUTES + 5)
    companion.audit()

    held = json.loads(db.get_meta("companion_unbacked"))
    assert held["count"] == 3
    assert any("stayed in the outbox" in e["msg"] for e in db.recent_events(20))


@pytest.mark.asyncio
async def test_a_claim_that_holds_clears_the_finding(rig, monkeypatch):
    """Confirmations, not the outbox count: top_up() adds files on its own
    cycle, so an outbox the same size may have turned over completely."""
    from app import companion, db

    enable()
    await fill_to_cap(monkeypatch, n=3, size=100)
    companion.request("manual")
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "freed", "freed_bytes": 99,
                      "backup": {"active": False}})

    rig.deliver(3)          # Google Photos took them; the files are gone
    from app import feeder
    feeder.reconcile()

    _age("companion_audit", minutes=companion.AUDIT_DELAY_MINUTES + 5)
    companion.audit()
    assert not db.get_meta("companion_unbacked")


@pytest.mark.asyncio
async def test_one_remainder_is_not_yet_an_alert(rig, monkeypatch):
    """Photos' media scanner lags Syncthing, so files that arrived minutes
    ago may not have been looked at yet. One remainder proves nothing; the
    same one, cycle after cycle, does."""
    from datetime import datetime, timedelta, timezone
    from app import alerts, companion, db

    enable(companion_cooldown_minutes=60)
    await fill_to_cap(monkeypatch, n=3, size=100)
    companion.request("manual")
    companion.poll({"device": "pixel"})
    companion.record({"ok": True, "detail": "x", "backup": {"active": False}})
    _age("companion_audit", minutes=companion.AUDIT_DELAY_MINUTES + 5)
    companion.audit()

    assert "companion_unbacked" not in {a["key"] for a in alerts.evaluate()}

    held = json.loads(db.get_meta("companion_unbacked"))
    held["since"] = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    db.set_meta("companion_unbacked", json.dumps(held))
    assert "companion_unbacked" in {a["key"] for a in alerts.evaluate()}


def test_the_check_in_is_recorded(rig):
    from app import companion
    enable()
    companion.poll({"device": "pixel-1", "battery": 55, "charging": True,
                    "app_version": "1.0.0", "free_bytes": 12345})
    snap = companion.snapshot()
    assert snap["device"]["device"] == "pixel-1"
    assert snap["device"]["battery"] == 55
    assert snap["seen_minutes"] is not None


# ---- going quiet --------------------------------------------------------

def test_a_phone_that_stops_checking_in_raises_an_alert(rig):
    """The symptom is indistinguishable from Google Photos being slow: the
    outbox just sits full. Nothing else would say why."""
    from datetime import datetime, timedelta, timezone
    from app import alerts, companion, db

    enable(companion_offline_minutes=60)
    companion.poll({"device": "pixel"})
    stale = datetime.now(timezone.utc) - timedelta(hours=20)
    db.set_meta("companion_seen_at", stale.isoformat())

    keys = {a["key"] for a in alerts.evaluate()}
    assert "companion_offline" in keys


def test_a_failed_run_says_the_button_may_have_moved(rig):
    from app import alerts, companion

    enable()
    companion.poll({"device": "pixel"})
    companion.record({"ok": False, "detail": "could not find the button"})

    fired = [a for a in alerts.evaluate() if a["key"] == "companion_failed"]
    assert fired, "a failed free-up has to surface somewhere"
    assert "renamed" in fired[0]["message"]


def test_no_companion_alerts_when_it_is_switched_off(rig):
    from app import alerts
    keys = {a["key"] for a in alerts.evaluate()}
    assert "companion_offline" not in keys
    assert "companion_failed" not in keys
