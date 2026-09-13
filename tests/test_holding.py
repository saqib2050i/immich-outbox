"""Keeping back a file that would land in Google Photos wearing today's date.

Immich's metadata cannot tell you a file has no date: its date fields are
filled from the Takeout sidecar at import and say nothing about the bytes.
So the check has to happen while the bytes are here, which is once, during
delivery, in the temporary file before the rename into the outbox.

The thing that must not go wrong: a held file has never been in the outbox,
so it must never be readable as backed up.
"""

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio


async def _classified(rig, monkeypatch, seen, *, n=1):
    """Run one fill with the classifier stubbed to whatever `seen` says."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(i) for i in range(n)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def fake(path, row):
        out = dict(seen)
        out.setdefault("checked_at", db.now())
        out.setdefault("checked_sum", row.get("checksum"))
        return out
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)


async def test_a_file_that_needs_fixing_never_reaches_the_outbox(rig, monkeypatch):
    import os
    from app import config, db
    await _classified(rig, monkeypatch,
                      {"hold": True, "kind": "blank", "zone": "gps",
                       "writes": [{"tag": "DateTimeOriginal",
                                   "value": "2024:01:01 11:20:38",
                                   "from": "Immich's localDateTime"}],
                       "why": "would fall back to upload time"})
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "held"
    assert row["outbox_name"] is None
    real = [n for n in os.listdir(config.OUTBOX_DIR) if not n.startswith(".")]
    assert real == [], real


async def test_a_held_file_can_never_be_read_as_backed_up(rig, monkeypatch):
    """The one thing that must not go wrong. Confirmation is derived from a
    file vanishing out of the outbox, and a held file was never in it."""
    from app import db, feeder
    await _classified(rig, monkeypatch, {"hold": True, "kind": "blank"})
    for _ in range(3):
        feeder.reconcile()
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "held"
    assert row["confirmed_at"] is None


async def test_a_held_file_is_not_claimed_again_next_cycle(rig, monkeypatch):
    """claim_batch takes pending and failed. Held is neither, so a fill does
    not download it over and over to reach the same conclusion."""
    from app import db, settings
    await _classified(rig, monkeypatch, {"hold": True, "kind": "absent"})
    rows = db.claim_batch(10 ** 9, 10, settings.load().eligibility)
    assert rows == []


async def test_a_good_file_goes_through_untouched(rig, monkeypatch):
    import os
    from app import config, db
    await _classified(rig, monkeypatch, {"hold": False, "kind": "ok",
                                         "why": "carries 2024:01:01 11:20:38"})
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "queued"
    assert row["outbox_name"]
    assert os.path.exists(os.path.join(config.OUTBOX_DIR, row["outbox_name"]))


async def test_the_answer_is_kept_for_a_good_file_too(rig, monkeypatch):
    """A known-good answer saves the next pass a download just as a
    known-bad one does."""
    from app import db
    await _classified(rig, monkeypatch, {"hold": False, "kind": "ok"})
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["checked_at"] and row["checked_sum"] == row["checksum"]
    assert row["hold_kind"] == "ok"


async def test_the_proposal_is_kept_as_data_not_prose(rig, monkeypatch):
    """So a correction could one day be pushed back into Immich without
    parsing English into tags."""
    import json
    from app import db
    await _classified(rig, monkeypatch,
                      {"hold": True, "kind": "blank", "zone": "assumed",
                       "writes": [{"tag": "OffsetTimeOriginal", "value": "+05:00",
                                   "from": "this library's rule"}]})
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert json.loads(row["hold_writes"])[0]["tag"] == "OffsetTimeOriginal"


async def test_signing_one_off_sends_it_next(rig, monkeypatch):
    from app import db
    await _classified(rig, monkeypatch, {
        "hold": True, "kind": "blank",
        "writes": [{"tag": "DateTimeOriginal", "value": "2024:01:01 11:20:38",
                    "from": "Immich's localDateTime"}]})
    ids = [r["id"] for r in db.held()]
    assert db.release_held(ids) == 1
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "pending" and row["forced"] == 1
    assert row["hold_writes"] is not None, "and it remembers what was decided"


async def test_signing_off_touches_nothing_that_is_not_held(rig, monkeypatch):
    from app import db
    await _classified(rig, monkeypatch, {"hold": False, "kind": "ok"})
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert db.release_held([row["id"]]) == 0
    assert dict(db.connect().execute(
        "SELECT * FROM assets").fetchone())["state"] == "queued"


async def test_checking_is_off_until_it_is_turned_on(rig, monkeypatch):
    """It holds files back, and a setting that quietly stops a backup should
    be one somebody turned on."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": False})
    db.upsert_assets([asset(1)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    called = []
    async def fake(path, row):
        called.append(row["id"]); return {"hold": True, "kind": "blank"}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert called == []
    assert dict(db.connect().execute(
        "SELECT * FROM assets").fetchone())["state"] == "queued"


async def test_a_classifier_that_fails_does_not_stop_the_backup(rig, monkeypatch):
    """A diagnostic that can halt a backup is worse than no diagnostic."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(1)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    monkeypatch.setattr(diagnose, "classify", diagnose.classify)   # the real one

    async def explode(path):
        raise RuntimeError("exiftool went away")
    monkeypatch.setattr(diagnose, "read_exif", explode)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert dict(db.connect().execute(
        "SELECT * FROM assets").fetchone())["state"] == "queued"


async def test_held_files_are_counted_somewhere(rig, monkeypatch):
    """A state missing from counts() is counted in nothing, and shows as a
    gap between figures that ought to add up."""
    from app import db
    await _classified(rig, monkeypatch, {"hold": True, "kind": "blank"})
    assert db.counts()["held"] == 1


async def test_already_sent_files_are_split_out_hard(rig, monkeypatch):
    """Correcting one of these means clearing the old copy from Google
    Photos first -- a different action, and not one this can take."""
    from app import db
    await _classified(rig, monkeypatch, {"hold": True, "kind": "blank"})
    c = db.connect()
    c.execute("UPDATE assets SET confirmed_at = ?", (db.now(),))
    c.commit()
    assert db.held()[0]["already_sent"] is True


# ---- what Immich still does not have -------------------------------------
#
# Every correction here was written into a delivered copy and never into
# Immich. Immich goes on showing a date it holds only in its database while
# the file beside it has none. Closing that gap needs `asset.update`, which
# invariant 3 does not grant -- so this is the record of what would be sent
# if that decision were ever taken, kept as data rather than as a sentence.

async def test_a_correction_is_recorded_as_data_not_only_prose(rig, monkeypatch):
    """A record kept only as prose would have to be parsed back into tags."""
    import json
    from app import db, diagnose
    path = await _in_outbox_stub(rig)

    async def fake_trace(name, send=False):
        return {"filename": name,
                "asset": {"id": "asset-1", "kind": "IMAGE",
                          "taken_at": "2024-01-01T06:20:38.000Z"},
                "outbox": {"present": True, "path": path},
                "proposal": {"needed": True, "writes": [
                    {"tag": "DateTimeOriginal", "value": "2024:01:01 11:20:38",
                     "from": "Immich's localDateTime"},
                    {"tag": "OffsetTimeOriginal", "value": "+05:00",
                     "from": "Immich's timeZone"}]}}

    reads = iter([{"EXIF:Software": "x"},
                  {"EXIF:DateTimeOriginal": "2024:01:01 11:20:38"}])
    monkeypatch.setattr(diagnose, "trace", fake_trace)
    monkeypatch.setattr(diagnose, "read_exif", lambda p: next(reads))
    monkeypatch.setattr(diagnose, "_stamp", lambda *a: (True, ""))
    assert (await diagnose.apply_correction("IMG_0001.jpg"))["ok"] is True

    row = dict(db.connect().execute(
        "SELECT * FROM assets WHERE id='asset-1'").fetchone())
    assert row["stamped_note"], "still readable by a person"
    tags = [w["tag"] for w in json.loads(row["hold_writes"])]
    assert tags == ["DateTimeOriginal", "OffsetTimeOriginal"]


async def test_it_is_listed_as_owed_to_immich(rig, monkeypatch):
    from app import db, diagnose
    path = await _in_outbox_stub(rig)

    async def fake_trace(name, send=False):
        return {"filename": name,
                "asset": {"id": "asset-1", "kind": "IMAGE",
                          "taken_at": "2024-01-01T06:20:38.000Z"},
                "outbox": {"present": True, "path": path},
                "proposal": {"needed": True, "writes": [
                    {"tag": "DateTimeOriginal", "value": "2024:01:01 11:20:38",
                     "from": "Immich's localDateTime"}]}}

    reads = iter([{"EXIF:Software": "x"},
                  {"EXIF:DateTimeOriginal": "2024:01:01 11:20:38"}])
    monkeypatch.setattr(diagnose, "trace", fake_trace)
    monkeypatch.setattr(diagnose, "read_exif", lambda p: next(reads))
    monkeypatch.setattr(diagnose, "_stamp", lambda *a: (True, ""))
    await diagnose.apply_correction("IMG_0001.jpg")

    owed = db.pending_to_immich()
    assert len(owed) == 1
    assert owed[0]["filename"] == "IMG_0001.jpg"
    assert owed[0]["writes"][0]["tag"] == "DateTimeOriginal"


async def test_nothing_uncorrected_is_listed_as_owed(rig, monkeypatch):
    from app import db
    await _classified(rig, monkeypatch, {"hold": True, "kind": "blank"})
    assert db.pending_to_immich() == []


async def test_pushing_to_immich_is_not_offered(rig):
    """Invariant 3: three read scopes and no write. That is what makes "it
    cannot alter your library" a fact rather than a promise, so the button
    is absent and the reason is given."""
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    auth.set_password("a-good-password")
    c = TestClient(app)
    c.post("/api/login", json={"password": "a-good-password"})
    d = c.get("/api/dates/to-immich").json()
    assert d["can_apply"] is False
    assert "asset.update" in d["why"]


async def _in_outbox_stub(rig, name="IMG_0001.jpg"):
    """A ledger row with a real file behind it in the outbox."""
    import os
    from app import config, db
    db.upsert_assets([asset(1, name=name)])
    path = os.path.join(config.OUTBOX_DIR, name)
    with open(path, "wb") as fh:
        fh.write(b"not really a photo")
    c = db.connect()
    c.execute("UPDATE assets SET state='queued', outbox_name=?, taken_at=? "
              "WHERE id='asset-1'", (name, "2024-01-01T06:20:38.000Z"))
    c.commit()
    return path


async def test_the_endpoint_says_whether_it_is_even_checking(rig):
    """An empty held list means either every file was fine or nothing was
    looked at. The server knows which; the page used to guess."""
    from fastapi.testclient import TestClient
    from app import auth, db, settings
    from app.main import app
    auth.set_password("a-good-password")
    c = TestClient(app)
    c.post("/api/login", json={"password": "a-good-password"})

    settings.save({"check_dates": False})
    d = c.get("/api/dates/held").json()
    assert d["checking"] is False and d["checked"] == 0

    settings.save({"check_dates": True})
    db.upsert_assets([asset(1)])
    db.record_check("asset-1", {"hold": False, "kind": "ok",
                                "checked_at": db.now(), "checked_sum": "x"})
    d = c.get("/api/dates/held").json()
    assert d["checking"] is True and d["checked"] == 1
    assert d["held"] == [], "and it was fine, which is not the same as unread"


# ---- signing off has to actually send the file ---------------------------
#
# It did not. A signed-off file went back to pending, was fetched, classified
# again, found to have the same fault and held again -- a loop, with the
# correction never written and the tab never emptying. The approval has to
# survive into the next fetch and be honoured there.

async def test_signing_off_writes_the_correction_and_sends(rig, monkeypatch):
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    wrote = []

    async def fake(path, row):
        return {"hold": True, "kind": "blank", "zone": "gps",
                "writes": [{"tag": "DateTimeOriginal",
                            "value": "2024:01:01 11:20:38", "from": "Immich"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    monkeypatch.setattr(diagnose, "write_tags",
                        lambda p, w: (wrote.append(w), (True, ""))[1])

    _, used = feeder.reconcile()
    await feeder.top_up(used)
    db.release_held([r["id"] for r in db.held()])
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "queued", "it must actually go"
    assert row["outbox_name"]
    assert wrote and wrote[0][0]["tag"] == "DateTimeOriginal"
    assert row["stamped_at"], "and be recorded as corrected"


async def test_an_approval_is_spent_once(rig, monkeypatch):
    """Left set, the same tags would be written again on any later pass."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def fake(path, row):
        return {"hold": True, "kind": "blank",
                "writes": [{"tag": "DateTimeOriginal", "value": "x",
                            "from": "y"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    monkeypatch.setattr(diagnose, "write_tags", lambda p, w: (True, ""))

    _, used = feeder.reconcile()
    await feeder.top_up(used)
    db.release_held([r["id"] for r in db.held()])
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert dict(db.connect().execute(
        "SELECT approved_at FROM assets").fetchone())["approved_at"] is None


async def test_an_approved_file_is_not_classified_again(rig, monkeypatch):
    """Reading it again would reach the same conclusion and hold it again,
    which is the loop."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    looks = []

    async def fake(path, row):
        looks.append(row["id"])
        return {"hold": True, "kind": "blank",
                "writes": [{"tag": "DateTimeOriginal", "value": "x",
                            "from": "y"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    monkeypatch.setattr(diagnose, "write_tags", lambda p, w: (True, ""))

    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert len(looks) == 1
    db.release_held([r["id"] for r in db.held()])
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert len(looks) == 1, "it was read a second time and held again"


async def test_a_correction_that_cannot_be_written_fails_loudly(rig, monkeypatch):
    """Delivering it uncorrected would put it in Google Photos wearing the
    wrong date permanently, and the sign-off would have done nothing. A
    failure can be retried; that cannot be taken back."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def fake(path, row):
        return {"hold": True, "kind": "blank",
                "writes": [{"tag": "DateTimeOriginal", "value": "x",
                            "from": "y"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    db.release_held([r["id"] for r in db.held()])

    monkeypatch.setattr(diagnose, "write_tags",
                        lambda p, w: (False, "exiftool is not installed"))
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "failed"
    assert "exiftool is not installed" in (row["last_error"] or "")
    assert row["outbox_name"] is None or not row["stamped_at"]


# ---- saying what is happening, not just how many bytes -------------------

async def test_each_phase_of_a_transfer_is_named(rig, monkeypatch):
    """The bar tracked bytes and nothing else. A file now has its EXIF read
    after the download and a correction written into it, both real time
    spent with the bar already full -- so a file being worked on looked
    stuck, and the only figure on screen said 100%."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    seen = []

    async def fake(path, row):
        seen.append(feeder.TRANSFERS[row["id"]]["phase"])
        return {"hold": False, "kind": "ok", "checked_at": db.now(),
                "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert seen == ["checking"]


async def test_an_approved_correction_says_it_is_correcting(rig, monkeypatch):
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    seen = []

    async def fake(path, row):
        return {"hold": True, "kind": "blank",
                "writes": [{"tag": "DateTimeOriginal", "value": "x",
                            "from": "y"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    db.release_held([r["id"] for r in db.held()])

    def watch(path, writes):
        seen.append(feeder.TRANSFERS["asset-0"]["phase"])
        return True, ""
    monkeypatch.setattr(diagnose, "write_tags", watch)
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert seen == ["correcting"]


# ---- a held verdict must not go stale ------------------------------------
#
# A held file is never claimed a second time, so its stored answer is frozen
# at whatever the build and the settings said that day. Forty-seven photos
# from 2022 sat under "no zone, outside the rule in Settings" while the rule
# covered every one of them -- they had been read before the rule outranked
# a bare Immich zone, and nothing ever looked again.

STALE = {"ok": True, "time_zone": "UTC+1", "latitude": None, "longitude": None,
         "local_date_time": "2022-07-02T14:53:54.000Z",
         "file_created_at": "2022-07-02T13:53:54.000Z"}


async def test_a_rule_set_afterwards_reaches_a_file_already_held(rig):
    """The whole complaint. The file is not fetched again; everything
    needed to judge it was kept when it was."""
    from app import diagnose, settings
    settings.save({"assume_zone_before": "2025-03-04",
                   "assume_zone_offset": "+05:00"})
    row = {"id": "a", "kind": "IMAGE", "taken_at": "2022-07-02T13:53:54.000Z",
           "hold_kind": "unfixable", "hold_zone": "immich", "writes": [],
           "says": STALE}
    out = diagnose.rejudge(row)
    assert out["hold_zone"] == "assumed"
    assert out["hold_kind"] != "unfixable", "it is no longer the ceiling"
    w = {x["tag"]: x["value"] for x in out["writes"]}
    assert w["OffsetTimeOriginal"] == "+05:00"
    assert w["DateTimeOriginal"] == "2022:07:02 18:53:54", \
        "the instant plus the rule, not Immich's own default"


async def test_a_reading_is_left_alone(rig):
    """Coordinates and the file's own offset are readings. No setting
    improves on them, so re-judging must not overwrite one with a rule."""
    from app import diagnose, settings
    settings.save({"assume_zone_before": "2025-03-04",
                   "assume_zone_offset": "+05:00"})
    for kept in ("gps", "file"):
        row = {"id": "a", "kind": "IMAGE", "hold_zone": kept,
               "taken_at": "2022-07-02T13:53:54.000Z",
               "hold_kind": "blank", "says": STALE,
               "writes": [{"tag": "DateTimeOriginal", "value": "keep me",
                           "from": "x"}]}
        assert diagnose.rejudge(row)["writes"][0]["value"] == "keep me"


async def test_a_row_with_nothing_kept_is_returned_unchanged(rig):
    """Read by a build that kept no answer. Nothing can be worked out, so
    nothing is invented -- it needs reading again instead."""
    from app import diagnose
    row = {"id": "a", "kind": "IMAGE", "hold_zone": "immich",
           "hold_kind": "unfixable", "writes": [], "says": None}
    assert diagnose.rejudge(row) == row


async def test_reading_again_clears_what_was_decided(rig, monkeypatch):
    """It goes back without an approval, so the next fetch classifies it
    fresh rather than writing tags nobody has looked at since."""
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def fake(path, row):
        return {"hold": True, "kind": "blank", "zone": "immich",
                "writes": [{"tag": "DateTimeOriginal", "value": "old",
                            "from": "x"}],
                "checked_at": db.now(), "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    assert db.recheck([r["id"] for r in db.held()]) == 1
    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["state"] == "pending" and row["forced"] == 1
    assert row["approved_at"] is None, "not an approval"
    assert row["hold_writes"] is None and row["checked_at"] is None


async def test_what_immich_said_is_kept_when_a_file_is_held(rig, monkeypatch):
    """Without it there is nothing to judge again from."""
    import json
    from app import db, diagnose, feeder, immich, settings
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def detail(asset_id):
        return dict(STALE)
    monkeypatch.setattr(immich, "asset_detail", detail)
    monkeypatch.setattr(diagnose, "read_exif",
                        lambda p: {"EXIF:DateTimeOriginal": ""})
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    row = dict(db.connect().execute("SELECT * FROM assets").fetchone())
    assert row["hold_says"], "nothing was kept"
    assert json.loads(row["hold_says"])["time_zone"] == "UTC+1"


async def test_the_poll_carries_the_held_count(rig, monkeypatch):
    """The badge is written from it on every tick, so a file kept back is
    visible without opening the tab it is kept on."""
    from fastapi.testclient import TestClient
    from app import auth, db, diagnose, feeder, immich, settings
    from app.main import app
    settings.save({"check_dates": True})
    db.upsert_assets([asset(0)])
    monkeypatch.setattr(immich, "stream_original", fake_download())

    async def fake(path, row):
        return {"hold": True, "kind": "blank", "checked_at": db.now(),
                "checked_sum": row.get("checksum")}
    monkeypatch.setattr(diagnose, "classify", fake)
    _, used = feeder.reconcile()
    await feeder.top_up(used)

    auth.set_password("a-good-password")
    c = TestClient(app)
    c.post("/api/login", json={"password": "a-good-password"})
    assert c.get("/api/status").json()["counts"]["held"] == 1
