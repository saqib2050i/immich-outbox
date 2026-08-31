"""Writing a date corrected in Immich into the file itself.

The only place this service alters an original. It is deliberately narrow:
the file must already carry a date of its own, and Immich must hold a
different one -- which is exactly the shape of a correction made in Immich,
since that edit lives in Immich's database while /original keeps serving
the untouched file.
"""

import os
import shutil
import subprocess

import pytest

from conftest import asset, fake_download

pytestmark = pytest.mark.asyncio

HAVE_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not installed")


# --- when to touch a file at all -----------------------------------------

@pytest.mark.parametrize("taken,exif,expect", [
    # corrected in Immich: the whole point
    ("2019-06-04T14:30:00Z", "2016-12-11T09:00:00Z", True),
    # agree: leave it alone
    ("2019-06-04T14:30:00Z", "2019-06-04T14:30:00Z", False),
    # a minute of slop is not a correction
    ("2019-06-04T14:30:00Z", "2019-06-04T14:30:30Z", False),
    # no date of its own: nothing to correct, and mtime already covers it
    ("2019-06-04T14:30:00Z", None, False),
    ("2019-06-04T14:30:00Z", "", False),
    # no date from Immich either
    (None, "2019-06-04T14:30:00Z", False),
    (None, None, False),
])
async def test_only_a_real_correction_counts(rig, taken, exif, expect):
    from app import feeder
    assert feeder.needs_date_fix(taken, exif) is expect


# --- the rewrite itself, against a real file ------------------------------

def make_jpeg(path, when="2016:12:11 09:00:00"):
    """A minimal but genuine JPEG carrying an EXIF date."""
    subprocess.run(["exiftool", "-q", "-overwrite_original",
                    f"-AllDates={when}", "-o", str(path), "-"],
                   input=bytes.fromhex(
                       "ffd8ffe000104a46494600010100000100010000"
                       "ffdb004300ffffffffffffffffffffffffffffffffffffffff"
                       "ffffffffffffffffffffffffffffffffffffffffffffffffff"
                       "ffffffffffffffffffffffffffffffffffffffffffffffffff"
                       "ffc00011080001000103012200021101031101"
                       "ffc4001f0000010501010101010100000000000000000102030405060708090a0b"
                       "ffda000c03010002110311003f00fefeffd9"),
                   capture_output=True, check=False)
    return os.path.exists(path)


@needs_exiftool
async def test_the_written_date_is_what_immich_says(rig, tmp_path):
    from app import feeder

    src = tmp_path / "photo.jpg"
    if not make_jpeg(src):
        pytest.skip("could not build a sample JPEG")

    before = subprocess.run(["exiftool", "-s3", "-DateTimeOriginal", str(src)],
                            capture_output=True, text=True).stdout.strip()
    assert before.startswith("2016:12:11")

    assert feeder.rewrite_capture_date(str(src), "2019-06-04T14:30:00Z")

    after = subprocess.run(["exiftool", "-s3", "-DateTimeOriginal", str(src)],
                           capture_output=True, text=True).stdout.strip()
    assert after == "2019:06:04 14:30:00"


@needs_exiftool
async def test_the_rewrite_leaves_no_backup_beside_it(rig, tmp_path):
    """exiftool's default is to keep a _original copy, which in the outbox
    would look like a second photo to Syncthing and the phone."""
    from app import feeder

    # Its own directory: tmp_path also holds the rig's database and outbox.
    here = tmp_path / "exifcheck"
    here.mkdir()
    src = here / "photo.jpg"
    if not make_jpeg(src):
        pytest.skip("could not build a sample JPEG")
    feeder.rewrite_capture_date(str(src), "2019-06-04T14:30:00Z")
    assert [p.name for p in here.iterdir()] == ["photo.jpg"]


async def test_an_unreadable_date_is_not_written(rig):
    from app import feeder
    assert feeder.rewrite_capture_date("/nonexistent", "not a date") is False


# --- how it fits into delivery -------------------------------------------

async def test_a_corrected_asset_is_rewritten_before_it_lands(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"fix_dates": True})
    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    seen = {}

    def spy(path, taken_at):
        # Still the temp file: never the delivered name.
        seen["path"] = path
        seen["in_outbox_yet"] = os.path.basename(path) in rig.files()
        return True

    monkeypatch.setattr(feeder, "rewrite_capture_date", spy)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1

    assert seen["path"].endswith(".part"), "the delivered file was edited in place"
    assert seen["in_outbox_yet"] is False


