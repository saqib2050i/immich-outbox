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

    answer = companion.poll({"device": "pixel", "battery": 90, "charging": True})
    assert answer["free_space"] is True
    assert "outbox full" in answer["reason"]
    assert answer["next_poll_seconds"] == 60


@pytest.mark.asyncio
async def test_room_in_the_outbox_is_not_a_reason_to_wake_the_phone(rig, monkeypatch):
    from app import companion, db

    enable()
    rig.cap(10_000)
    await fill_to_cap(monkeypatch, n=1, size=100)
    db.upsert_assets([asset(9, size=100)])
    db.set_meta("outbox_used", str(rig.used()))

    answer = companion.poll({"device": "pixel", "battery": 90, "charging": True})
    assert answer["free_space"] is False


@pytest.mark.asyncio
async def test_auto_can_be_switched_off(rig, monkeypatch):
    from app import companion, db

    enable(companion_auto=False)
    rig.cap(300)
    await fill_to_cap(monkeypatch, n=3, size=100)
    db.upsert_assets([asset(i, size=100) for i in range(3, 8)])
    db.set_meta("outbox_used", str(rig.used()))

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

    assert companion.poll({"device": "pixel"})["free_space"] is True
    companion.record({"ok": True, "detail": "done"})

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

    enable(companion_offline_hours=12)
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
