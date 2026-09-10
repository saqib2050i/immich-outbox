"""Sending a month that is already backed up.

Everything else in this system refuses to touch a confirmed asset -- that
is invariant 4 -- so the one path that does needs holding to its promises.
It exists for a month deleted from Google Photos: nothing else can put it
back, because a scan will not touch an existing row and confirmation is
permanent.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


def confirmed_in(month: str) -> int:
    return db_module().connect().execute(
        "SELECT COUNT(*) n FROM assets WHERE state='confirmed' "
        "AND substr(taken_at,1,7)=?", (month,)).fetchone()["n"]


def db_module():
    from app import db
    return db


async def a_confirmed_month(rig, monkeypatch, month="2026-05", n=3, base=0):
    """Send some files and let the phone clear them, the honest way.

    `base` offsets the asset ids: upsert_assets is INSERT OR IGNORE, so two
    months built from the same range would silently share rows and the
    second would appear to seed nothing.
    """
    from app import db, feeder, immich
    db.upsert_assets([asset(base + i, size=100, taken=f"{month}-0{i+1}")
                      for i in range(n)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    rig.deliver(n)                      # Smart Storage clears them
    feeder.reconcile()
    assert confirmed_in(month) == n
    return month


# ---- the default still refuses -----------------------------------------

async def test_a_normal_month_send_leaves_confirmed_alone(rig, monkeypatch):
    """Invariant 4 is the default and stays the default."""
    from app import db
    month = await a_confirmed_month(rig, monkeypatch)

    assert db.force_send_month(month) == 0
    assert db.counts()["confirmed"] == 3


async def test_the_route_does_not_resend_unless_asked(rig, monkeypatch):
    from app import db, main
    month = await a_confirmed_month(rig, monkeypatch)

    out = await main.month_send({"month": month})
    assert out["queued"] == 0
    assert db.counts()["confirmed"] == 3


# ---- and the deliberate path works --------------------------------------

async def test_resend_takes_back_a_confirmed_month(rig, monkeypatch):
    from app import db
    month = await a_confirmed_month(rig, monkeypatch)

    assert db.force_send_month(month, resend=True) == 3
    counts = db.counts()
    assert counts["confirmed"] == 0
    assert counts["pending"] == 3


async def test_resend_clears_the_delivery_record(rig, monkeypatch):
    """A row saying 'pending' while still carrying confirmed_at and an
    outbox_name points at a file that is gone and a backup being redone."""
    from app import db
    month = await a_confirmed_month(rig, monkeypatch)
    db.force_send_month(month, resend=True)

    for row in db.connect().execute(
            "SELECT state, confirmed_at, sent_at, outbox_name, seen_on_phone "
            "FROM assets"):
        assert row["state"] == "pending"
        assert row["confirmed_at"] is None
        assert row["sent_at"] is None
        assert row["outbox_name"] is None
        assert row["seen_on_phone"] == 0


async def test_a_resent_month_actually_goes_out_again(rig, monkeypatch):
    """The point of the whole exercise."""
    from app import db, feeder, immich
    month = await a_confirmed_month(rig, monkeypatch)
    assert not rig.files(), "the phone cleared them"

    db.force_send_month(month, resend=True)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3
    assert len(rig.files()) == 3, "they are in the outbox again"


async def test_resend_only_touches_the_month_asked_for(rig, monkeypatch):
    from app import db
    await a_confirmed_month(rig, monkeypatch, month="2026-05", n=3, base=0)
    await a_confirmed_month(rig, monkeypatch, month="2026-06", n=2, base=50)
    assert db.counts()["confirmed"] == 5

    assert db.force_send_month("2026-05", resend=True) == 3
    counts = db.counts()
    assert counts["confirmed"] == 2, "June was not asked for"
    assert counts["pending"] == 3


async def test_resend_is_reachable_through_the_route(rig, monkeypatch):
    from app import db, immich, main
    month = await a_confirmed_month(rig, monkeypatch)
    monkeypatch.setattr(immich, "stream_original", fake_download())

    out = await main.month_send({"month": month, "resend": True})
    assert out["queued"] == 3
    assert db.counts()["confirmed"] == 0


# ---- excluding a month from Library -------------------------------------

async def test_a_month_can_be_excluded_and_brought_back(rig):
    """What replaces sweeping the backlog: the decision sits beside the
    month it is about, and sending the month is the undo."""
    from app import db
    db.upsert_assets([asset(i, taken=f"2026-07-0{i+1}") for i in range(4)])

    assert db.dismiss_waiting(month="2026-07") == 4
    assert db.counts()["skipped"] == 4
    assert db.counts()["pending"] == 0

    assert db.force_send_month("2026-07") == 4
    assert db.counts()["skipped"] == 0
    assert db.counts()["pending"] == 4


async def test_excluding_a_month_never_touches_what_is_backed_up(rig, monkeypatch):
    """Dismissal is about what has not gone yet; a confirmed row is a record
    that it did, and losing that would re-send the whole month later."""
    from app import db
    month = await a_confirmed_month(rig, monkeypatch)

    assert db.dismiss_waiting(month=month) == 0
    assert db.counts()["confirmed"] == 3


# ---- the number an action reports must match the number shown -----------

@pytest.mark.asyncio
async def test_sending_a_month_ignores_what_immich_no_longer_has(rig):
    """month_detail excludes stranded rows and force_send_month did not, so
    "Send the whole month (53)" queued 53 where the screen counted 33. The
    extra 20 could never be claimed, so they sat pending for good -- the
    ghost count again, in the control Library is built around."""
    from app import db

    db.upsert_assets([asset(i, taken="2026-11-05") for i in range(10)])
    # Immich returns only the first four on a full pass.
    db.mark_missing({f"asset-{i}" for i in range(4)})
    assert db.counts()["missing"] == 6

    shown = sum(g["remaining"] for g in db.month_detail("2026-11")["groups"])
    queued = db.force_send_month("2026-11")
    assert shown == 4
    assert queued == shown, \
        f"the button would report {queued} against a screen showing {shown}"


@pytest.mark.asyncio
async def test_forcing_by_id_ignores_what_immich_no_longer_has(rig):
    from app import db
    db.upsert_assets([asset(i) for i in range(4)])
    db.mark_missing({"asset-0", "asset-1"})

    assert db.force_send(ids=["asset-0", "asset-2"]) == 1, \
        "only the one Immich still has"


@pytest.mark.asyncio
async def test_excluding_a_month_ignores_what_immich_no_longer_has(rig):
    """Otherwise it reports dismissing more than the screen offered."""
    from app import db
    db.upsert_assets([asset(i, taken="2026-11-05") for i in range(10)])
    db.mark_missing({f"asset-{i}" for i in range(4)})

    assert db.dismiss_waiting(month="2026-11") == 4


# ---- "N left" has to say whether N is going anywhere --------------------

@pytest.mark.asyncio
async def test_a_month_says_whether_its_remainder_will_actually_move(rig):
    """"N left" said nothing about whether N was ever going to go. Outside
    every date window a file sits pending by design, and reading that as a
    backlog is what made the whole ledger look like a queue."""
    from app import db, settings
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-27",
                   "backfill_enabled": False})

    db.upsert_assets([asset(1, taken="2026-09-01"),     # inside the window
                      asset(2, taken="2019-01-01"),     # outside it
                      asset(3, taken="2019-02-01")])
    months = {m["month"]: m for m in db.timeline()}

    assert months["2026-09"]["sending"] == 1
    assert months["2026-09"]["resting"] == 0
    assert months["2019-01"]["sending"] == 0
    assert months["2019-01"]["resting"] == 1, \
        "a month outside every window is not a backlog"
    # The parts still add up to the whole.
    for m in months.values():
        assert m["sending"] + m["resting"] == m["remaining"]


@pytest.mark.asyncio
async def test_asking_for_a_file_moves_it_from_resting_to_sending(rig):
    from app import db, settings
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-27",
                   "backfill_enabled": False})
    db.upsert_assets([asset(1, taken="2019-01-01")])

    assert db.timeline()[0]["resting"] == 1
    db.force_send(ids=["asset-1"])
    m = db.timeline()[0]
    assert m["resting"] == 0 and m["sending"] == 1


@pytest.mark.asyncio
async def test_the_split_agrees_with_what_the_feeder_will_claim(rig, monkeypatch):
    """The timeline and claim_batch must not disagree about what is going
    out, or the month row promises sends that never happen."""
    from app import db, feeder, immich, settings
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-27",
                   "backfill_enabled": False})
    db.upsert_assets([asset(1, size=100, taken="2026-09-01"),
                      asset(2, size=100, taken="2019-01-01")])

    promised = sum(m["sending"] for m in db.timeline())
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == promised == 1


# ---- excluding one file ------------------------------------------------

@pytest.mark.asyncio
async def test_a_single_file_can_be_excluded_and_sent_again(rig):
    """The case the month buttons cannot serve: a screenshot inside a window
    you otherwise want."""
    from app import db
    db.upsert_assets([asset(i, taken="2026-09-0" + str(i + 1)) for i in range(3)])

    assert db.dismiss_waiting(ids=["asset-1"]) == 1
    listed = {f["id"]: f for f in db.list_in_month("2026-09")["items"]}
    assert listed["asset-1"]["state"] == "skipped"
    assert listed["asset-0"]["state"] == "pending"

    assert db.force_send(ids=["asset-1"]) == 1
    assert db.list_in_month("2026-09")["items"] and \
        {f["id"]: f["state"] for f in db.list_in_month("2026-09")["items"]}["asset-1"] \
        == "pending"


@pytest.mark.asyncio
async def test_the_month_file_list_says_what_will_move(rig):
    """"pending" alone does not distinguish "on its way" from "outside every
    window and going nowhere"."""
    from app import db, settings
    settings.save({"ongoing_enabled": True, "ongoing_from": "2026-08-27",
                   "backfill_enabled": False})
    db.upsert_assets([asset(1, taken="2026-09-01"), asset(2, taken="2019-01-01")])

    byid = {f["id"]: f for f in db.list_in_month("2026-09")["items"]}
    assert byid["asset-1"]["will_send"] == 1
    byid = {f["id"]: f for f in db.list_in_month("2019-01")["items"]}
    assert byid["asset-2"]["will_send"] == 0


@pytest.mark.asyncio
async def test_the_month_file_list_hides_what_immich_no_longer_has(rig):
    """They cannot be sent or excluded, so offering them is a dead end."""
    from app import db
    db.upsert_assets([asset(i, taken="2026-09-05") for i in range(4)])
    db.mark_missing({"asset-0", "asset-1"})

    # mark_missing takes the ids Immich still returns, so 2 and 3 are gone.
    d = db.list_in_month("2026-09")
    assert d["total"] == 2
    assert {f["id"] for f in d["items"]} == {"asset-0", "asset-1"}