async def test_an_uncorrected_asset_is_never_touched(rig, monkeypatch):
    """'otherwise don't touch' -- the file goes through byte for byte."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2019-06-04T14:30:00Z"),
                      asset(1, size=100, taken="2019-06-04T14:30:00Z")])

    calls = []
    monkeypatch.setattr(feeder, "rewrite_capture_date",
                        lambda p, t: calls.append(p) or True)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 2
    assert calls == [], "a file with no correction was modified"


async def test_the_setting_turns_it_off(rig, monkeypatch):
    from app import db, feeder, immich, settings

    settings.save({"fix_dates": False})
    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    calls = []
    monkeypatch.setattr(feeder, "rewrite_capture_date",
                        lambda p, t: calls.append(p) or True)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    await feeder.top_up(used)
    assert calls == []


async def test_a_failed_rewrite_still_delivers_the_file(rig, monkeypatch):
    """A missing exiftool must not stop the relay."""
    from app import db, feeder, immich, settings

    settings.save({"fix_dates": True})
    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    monkeypatch.setattr(feeder, "rewrite_capture_date", lambda p, t: False)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 1
    assert len(rig.files()) == 1


async def test_the_size_check_runs_before_any_edit(rig, monkeypatch):
    """Integrity is verified against Immich first; a truncated download is
    rejected rather than rewritten and delivered."""
    from app import db, feeder, immich, settings

    settings.save({"fix_dates": True})
    db.upsert_assets([asset(0, size=500_000, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    calls = []
    monkeypatch.setattr(feeder, "rewrite_capture_date",
                        lambda p, t: calls.append(p) or True)
    monkeypatch.setattr(immich, "stream_original",
                        fake_download(lambda _id, _s: b"<html>nope</html>"))
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 0
    assert calls == [], "a truncated file was edited"
    assert db.counts()["failed"] == 1


async def test_an_interrupted_rewrite_leaves_nothing_behind(rig):
    """exiftool writes a whole new file beside the target and renames it
    over, so an interruption leaves a copy the outbox listing cannot see --
    a dotfile, and therefore never swept."""
    import time
    from app import feeder

    stale = rig.outbox / ".partial-abc123.part_exiftool_tmp"
    stale.write_bytes(b"half a photo")
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))

    fresh = rig.outbox / ".partial-def456.part_exiftool_tmp"
    fresh.write_bytes(b"still being written")

    assert feeder.sweep_partials() == 1
    assert not stale.exists(), "an orphaned exiftool copy was left in the outbox"
    assert fresh.exists(), "a rewrite in progress was swept out from under itself"


async def test_the_exiftool_copy_is_never_counted_as_a_photo(rig):
    """It lives in the outbox while the rewrite runs, so it must not be
    read as a delivered file or counted against the cap."""
    from app import feeder

    (rig.outbox / ".partial-abc.part_exiftool_tmp").write_bytes(b"x" * 1000)
    assert rig.files() == set()
    names, used = feeder.list_outbox()
    assert names == [] and used == 0


# --- held back until you decide ------------------------------------------

async def test_rewriting_is_off_by_default(rig):
    from app import settings
    assert settings.load().fix_dates is False


async def test_a_corrected_file_is_held_back_while_rewriting_is_off(rig, monkeypatch):
    """Sending it now would put the old date in Google Photos permanently,
    and Google keeps whatever it is first given."""
    from app import db, feeder, immich

    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z"),
                      asset(1, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2019-06-04T14:30:00Z")])
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()

    assert await feeder.top_up(used) == 1, "the mismatched file went out anyway"
    assert db.queue_contents()[0]["id"] == "asset-1"


async def test_the_category_lists_them_with_examples(rig):
    from app import db, main

    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z", name="a.jpg"),
                      asset(1, size=200, taken="2019-07-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z", name="b.jpg"),
                      asset(2, size=100, taken="2019-06-04T14:30:00Z")])

    d = await main.date_mismatch()
    assert d["total"] == 2
    assert d["bytes"] == 300
    assert d["rewriting_enabled"] is False
    assert {m["month"] for m in d["months"]} == {"2019-06", "2019-07"}
    # The examples show the disagreement, so the decision is informed.
    ex = d["examples"][0]
    assert ex["taken_at"] and ex["exif_taken_at"]
    assert ex["taken_at"] != ex["exif_taken_at"]


async def test_sending_the_batch_turns_rewriting_on(rig, monkeypatch):
    from app import db, feeder, immich, main, settings

    db.upsert_assets([asset(i, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z") for i in range(3)])
    assert (await main.date_mismatch())["total"] == 3

    r = await main.date_mismatch_send({})
    assert r["queued"] == 3 and r["rewriting_enabled"] is True
    assert settings.load().fix_dates is True

    monkeypatch.setattr(feeder, "rewrite_capture_date", lambda p, t: True)
    monkeypatch.setattr(immich, "stream_original", fake_download())
    _, used = feeder.reconcile()
    assert await feeder.top_up(used) == 3


async def test_one_month_can_be_sent_at_a_time(rig):
    from app import db, main

    db.upsert_assets([asset(0, size=100, taken="2019-06-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z"),
                      asset(1, size=100, taken="2020-01-04T14:30:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    r = await main.date_mismatch_send({"month": "2019-06"})
    assert r["queued"] == 1

    # Only that month was released; the other is still waiting on a decision.
    forced = {row["id"]: row["forced"] for row in db.connect().execute(
        "SELECT id, forced FROM assets")}
    assert forced == {"asset-0": 1, "asset-1": 0}


async def test_a_rescan_notices_a_date_corrected_later(rig, monkeypatch):
    """The correction usually happens after the asset was first scanned."""
    from app import db, immich, main, worker

    db.upsert_assets([asset(0, size=100, taken="2016-12-11T09:00:00Z",
                            exif_taken="2016-12-11T09:00:00Z")])
    assert (await main.date_mismatch())["total"] == 0

    corrected = asset(0, size=100, taken="2019-06-04T14:30:00Z",
                      exif_taken="2016-12-11T09:00:00Z")

    async def page(taken_after=None):
        yield [corrected]
    monkeypatch.setattr(immich, "list_assets", page)
    await worker.full_scan()

    assert (await main.date_mismatch())["total"] == 1
