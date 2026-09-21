"""Trace one file from Immich to the phone, and say where it changes.

The relay promises a file passes through byte for byte, with one documented
exception (invariant 2a). That promise was never checkable from anywhere:
Immich's copy needs an API call, the outbox copy needs a shell on the
server, and nobody could see the two side by side. So a file that arrived
already damaged and a file the relay damaged looked exactly the same --
which is how five hours went missing from a library and the relay spent an
afternoon under suspicion for it.

The phone's copy is confirmed rather than read. The companion holds no
storage permission at all, which is what makes "it cannot delete a photo"
an Android guarantee rather than a promise in a comment, and reading a
photo's metadata would need exactly that permission. Syncthing verifies
every block it transfers, so a file it reports in sync is byte-identical to
the one in the outbox -- a stronger statement than re-reading its EXIF.

It also answers the question that decides where a photo lands: will Google
Photos read a date out of this file, or file it under the day it was
uploaded? Much of this library carries DateTimeOriginal present and *empty*
-- the date lived in a Google Takeout sidecar, Immich read it into its own
database at import, and the file left carrying a blank field. Immich shows
the right date on every screen while Google Photos dates the photo to the
upload, and the two facts never meet anywhere but here.

Nothing here writes. It downloads Immich's copy to a temporary file, reads
it, and deletes it.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from . import config, db, feeder, immich, settings

# Focused rather than exhaustive: these are the tags that decide what date
# Google Photos gives a photo, plus whatever exiftool wants to complain
# about. `-Warning` with `-a` is the point of the last one -- structural
# damage shows up there and nowhere else.
EXIF_TAGS = [
    # Stills. DateTimeOriginal is the one Google Photos reads; the rest are
    # here to say what else the file has when that one is empty.
    "-DateTimeOriginal", "-CreateDate", "-ModifyDate",
    "-OffsetTime", "-OffsetTimeOriginal", "-OffsetTimeDigitized",
    "-SubSecDateTimeOriginal", "-GPSDateTime",
    "-XMP:DateTimeOriginal", "-XMP:CreateDate", "-XMP:DateCreated",
    # Video. QuickTime:CreateDate is UTC by specification, unlike every
    # tag above it, and MediaCreateDate corroborates it.
    "-QuickTime:CreateDate", "-QuickTime:ModifyDate",
    "-QuickTime:MediaCreateDate", "-QuickTime:TrackCreateDate",
    "-QuickTime:CreationDate",
    "-Make", "-Model", "-Software", "-MIMEType",
    "-ImageWidth", "-ImageHeight", "-FileModifyDate",
    # Not dates, and here for the same reason the dates are: a Takeout
    # sidecar carried them, Immich read them into its database at import,
    # and the file went on without them. Asked of the file so the two can
    # be compared -- nothing writes these.
    "-GPSLatitude", "-GPSLongitude", "-GPSPosition",
    "-QuickTime:GPSCoordinates", "-UserData:GPSCoordinates",
    "-ImageDescription", "-XMP:Description", "-IPTC:Caption-Abstract",
    "-QuickTime:Description",
    # -G qualifies every key by group, so EXIF:DateTimeOriginal and
    # XMP:DateTimeOriginal stay apart. Without it exiftool returns both
    # under the bare name and the second silently wins -- which would
    # report a date in the tag Google reads when the value came from one
    # it does not.
    "-G", "-a", "-Warning",
]

# What the camera called it. An independent witness to the moment of the
# shutter -- but only if you know which clock it was reading, and cameras
# do not agree on that.
NAME_TIME = re.compile(
    r"(?:^|[^0-9])(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")

# A millisecond epoch in the name -- what an app writes when it saves a file
# it did not take: `1671553587634-<uuid>.jpg`, `FB_IMG_1656770033857.jpg`.
# It is an instant, and it is the same instant a Takeout sidecar carries, so
# it agrees with Immich by construction and reveals no zone.
NAME_EPOCH = re.compile(r"(?:^|[^0-9])(1[0-9]{12})(?:[^0-9]|$)")

# The Pixel camera names files in UTC and records the zone separately, so
# `PXL_20230101_025759` with an offset of +05:00 is a photo taken at 07:57
# local -- and the name being five hours off is the file being *right*.
# Older Google Camera builds, Samsung and most everything else wrote the
# local wall clock into the name instead.
UTC_NAMED = ("pxl_",)
LOCAL_NAMED = ("img_", "vid_", "mvimg_", "dsc_", "screenshot_", "lv_0_",
               "photogrid_")
# Samsung's `20221225_103124.jpg`, and the `2022-12-25-10-31-24-541.jpg` an
# Android gallery writes when it saves an edit: both the local wall clock.
LOCAL_SHAPED = (re.compile(r"^\d{8}_\d{6}"),
                re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}"))

# A name Google Photos gave something it *made*. The name is one of the
# source photos' -- `20211010_155825-COLLAGE.jpg` was built in June 2023 out
# of a photo taken in October 2021, and `20211010_155825.jpg` is in the same
# library. So the time in the name is a fact about a different file, and
# reading it as this one's is how a correct creation comes to look years
# wrong. 580 of the 603 creations in the library this was built for name a
# file that is also in it, which is the second, mechanical test below.
CREATION = re.compile(
    r"-(COLLAGE|ANIMATION|EFFECTS|CINEMATIC|CINEMATIC_MOMENT_VIDEO|MIX|SMILE|"
    r"PHOTO_FRAME|COLOR_POP|PORTRAIT_BLUR)|_exported_|^MOVIE\.", re.I)


def filename_time(name: str) -> datetime | None:
    """The time written into the name, naive, in whatever clock it is."""
    m = NAME_TIME.search(name or "")
    if m:
        try:
            return datetime(*(int(g) for g in m.groups()))
        except ValueError:
            return None
    m = NAME_EPOCH.search(name or "")
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc
                                          ).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def filename_clock(name: str) -> str:
    """Which clock was being read when the file was named.

    "unknown" is the honest and common answer, and it is load-bearing: a
    name whose convention is not known is never used to decide anything.
    Both conventions are checked against the file regardless -- a name that
    fits either is not evidence of anything being wrong, and asserting a
    convention would manufacture faults out of correct files.
    """
    base = os.path.basename(name or "").lower()
    if base.startswith(UTC_NAMED) or NAME_EPOCH.search(base):
        return "utc"
    if base.startswith(LOCAL_NAMED) or any(r.match(base) for r in LOCAL_SHAPED):
        return "local"
    return "unknown"


# Where a file keeps a location, and what it keeps a caption in. Both
# groups, because a still and a video do not agree on either.
GPS_TAGS = ("EXIF:GPSLatitude", "Composite:GPSPosition", "XMP:GPSLatitude",
            "QuickTime:GPSCoordinates", "UserData:GPSCoordinates")
CAPTION_TAGS = ("EXIF:ImageDescription", "XMP:Description",
                "IPTC:Caption-Abstract", "QuickTime:Description")


def gaps(exif: dict, says: dict, kind: str | None = None) -> list[dict]:
    """What Immich holds about this photo that the file itself does not.

    The same fault as the dates and for the same reason: a Google Takeout
    sidecar carried it, Immich read it into its database at import, and the
    file went to Google Photos without it. A sidecar carries five things --
    the dates, the location, the description, whether it was a favourite,
    and who is in it -- and only the first three can be written into a file
    at all.

    Everything else Immich reports about a photo (make, model, lens, ISO,
    aperture, focal length) it read *out of the file*, so a file missing one
    is a file Immich cannot supply it for either. There is nothing to
    restore there, which is worth knowing before anybody goes looking.

    Reported, never written. Whether Google Photos reads a location out of
    an upload is expected but unmeasured here, and a third exception to
    "the file goes through byte for byte" should be earned by a measurement
    the way the dates were.
    """
    if not says.get("ok"):
        return []
    out = []
    if says.get("latitude") is not None and says.get("longitude") is not None:
        if not any(pick(exif, t)[1] for t in GPS_TAGS):
            where = says.get("place")
            out.append({
                "what": "location",
                "immich": (f"{says['latitude']:.5f}, {says['longitude']:.5f}"
                           + (f" — {where}" if where else "")),
                "note": "Google Photos falls back to guessing a place from "
                        "everything else it knows, and labels it "
                        "\u201cestimated\u201d."})
    if says.get("description"):
        if not any(pick(exif, t)[1] for t in CAPTION_TAGS):
            text = str(says["description"]).strip()
            out.append({
                "what": "description",
                "immich": text[:120] + ("…" if len(text) > 120 else ""),
                "note": "Written under the photo in Immich, and nowhere in "
                        "the file."})
    return out


def _made_here(name: str) -> bool:
    """Is this a file Google Photos made, named after one of its sources?"""
    base = os.path.basename(name or "")
    if CREATION.search(base):
        return True
    # The general form: whatever suffix it carries, if the library holds a
    # file whose name is the front of this one, the time belongs to that
    # file. Catches the creation types nobody here has seen yet.
    stem = os.path.splitext(base)[0]
    for cut in ("-", "~"):
        head = stem.split(cut)[0]
        if len(head) >= 12 and head != stem and db.another_asset_named(head):
            return True
    return False


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_exif(path: str) -> dict:
    """What exiftool makes of a file, or why it could not say."""
    try:
        r = subprocess.run(["exiftool", "-json", *EXIF_TAGS, path],
                           capture_output=True, timeout=60, check=False)
    except FileNotFoundError:
        return {"error": "exiftool is not installed in this image, so nothing "
                         "here can be read. Everything else still works."}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": f"exiftool failed: {exc}"}
    if r.returncode != 0:
        detail = (r.stderr or b"").decode(errors="replace").strip()[:200]
        return {"error": f"exiftool refused the file: {detail}"}
    try:
        out = json.loads(r.stdout.decode(errors="replace"))[0]
    except (ValueError, IndexError):
        return {"error": "exiftool returned nothing readable"}
    out.pop("SourceFile", None)
    return out


def find(filename: str, asset_id: str | None = None):
    """The asset meant, the nearest things to the name, and the namesakes.

    A name that matches nothing is the most likely thing to happen at this
    box, and it used to be indistinguishable from a file with no problems.

    A name that matches *several* is the next most likely, and it used to be
    indistinguishable from a name that matched one. A camera restarts its
    counter, so DSC_0464.JPG is six different photographs in the library this
    was built for, taken between 2015 and 2017 -- and 3,589 names there
    belong to more than one asset, MOVIE.mp4 to 117 of them. `fetchone()`
    picked whichever SQLite handed back first and the report then spoke with
    perfect confidence about a photo nobody had asked about. An id settles
    it; without one, the caller is given the list and asked.
    """
    c = db.connect()
    if asset_id:
        row = c.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return (dict(row) if row else None), [], []

    name = (filename or "").strip()
    if not name:
        return None, [], []
    rows = c.execute("SELECT * FROM assets WHERE filename = ?", (name,)).fetchall()
    if not rows:
        rows = c.execute("SELECT * FROM assets WHERE filename = ? COLLATE NOCASE",
                         (name,)).fetchall()
    if len(rows) == 1:
        return dict(rows[0]), [], []
    if len(rows) > 1:
        return None, [], [
            {k: r[k] for k in ("id", "filename", "taken_at", "state", "size",
                               "kind", "outbox_name")}
            for r in sorted(rows, key=lambda r: r["taken_at"] or "")]

    # Near misses, so a typo or a pasted path is obvious rather than a
    # dead end. The stem first, then anything sharing the date in the name.
    stem = os.path.splitext(os.path.basename(name))[0][:24]
    near = c.execute(
        "SELECT filename FROM assets WHERE filename LIKE ? LIMIT 8",
        (f"%{stem}%",)).fetchall()
    if not near:
        m = NAME_TIME.search(name)
        if m:
            near = c.execute(
                "SELECT filename FROM assets WHERE filename LIKE ? LIMIT 8",
                (f"%{''.join(m.groups()[:3])}%",)).fetchall()
    return None, [r["filename"] for r in near], []


# A single file, downloaded deliberately. Large enough to cover any photo
# and most video; past it the byte comparison is skipped rather than the
# server spending minutes on it.
MAX_FETCH_BYTES = 600 * 1024 * 1024


async def _immich_copy(asset_id: str, expect: int) -> dict:
    """Immich's original, read and then thrown away.

    Downloaded to the system temp directory rather than the outbox: nothing
    here may leave a file where Syncthing would pick it up, and nothing here
    may look like a delivery that later went missing.
    """
    if expect and expect > MAX_FETCH_BYTES:
        return {"ok": False,
                "error": f"{expect / 1e6:.0f} MB is too large to fetch just to "
                         "compare — raise MAX_FETCH_BYTES if this is the file "
                         "you need"}
    tmp = None
    try:
        resp, client = await immich.stream_original(asset_id)
        try:
            fd, tmp = tempfile.mkstemp(prefix="trace-", suffix=".bin")
            with os.fdopen(fd, "wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 512):
                    fh.write(chunk)
        finally:
            await resp.aclose()
            await client.aclose()
        return {"ok": True, "bytes": os.path.getsize(tmp),
                "sha256": _sha(tmp), "exif": read_exif(tmp)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _outbox_copy(outbox_name: str | None) -> dict:
    if not outbox_name:
        return {"present": False,
                "note": "not in the outbox — nothing has been sent for this "
                        "asset yet, so there is no second copy to compare"}
    path = os.path.join(config.OUTBOX_DIR, outbox_name)
    if not os.path.exists(path):
        return {"present": False, "name": outbox_name,
                "note": "the ledger has a name for it but the file is not "
                        "there. Either Google Photos has cleared it off the "
                        "phone and Syncthing propagated the deletion — which "
                        "is how a file is confirmed — or the outbox is not "
                        "mounted"}
    return {"present": True, "name": outbox_name, "path": path,
            "bytes": os.path.getsize(path), "sha256": _sha(path),
            # Read in place, so this is a fact about the delivered file and
            # is what Google Photos falls back to.
            "mtime": os.path.getmtime(path),
            "modified": datetime.utcfromtimestamp(
                os.path.getmtime(path)).isoformat(timespec="seconds") + "Z",
            "exif": read_exif(path)}


async def _phone_copy(outbox_name: str | None) -> dict:
    """Syncthing's word on whether the phone holds this file.

    Not read, and deliberately so: the companion declares no storage
    permission, and reading a photo's metadata would need one. Syncthing
    hashes every block it transfers, so a device listed as having the
    current version has a byte-identical copy. That is a stronger claim
    than anything a re-read could make.
    """
    cfg = settings.load()
    if not cfg.syncthing_url.strip() or not cfg.syncthing_api_key.strip():
        return {"known": False,
                "note": "Syncthing is not configured here, so whether the "
                        "phone has this file cannot be checked. Add its "
                        "address and API key in Settings"}
    folder = cfg.syncthing_folder.strip()
    if not folder:
        return {"known": False,
                "note": "no Syncthing folder id is set, so a single file "
                        "cannot be looked up"}
    if not outbox_name:
        return {"known": False, "note": "nothing has been sent, so there is "
                                        "nothing for the phone to have"}

    from . import syncthing
    try:
        async with syncthing._client(cfg) as client:  # noqa: SLF001
            r = await client.get("/rest/db/file",
                                 params={"folder": folder, "file": outbox_name})
            if r.status_code != 200:
                return {"known": False,
                        "note": f"Syncthing answered HTTP {r.status_code} for "
                                "this file — check the folder id"}
            d = r.json()
            glob = d.get("global") or {}
            local = d.get("local") or {}
            names = {}
            rc = await client.get("/rest/config")
            if rc.status_code == 200:
                for dev in (rc.json().get("devices") or []):
                    names[dev.get("deviceID")] = dev.get("name") or dev.get("deviceID")
            holders = [names.get(i, i[:7]) for i in (d.get("availability") or [])
                       if isinstance(i, str)]
            return {
                "known": True,
                "on_the_server": bool(local.get("size")) and not local.get("deleted"),
                "deleted_globally": bool(glob.get("deleted")),
                "size": glob.get("size"),
                "other_devices_with_it": holders,
            }
    except Exception as exc:  # noqa: BLE001
        return {"known": False,
                "note": f"could not ask Syncthing: {type(exc).__name__}: "
                        f"{str(exc)[:120]}"}


EXIF_FMT = "%Y:%m:%d %H:%M:%S"


def _exif_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:19], EXIF_FMT)
    except ValueError:
        return None


# Every shape a zone arrives in. EXIF writes "+05:00"; Immich writes
# "UTC+1", "UTC+05:30", "UTC-3" wherever it has no IANA name, and a parser
# that only knew the first called 42 files unfixable while Immich was
# holding their zone all along.
OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$",
                    re.IGNORECASE)


def _offset_hours(value) -> float | None:
    """"+05:00" as 5.0, and so are "UTC+5", "UTC+05:30" and "GMT-3"."""
    if not isinstance(value, str):
        return None
    m = OFFSET.match(value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) + int(m.group(3) or 0) / 60)


def pick(exif: dict, *candidates: str) -> tuple[object, str | None]:
    """The first of these tags the file actually carries, and which one.

    Keys arrive group-qualified from `-G`. A bare name is tried too, so a
    dict assembled by hand in a test still resolves, and so a reading taken
    before `-G` was added does not silently come back empty.
    """
    for key in candidates:
        if key in exif:
            return exif[key], key
        bare = key.split(":")[-1]
        if bare in exif:
            return exif[bare], bare
    return None, None


def is_blank(value: object) -> bool:
    """A tag that is present and says nothing.

    This is the whole problem in one function. A Takeout export can leave
    DateTimeOriginal in the file with its twenty bytes blanked, and an mp4
    whose creation_time is zero reports `0000:00:00 00:00:00` -- neither is
    a missing tag and neither is a date, and both were being filtered out
    one line above this as though they were nothing at all. Google Photos
    reads no date from either and files the upload under today.
    """
    if not isinstance(value, str):
        return False
    # Strip the punctuation a date is made of and see whether anything was
    # ever written between it. "0000:00:00 00:00:00" collapses to zeros, a
    # space-filled tag to nothing at all, and "Google" to itself.
    text = value.replace("\x00", "").strip()
    bare = text.replace(":", "").replace("-", "").replace(" ", "")
    return bare == "" or bare.strip("0") == ""


# The four things a date tag can be. "missing" and "blank" have the same
# consequence and different causes, so they are never merged.
MISSING, BLANK, UNREADABLE, VALUE = "missing", "blank", "unreadable", "value"


def date_state(exif: dict, *candidates: str) -> tuple[str, object, str | None]:
    """(state, raw value, which tag) for a date the file may or may not have."""
    raw, key = pick(exif, *candidates)
    if key is None:
        return MISSING, None, None
    if is_blank(raw):
        return BLANK, raw, key
    if _exif_dt(raw) is None:
        return UNREADABLE, raw, key
    return VALUE, raw, key


def is_video(exif: dict, kind: str | None = None) -> bool:
    """What the file is, asked of the file before the ledger."""
    mime, _ = pick(exif, "File:MIMEType", "MIMEType")
    if isinstance(mime, str) and mime:
        return mime.lower().startswith("video/")
    return (kind or "").upper() == "VIDEO"


# The tag Google Photos reads, per kind, and what else the file might be
# carrying when that one is empty. Order is priority: the first with a
# value is the one quoted.
PHOTO_DATE = ("EXIF:DateTimeOriginal",)
VIDEO_DATE = ("QuickTime:CreateDate",)

# Where a capture time could honestly be recovered from when the tag above
# is empty. ModifyDate is deliberately not here: it is when the file was
# last written, which for a Takeout export is the export itself, and
# offering it as a capture time would date a whole library to the day it
# was downloaded. Composite tags are not here either -- exiftool derives
# them from the very tags being examined, so on a blank file they restate
# the blank with an offset glued to it.
PHOTO_SPARE = ("XMP:DateTimeOriginal", "XMP:CreateDate", "XMP:DateCreated",
               "EXIF:CreateDate")
VIDEO_SPARE = ("QuickTime:CreationDate", "QuickTime:MediaCreateDate",
               "QuickTime:TrackCreateDate")

# Everything shown side by side, whether or not it has anything in it.
# Wider than the lists above on purpose: a tag that must never be *used*
# is still worth *seeing*.
PHOTO_SHOWN = PHOTO_DATE + PHOTO_SPARE + (
    "EXIF:ModifyDate", "EXIF:OffsetTimeOriginal", "EXIF:OffsetTime",
    "EXIF:Make", "EXIF:Model", "EXIF:Software", "File:FileModifyDate")
VIDEO_SHOWN = VIDEO_DATE + VIDEO_SPARE + (
    "QuickTime:ModifyDate", "QuickTime:Make", "QuickTime:Model",
    "File:FileModifyDate")

# A file's own groups, whose names are unambiguous within it.
NATIVE = {False: ("EXIF", "File"), True: ("QuickTime", "File")}


def label(key: str, video: bool = False) -> str:
    """A tag's name, kept qualified wherever dropping the group would lie.

    `EXIF:DateTimeOriginal` and `XMP:DateTimeOriginal` are different tags
    that Google Photos treats differently, and shortening both to
    "DateTimeOriginal" put them on one row -- the same collapse that
    requesting them without -G causes, arriving by the back door.
    """
    group, _, name = key.rpartition(":")
    return name if not group or group in NATIVE[video] else key


# Immich's copy is fetched to a temporary file, so its modification time is
# when the download happened -- today, always. Reporting that beside the
# outbox copy's real mtime invents a difference between two files that are
# byte for byte identical, and paints it red directly under the finding
# saying so.
DOWNLOADED_MEANINGLESS = ("File:FileModifyDate",)


def _dates(exif: dict, kind: str | None = None,
           downloaded: bool = False) -> dict:
    """Every tag that decides the date, said out loud including the empty ones.

    It used to drop anything falsy, which meant a blanked DateTimeOriginal
    -- the exact fault this is for -- came back indistinguishable from a
    file that never had one, and the dashboard drew an em-dash for both.
    """
    video = is_video(exif, kind)
    out: dict = {}
    for key in (VIDEO_SHOWN if video else PHOTO_SHOWN):
        if downloaded and key in DOWNLOADED_MEANINGLESS:
            continue
        raw, found = pick(exif, key)
        if found is None:
            continue
        out[label(key, video)] = "" if raw is None else str(raw)
    return out


def _tag_table(exif: dict, kind: str | None = None,
               downloaded: bool = False) -> list[dict]:
    """The same tags with their state, for a reader rather than a diff."""
    video = is_video(exif, kind)
    rows = []
    for key in (VIDEO_SHOWN if video else PHOTO_SHOWN):
        raw, found = pick(exif, key)
        name = label(key, video)
        if downloaded and key in DOWNLOADED_MEANINGLESS:
            rows.append({"tag": name, "state": "n/a",
                         "text": "not meaningful — this copy was downloaded"})
        elif found is None:
            rows.append({"tag": name, "state": MISSING, "text": "not in the file"})
        elif is_blank(raw):
            rows.append({"tag": name, "state": BLANK,
                         "text": "present but empty"})
        else:
            rows.append({"tag": name, "state": VALUE, "text": str(raw)})
    return rows


def _naive(value: object) -> datetime | None:
    """An ISO timestamp with whatever zone it carries thrown away.

    Immich serialises `localDateTime` with a trailing Z, but it is the wall
    clock where the shutter fired and not a UTC instant -- the Z is an
    artefact of the transport. Comparing it against DateTimeOriginal, which
    EXIF also defines as local time with no zone, means comparing the two
    naively and on purpose.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "").replace("z", "")
    text = re.sub(r"[+-]\d{2}:?\d{2}$", "", text)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def _instant(value: object) -> float | None:
    """An ISO timestamp as a POSIX instant, zone respected."""
    return db.capture_time(value if isinstance(value, str) else None)


def verdict(exif: dict, kind: str | None = None, *,
            mtime: float | None = None, taken_at: str | None = None,
            says: dict | None = None, downloaded: bool = False,
            name: str | None = None) -> dict:
    """Will Google Photos date this file, or file it under the upload?

    The only question that matters about a file on its way to the phone,
    and it is decided by one tag: DateTimeOriginal for a still, QuickTime's
    CreateDate for a video. Everything else in the file is a note.

    A confirmed case sets the bar: PXL_20240105_034733992.jpg carries
    DateTimeOriginal present and empty, Immich shows it correctly from a
    Takeout sidecar, and Google Photos filed it under the day it was
    uploaded -- with a perfectly parseable date sitting in the filename,
    which Google did not use. So a name is never counted as a date here.

    A *modification time* is counted, though, because Google Photos does
    fall back to one and this service deliberately sets it:
    `feeder.stamp_capture_time()` stamps every delivered file with Immich's
    capture instant, and Syncthing preserves it all the way to the phone.
    Snapchat-618209934.jpg has no date tag of any kind and Google Photos
    still dated it Jan 1 2024, 6:30 AM -- the same second as the outbox
    copy's mtime. Calling that file "would fall back to upload time" was
    this function being wrong, out loud, about a file that was fine.

    The fallback is weaker than the tag and is reported as such: an mtime
    survives nothing that rewrites the file, and it carries no zone, so what
    Google Photos displays for a photo taken outside UTC is not settled by
    anything here.
    """
    video = is_video(exif, kind)
    want = VIDEO_DATE if video else PHOTO_DATE
    spare = VIDEO_SPARE if video else PHOTO_SPARE
    tag = want[0].split(":")[-1]
    state, raw, key = date_state(exif, *want)

    others = []
    for cand in spare:
        st, value, found = date_state(exif, cand)
        if st == VALUE:
            others.append({"tag": label(cand, video), "value": str(value)})

    if state == VALUE:
        note = (" QuickTime records it in UTC, which is the specification and "
                "what Google Photos expects of a video."
                if video else "")
        return {"dated": True, "level": "ok", "tag": tag, "state": state,
                "value": str(raw), "others": others,
                "headline": "Would be dated correctly by Google",
                "reason": f"{tag} carries {str(raw).strip()}." + note}

    blank = (f"{tag} is in the file but holds nothing but zeros — an mp4 "
             "whose creation_time was never set, which a re-encode or an "
             "export strips."
             if video else
             f"{tag} is in the file but empty — blank bytes where the date "
             "should be. That is not a missing tag and it is not a date, and "
             "it is what a Google Takeout export leaves behind when the date "
             "lived in the sidecar rather than in the file.")
    why = {
        BLANK: blank,
        MISSING: f"{tag} is not in the file at all.",
        UNREADABLE: f"{tag} holds {str(raw)!r}, which is not a date.",
    }[state]

    # No tag, but Google Photos falls back to the modification time and this
    # service sets that to the capture instant on the way out. Checked
    # against Immich rather than assumed: a file downloaded to a temp
    # directory has today's mtime and must not be credited with it, which is
    # why only a copy read in place passes one in.
    #
    # MISSING only, and that is measured rather than reasoned. Two files
    # went the same route with the same correct mtime and landed in
    # different years:
    #
    #   Snapchat-618209934.jpg   tag absent         -> Jan 1 2024, 6:30 AM
    #   PXL_20240101_062038690   tag present, empty -> today, 12:33 PM
    #
    # A blank tag poisons the fallback -- the scanner evidently reads the
    # file as carrying metadata, fails to parse it, and never reaches the
    # modification time. An absent tag falls through cleanly. So a blank one
    # is not rescued here, however good the mtime beside it looks.
    want = db.capture_time(taken_at)
    rescued = (state == MISSING and mtime is not None and want is not None
               and abs(mtime - want) <= 120)
    if rescued:
        when = datetime.fromtimestamp(mtime, timezone.utc).strftime(
                   "%Y-%m-%d %H:%M:%S")
        base = (why + f" Its modification time is {when}Z, which is Immich's "
                "capture time — this service stamps every delivered file with "
                "it and Syncthing carries it to the phone, and Google Photos "
                "falls back to it when there is no tag.")
        # True of all three readings below, so it is said in all three
        # rather than only in the happy one.
        frail = (" The mtime is the weaker carrier either way: it does not "
                 "survive anything that rewrites the file.")

        # An mtime is an instant with no zone, and Google Photos shows it as
        # UTC: Snapchat-618209934.jpg came back labelled GMT+00:00. So the
        # moment is right and the clock on screen is the capture zone's
        # offset out -- which is nothing at UTC and five hours across most of
        # this library.
        kindz, zone = zone_source(exif, says or {}, taken_at, name)
        off = zone_hours(kindz, zone, taken_at)
        if off is None:
            return {"dated": True, "level": "warn", "tag": tag, "state": state,
                    "value": None, "others": others,
                    "headline": "Dated by its modification time, zone unknown",
                    "reason": base + " Google Photos shows that instant as "
                              "UTC. Nothing here knows which zone the photo "
                              "was taken in, so whether the time on screen is "
                              "the time on the clock cannot be said." + frail}
        if abs(off) < 1 / 60:
            return {"dated": True, "level": "ok", "tag": tag, "state": state,
                    "value": None, "others": others,
                    "headline": "Dated by its modification time, not its metadata",
                    "reason": base + " This photo was taken at UTC, so the "
                              "instant and the wall clock are the same number "
                              "and it lands right." + frail}

        local = _naive((says or {}).get("local_date_time"))
        crosses = local is not None and (
            local.hour + local.minute / 60 < off if off > 0
            else local.hour + local.minute / 60 >= 24 + off)
        day = (" And it was taken within that of midnight, so Google Photos "
               "puts it on the wrong day as well." if crosses else
               " The day is right; the time of day is not.")
        return {"dated": True, "level": "warn", "tag": tag, "state": state,
                "value": None, "others": others,
                "headline": f"Dated by its modification time, {abs(off):g}h out",
                "reason": base + " But an mtime is an instant with no zone and "
                          f"Google Photos shows it as UTC, while this photo "
                          f"was taken at {zone} — so it appears {abs(off):g} "
                          f"hours {'early' if off > 0 else 'late'} there."
                          + day
                          + (" That zone is this library's rule for a photo of "
                             "this age rather than anything in the file."
                             if kindz == "assumed" else "") + frail}

    extra = ""
    # Immich's copy is fetched to a temp file, so it has no delivered
    # modification time to be judged on and this verdict is about its
    # metadata alone. Saying only "would fall back to upload time" puts a
    # cross beside a file the pipeline actually handles, one line above the
    # tick that says so.
    if downloaded:
        # Not a hypothetical about uploading this byte stream by hand. The
        # relay stamps every delivered file with Immich's capture instant,
        # so where the ledger has a date, the copy that reaches the phone
        # carries one and the upload date never comes into it.
        stamped = db.capture_time(taken_at) is not None
        extra += (" That is Immich's copy judged on its metadata alone: it "
                  "was fetched here to a temporary file, so it has no "
                  "delivered modification time to be read."
                  + (" The copy this service delivers is stamped with "
                     "Immich's capture time, so in practice this file is "
                     "dated by that rather than by the upload — see the "
                     "outbox copy below." if stamped else
                     " And Immich holds no date to stamp one with, so the "
                     "upload date really is where this would land."))
    if (state == BLANK and mtime is not None and want is not None
            and abs(mtime - want) <= 120):
        extra += (" Its modification time is correct — this service stamps "
                  "every delivered file with Immich's capture instant — and "
                  "that does not save it. A file with no date tag at all "
                  "falls through to the mtime and lands on the right day; "
                  "one carrying a blank tag does not, which is the "
                  "difference between two files from this library that took "
                  "the same route and landed nine hundred days apart.")
    if others:
        extra = (" The file does carry a date elsewhere — "
                 + ", ".join(f"{o['tag']} {o['value'].strip()}" for o in others)
                 + " — but that is not the tag Google Photos reads.")
    # Only when nothing above has already said otherwise. With a capture
    # date in the ledger the delivered copy is stamped, and appending this
    # contradicted the sentence before it in the same paragraph.
    tail = ("" if downloaded and db.capture_time(taken_at) is not None
            else " Google Photos will file it under the day it was uploaded.")
    return {"dated": False, "level": "bad", "tag": tag, "state": state,
            "value": None if raw is None else str(raw), "others": others,
            "headline": ("Its own metadata would not date it" if downloaded
                         and db.capture_time(taken_at) is not None
                         else "Would fall back to upload time"),
            "reason": why + extra + tail}


# A zone Immich reports as UTC on a file with no coordinates is a default,
# not a finding. Told apart because the consequence differs by five hours.
UTC_ISH = ("utc", "utc+0", "utc+00:00", "+00:00", "+0000", "z", "gmt", "gmt+0")


def _assumed(taken_at: str | None) -> tuple[str, str] | None:
    """The owner's own rule for a photo that carries no zone.

    Not readable from any file: it is where they were living, and this
    library spans a move. Configured rather than constant, and off unless
    both halves are set.
    """
    cfg = settings.load()
    before, offset = (cfg.assume_zone_before or "").strip(), \
        (cfg.assume_zone_offset or "").strip()
    if not before or not offset or _offset_hours(offset) is None:
        return None
    when = db.capture_time(taken_at)
    edge = db.capture_time(before)
    if when is None or edge is None or when >= edge:
        return None
    return offset, before


# How far the name may sit from Immich's instant and still be the same
# moment. A video is named when recording started and dated when the file
# was written, which is seconds to minutes later.
NAME_SLACK_H = 3 / 60
VIDEO_SLACK_H = 8 / 60


def name_reading(name: str | None, taken_at: str | None,
                 video: bool = False) -> dict:
    """What the file's name says about when it was taken, if anything.

    Four answers, and telling them apart is the whole of it:

      zone        a name written in local time, a whole quarter-hour from
                  Immich's instant: that gap *is* the offset it was taken
                  at, evidence about this one file rather than a rule about
                  a whole library
      agrees      a name written in UTC that matches the instant. True of a
                  Pixel, and of the millisecond epoch an app writes into a
                  file it saved. Consistent, and no zone in it
      disagrees   neither reading fits. One of the two is about something
                  else: a sidecar date from an import, or a camera whose
                  clock was wrong
      made        Google Photos built this file and named it after a source
      none        no time in the name, or a convention nobody here knows --
                  Snapchat's numbers, `images (25).jpeg`

    Nothing is asserted from a name alone. "unknown" stays unknown, because
    guessing a convention is how an entirely correct library came to look
    five hours broken.
    """
    out: dict = {"verdict": "none", "when": None, "clock": "unknown",
                 "gap": None, "offset": None}
    when = filename_time(name or "")
    if when is None:
        return out
    out["when"], out["clock"] = when, filename_clock(name or "")
    if _made_here(name or ""):
        out["verdict"] = "made"
        return out
    instant = db.capture_time(taken_at)
    if instant is None or out["clock"] == "unknown":
        return out

    gap = (when - datetime.fromtimestamp(instant, timezone.utc)
           .replace(tzinfo=None)).total_seconds() / 3600
    out["gap"] = gap
    slack = VIDEO_SLACK_H if video else NAME_SLACK_H
    if out["clock"] == "utc":
        out["verdict"] = "agrees" if abs(gap) <= slack else "disagrees"
        return out

    off = _zone_from_gap(gap, slack)
    if off is None:
        out["verdict"] = "disagrees"
    else:
        out["verdict"], out["offset"] = "zone", off
    return out


# Every offset the world actually keeps. Whole hours, plus the ones that are
# not: Newfoundland, the Marquesas, Iran, Afghanistan, India, Nepal, Myanmar,
# Eucla, central Australia, Lord Howe, the Chathams.
#
# The list is the check. Any quarter-hour would do as arithmetic, and reading
# a save delay of 25 minutes as "+04:45" is how seven edited screenshots from
# one sitting in Lahore came out in three different zones, none of them a
# place. An offset nobody keeps is not a zone; it is a gap.
REAL_OFFSETS = tuple(sorted(
    set(range(-12, 15))
    | {-9.5, -3.5, 3.5, 4.5, 5.5, 6.5, 9.5, 10.5, 5.75, 8.75, 12.75}))

def _zone_from_gap(gap: float, slack: float) -> float | None:
    """The offset a local-clock name implies, or None if it implies none.

    `gap` is the name minus Immich's instant, and only an *exact* fit counts
    -- a real offset, within the slack a file's write takes.

    It briefly also took the next offset above the gap, on the reasoning
    that a file written a while after the shutter shows the offset minus
    that delay. The reasoning holds and the inference does not: a gap of
    4h26m is +04:30 with no delay, or +05:00 with a 34-minute one, and
    nothing in the two numbers says which. Seven edited screenshots from one
    sitting came out at +04:30, +04:45 and +05:00 -- three zones for one
    afternoon in Lahore. A gap that is not an offset is not evidence of one;
    it is reported as a disagreement instead, which is what it is.
    """
    near = min(REAL_OFFSETS, key=lambda o: abs(gap - o))
    return near if abs(gap - near) <= slack else None


def zone_source(exif: dict, says: dict, taken_at: str | None = None,
                name: str | None = None) -> tuple[str, str | None]:
    """Where the time zone came from, and what it was.

    Three provenances with three different weights. The file's own
    `OffsetTimeOriginal` is the photographer's camera saying what it was set
    to. GPS is Immich deriving a zone from where the shutter was pressed --
    just as good. Neither is the third case, where Immich has nothing and
    reports UTC, and the wall clock it then shows is the UTC instant wearing
    a local label.
    """
    own = pick(exif, "EXIF:OffsetTimeOriginal", "EXIF:OffsetTime")[0]
    if isinstance(own, str) and not is_blank(own) and _offset_hours(own) is not None:
        return "file", own.strip()
    zone = says.get("time_zone")
    if says.get("latitude") is not None and says.get("longitude") is not None:
        return "gps", zone

    # The name, where the camera wrote a local clock into it: the gap
    # between that and Immich's instant is the offset this one file was
    # taken at. It outranks the rule because the rule is a sentence about a
    # whole library and this is evidence about this photo -- it is what puts
    # a journey in the right zone, and a trip home during a year spent
    # elsewhere. It does not outrank coordinates or the file's own offset,
    # which are recorded at the shutter rather than inferred from two
    # numbers that might both be wrong.
    told = name_reading(name, taken_at, is_video(exif))
    if told["verdict"] == "zone":
        return "name", offset_text(told["offset"])

    # The owner's rule outranks a bare Immich zone, and that order matters.
    # With no offset tag and no coordinates Immich has nothing to derive a
    # zone from, so what it reports is its own default -- in practice the
    # machine that ran the import. A 2022 photo taken in Karachi came back
    # as UTC+1 because that is where its owner lives now, and honouring it
    # would have written a wall clock four hours out. The rule is somebody
    # saying where they were; this is a server saying where it is.
    guess = _assumed(taken_at)
    if guess:
        return "assumed", guess[0]
    if zone and str(zone).strip().lower() not in UTC_ISH:
        return "immich", str(zone).strip()
    return "none", zone


def wall_clock(says: dict, exif: dict, taken_at: str | None,
               name: str | None = None) -> tuple[datetime | None, float | None, str]:
    """When the shutter actually fired, local, with the correction applied.

    Immich's `localDateTime` is the wall clock only where Immich knows the
    zone. Where it does not, it reports UTC and `localDateTime` is the
    instant wearing a local label -- so the owner's rule, once it supplies
    an offset, has to be *added* to it. Reporting the raw value beside the
    rule that contradicts it is how this report came to state two different
    times for one photo, two lines apart.
    """
    kind, zone = zone_source(exif, says, taken_at, name)
    off = zone_hours(kind, zone, taken_at)

    # The name *is* the wall clock where it decided the zone: the camera
    # wrote down the local time, and Immich supplies the moment it belongs
    # to. Nothing else has to be derived, and Immich's own conversion is not
    # consulted -- it converted through a zone that has just been outranked.
    if kind == "name":
        told = name_reading(name, taken_at, is_video(exif))
        if told["when"] is not None:
            return told["when"], off, kind

    base = _naive(says.get("local_date_time"))
    if base is None:
        return None, off, kind
    # The clock has to follow the zone that was chosen, not the one Immich
    # chose. `localDateTime` is `fileCreatedAt` converted through Immich's
    # own `timeZone`, so it is the wall clock only while that zone is the
    # one being used.
    #
    # Where the rule wins, it wins *against* Immich's zone -- a 2022 Karachi
    # photo Immich filed at UTC+1 -- so the wall clock is the instant plus
    # the rule's offset, and taking Immich's converted value would apply the
    # zone that was just rejected. Where Immich's zone or the coordinates
    # are what is being used, its conversion is already right.
    #
    # It was keyed on whether Immich knew *a* zone, which was the same thing
    # only while the rule could not outrank one.
    # Immich's conversion is usable only when the zone it converted through
    # is the zone being used. Two ways it is not:
    #
    #   assumed   the rule won against Immich's zone, so taking Immich's
    #             converted value applies the zone just rejected
    #   file      the offset is in the file but Immich never saw it -- it
    #             was written into the outbox copy after the import, and
    #             Immich's own file still has none
    #
    # Both are answered by asking whether Immich had a zone of its own and
    # whether that is the one winning.
    ours = kind in ("gps", "immich") or (
        kind == "file" and _immich_knew_the_zone(says))
    if not ours and off is not None:
        instant = _naive(says.get("file_created_at"))
        return (instant or base) + timedelta(hours=off), off, kind
    return base, off, kind


def _immich_knew_the_zone(says: dict) -> bool:
    """Did Immich have anything to derive a zone from?

    Coordinates, or a timeZone that is not its UTC fallback. Immich reads an
    offset tag at import too, but that shows up as a non-UTC timeZone here,
    so both routes are covered by the same two checks.
    """
    if says.get("latitude") is not None and says.get("longitude") is not None:
        return True
    zone = says.get("time_zone")
    if zone and str(zone).strip().lower() not in UTC_ISH:
        return True
    # And the data says so itself: localDateTime is fileCreatedAt converted
    # through Immich's zone, so the two differing *is* Immich having applied
    # one. Stronger than the label, since it cannot be out of step with the
    # numbers beside it.
    local, created = (_naive(says.get("local_date_time")),
                      _naive(says.get("file_created_at")))
    return local is not None and created is not None and local != created


def zone_hours(kind: str, zone: str | None, taken_at: str | None) -> float | None:
    """The zone as a number of hours, or None when it is not known.

    A named zone is resolved at the capture instant rather than today, so
    the answer is right across a DST boundary. "none" is deliberately not
    zero: not knowing the offset and knowing it to be zero are different,
    and conflating them is the whole of this section.
    """
    if kind == "none" or not zone:
        return None
    direct = _offset_hours(zone)
    if direct is not None:
        return direct
    text = str(zone).strip()
    if text.lower() in UTC_ISH:
        return 0.0
    when = db.capture_time(taken_at)
    if when is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        off = datetime.fromtimestamp(when, ZoneInfo(text)).utcoffset()
        return off.total_seconds() / 3600 if off else 0.0
    except Exception:  # noqa: BLE001  -- missing tzdata, or not a zone name
        return None


def _agrees_with_immich(exif: dict, says: dict, kind: str | None,
                        taken_at: str | None = None,
                        name: str | None = None) -> dict | None:
    """Having a date is not the same as having the right one.

    Two different comparisons, and swapping them is a five-hour error. A
    still's DateTimeOriginal is local time with no zone, so it goes against
    Immich's `localDateTime`, which is the same wall clock. A video's
    QuickTime CreateDate is UTC by specification, so it goes against
    `fileCreatedAt`, which is the instant.
    """
    if not says.get("ok"):
        return None
    video = is_video(exif, kind)
    state, raw, _ = date_state(exif, *(VIDEO_DATE if video else PHOTO_DATE))
    if state != VALUE:
        return None

    if video:
        mine = _instant(str(raw).replace(":", "-", 2) + "Z")
        theirs = _instant(says.get("file_created_at"))
        label = "Immich's fileCreatedAt, the UTC instant"
    else:
        # The corrected clock, not the raw field. Comparing a stamped file
        # against Immich's unconverted localDateTime reports the correction
        # itself as a disagreement.
        a = _exif_dt(raw)
        b, _, _ = wall_clock(says, exif, taken_at, name)
        mine = a.timestamp() if a else None
        theirs = b.timestamp() if b else None
        label = "when Immich says it was taken"
    if mine is None or theirs is None:
        return None
    if abs(mine - theirs) <= 120:
        return {"level": "ok", "text":
                f"The date in the file agrees with {label}."}
    return {"level": "warn", "text":
            f"The file says one thing and Immich another: the file's date is "
            f"{abs(mine - theirs) / 3600:+.2f}h from {label}. Google Photos "
            "would use the file's."}


def _clock_finding(name: str, want: datetime, got: datetime,
                   exif: dict) -> dict:
    """The time in the camera's name against the file's own clock.

    Both readings are tried, on purpose. Cameras do not agree on which
    clock they name a file by: the Pixel names in UTC and records the zone
    separately, so a PXL_ name sitting exactly one offset behind
    DateTimeOriginal is the file being *right*. Reading that as a fault is
    how an entirely correct library came to look five hours broken, and it
    is why nothing here asserts a convention -- a name that fits either
    reading is not evidence of anything at all.
    """
    drift = (got - want).total_seconds() / 3600
    zone = pick(exif, "EXIF:OffsetTimeOriginal", "EXIF:OffsetTime")[0]
    off = _offset_hours(zone)

    if abs(drift) < 1 / 60:
        return {"level": "ok", "text":
                f"The name and DateTimeOriginal agree "
                f"({want:%Y-%m-%d %H:%M:%S}), so the camera named this file "
                "by the local clock."}
    if off is not None and abs(drift - off) < 1 / 60:
        return {"level": "ok", "text":
                f"DateTimeOriginal is {drift:+g}h from the name, which is "
                f"exactly the {zone} this file records: the camera named it "
                "in UTC and DateTimeOriginal is the local time. Consistent"
                + (", as a Pixel should be."
                   if filename_clock(name) == "utc" else ".")}
    if off is None:
        quarter = abs(drift * 4 - round(drift * 4)) < 0.02 and abs(drift) <= 14
        return {"level": "warn", "text":
                f"DateTimeOriginal is {drift:+g}h from the name, and this file "
                "records no time zone at all — so which of the two is the "
                "local clock cannot be settled from the file alone."
                + (" A gap landing on a whole quarter-hour is usually a zone "
                   "the camera never wrote down, which older cameras and "
                   "phones did not." if quarter else "")}
    return {"level": "bad", "text":
            f"DateTimeOriginal is {drift:+g}h from the time in the name, and "
            f"this file's own {zone} does not account for it. Neither reading "
            "of the name fits, so one of the two has been rewritten."}


def _findings(rep: dict) -> list[dict]:
    """What is wrong, said out loud.

    Never returns nothing. A silent result is indistinguishable from a
    result nobody computed, which is the failure this whole tool exists to
    stop -- a file can be five hours out and every screen still look fine.
    """
    out: list[dict] = []
    asset = rep.get("asset") or {}
    src = (rep.get("immich") or {}).get("exif") or {}
    dst = (rep.get("outbox") or {}).get("exif") or {}
    name = rep.get("filename") or ""

    # What the send did, when one was asked for. First, because it is the
    # thing the reader just pressed a button to make happen, and a refusal
    # explains everything underneath it.
    sent = rep.get("sent")
    if sent:
        out.append({"level": "ok" if sent.get("ok") else "bad",
                    "text": sent["text"]})

    for where, block in (("Immich", rep.get("immich") or {}),
                         ("the outbox", rep.get("outbox") or {})):
        err = (block.get("exif") or {}).get("error") or block.get("error")
        if err:
            out.append({"level": "bad", "text": f"Could not read {where}: {err}"})

    kind = asset.get("kind")
    # The delivered copy first, and Immich's only when there is not one.
    #
    # Every date and zone finding below is about one file, and the file that
    # matters is the one that reaches the phone. Preferring Immich's copy
    # meant they all described the untouched original -- which, once a
    # correction has been written, can never carry it, because not touching
    # Immich is the whole of invariant 3. A corrected file came back reading
    # "Immich knows when this was taken and the file does not" and "no zone
    # in the file", both stale, both alarming, directly above a green line
    # saying the outbox copy was now dated correctly.
    ref = dst if dst and "error" not in dst else src

    # 1. The question the whole thing is for: will Google Photos read a date
    #    out of this, or file it under the upload? Asked of each copy that
    #    could be read, because the answer is allowed to differ between them
    #    -- that is the entire point of stamping one on its way past.
    # The verdict trace() already computed, never a fresh one. Recomputing
    # it here dropped the modification time and the zone, so the finding
    # line and the card below it stated opposite conclusions about the same
    # file -- "would fall back to upload time" over "dated by its
    # modification time", one above the other.
    for where, block in (("Immich's original", rep.get("immich") or {}),
                         ("the outbox copy", rep.get("outbox") or {})):
        exif = block.get("exif")
        if not isinstance(exif, dict) or "error" in exif:
            continue
        v = block.get("verdict") or verdict(exif, kind, name=name)
        # Tagged, because the dashboard draws these as their own cards and
        # a reader should not be told the same thing twice. The line stays
        # in the findings list: that list is the machine-readable answer and
        # the thing that must never come back empty.
        # Once there is a delivered copy, it is the answer and Immich's is
        # background. Immich's original will always read as undated for
        # exactly the files this tool corrects -- correcting it is
        # forbidden -- so scoring it pass/fail puts a cross beside a file
        # that has just been fixed.
        context = (where == "Immich's original"
                   and isinstance((rep.get("outbox") or {}).get("exif"), dict))
        out.append({"level": "note" if context else v["level"],
                    "kind": "verdict", "context": context,
                    "text": f"{where}: {v['headline']}. {v['reason']}"})

    # 2. And having a date is not the same as having the right one.
    if ref and "error" not in ref:
        agree = _agrees_with_immich(ref, rep.get("says") or {}, kind,
                                    asset.get("taken_at"), name)
        if agree:
            out.append(agree)

    # 3. Immich holding a date the file does not is the entire fault, and
    #    the mismatch counter is blind to it by construction: it compares
    #    fileCreatedAt against exifInfo.dateTimeOriginal, and on a Takeout
    #    import both were filled from the same sidecar, so they agree.
    says = rep.get("says") or {}
    if ref and "error" not in ref and says.get("ok"):
        video = is_video(ref, kind)
        state, _, _ = date_state(ref, *(VIDEO_DATE if video else PHOTO_DATE))
        if state in (BLANK, MISSING, UNREADABLE) and says.get("local_date_time"):
            local, off, zkind = wall_clock(says, ref, asset.get("taken_at"),
                                           name)
            when = (local.strftime("%Y-%m-%d %H:%M:%S") if local else
                    str(says["local_date_time"]).replace("T", " ")[:19])
            zone = says.get("time_zone")
            label = (f" in {zone}" if zkind != "assumed" and zone
                     else f", reading it at {zone_source(ref, says, asset.get('taken_at'), name)[1]} "
                          "by this library's rule" if zkind == "assumed"
                     else ", with no zone recorded")
            out.append({"level": "warn", "text":
                        f"Immich knows when this was taken — {when}"
                        + label
                        + " — and the file does not. That date is Immich's own "
                        "record, read from a Google Takeout sidecar at import, "
                        "and it never reached the file. Nothing on the "
                        "Problems tab can see this: the mismatch figures "
                        "compare two fields that were both filled from that "
                        "same sidecar, so they agree, and a library of "
                        "undated files reads as zero."})

    # 3b. What the name says about the moment. Independent of every tag in
    #     the file and of Immich's zone, which is what makes it worth
    #     saying out loud even when nothing here acts on it.
    told = name_reading(name, asset.get("taken_at"), is_video(ref or {}, kind))
    if told["verdict"] == "zone":
        out.append({"level": "ok", "text":
                    f"The time in the name is {offset_text(told['offset'])} "
                    f"from the moment Immich holds, which is a real zone — so "
                    f"the camera wrote the local clock into the name, and that "
                    f"gap is the offset this was taken at."})
    elif told["verdict"] == "made":
        out.append({"level": "note", "text":
                    "Google Photos made this file and named it after one of "
                    "its sources, so the date in the name belongs to a "
                    "different photo and nothing here reads it."})
    elif told["verdict"] == "disagrees":
        gap = told["gap"] or 0
        span = (f"{abs(gap) / 24:.0f} days" if abs(gap) >= 36
                else f"{abs(gap):.2g} hours")
        out.append({"level": "warn", "text":
                    f"The name says {told['when']:%Y-%m-%d %H:%M:%S} and "
                    f"Immich says {str(asset.get('taken_at'))[:19].replace('T', ' ')} "
                    f"— {span} apart, which is no zone. One of the two is "
                    "about something else: a date read from a sidecar at "
                    "import, or a camera whose clock was wrong. Nothing here "
                    "chooses between them."})

    # 3c. What else the sidecar carried and the file never got. Not a date,
    #     and reported rather than acted on: nothing here writes these.
    for gap in gaps(ref or {}, rep.get("says") or {}, kind):
        out.append({"level": "warn", "text":
                    f"Immich holds a {gap['what']} for this photo that the "
                    f"file does not carry — {gap['immich']}. Same cause as a "
                    f"missing date: it came from the Takeout sidecar at "
                    f"import and never reached the file. {gap['note']} "
                    "Nothing here writes it."})

    # 4. Whether the zone is known at all. A wall clock with no zone behind
    #    it is a number, not a time, and this library spans a move.
    if ref and "error" not in ref and (rep.get("says") or {}).get("ok"):
        src_kind, zone = zone_source(ref, rep["says"], asset.get("taken_at"),
                                     name)
        place = (rep["says"] or {}).get("place")
        if src_kind == "file":
            out.append({"level": "ok", "text":
                        f"The file records its own time zone ({zone}), which "
                        "settles the reading whatever else is missing."})
        elif src_kind == "gps":
            out.append({"level": "ok", "text":
                        f"The zone is {zone}, which Immich derived from the "
                        "coordinates in the file"
                        + (f" ({place})." if place else ".")})
        elif src_kind == "immich":
            out.append({"level": "warn", "text":
                        f"The only zone going is Immich's own default, "
                        f"{zone}. The file carries no offset and Immich has "
                        "no coordinates to derive one from, so this is where "
                        "the server was rather than where the photo was "
                        "taken. Set the rule in Settings if you know better."})
        elif src_kind == "assumed":
            out.append({"level": "warn", "text":
                        f"No zone in the file and no coordinates, so this "
                        f"library's own rule applies: taken before "
                        f"{settings.load().assume_zone_before}, so "
                        f"{zone}. That is a decision about where its owner "
                        "was living, not a reading — it is right for a photo "
                        "taken at home and wrong for one taken on a trip."})
        else:
            out.append({"level": "warn", "text":
                        "No time zone anywhere: not in the file, and no "
                        "coordinates for Immich to derive one from. Immich "
                        "reports UTC because it has nothing else, so the "
                        "time it displays is the UTC instant wearing a local "
                        "label — a photo taken at 11:20 in Karachi reads as "
                        "06:20 here and in Google Photos, and the two agreeing "
                        "is not evidence either is right. Only the date it "
                        "was taken can settle this, and that is a decision "
                        "rather than a reading."})

    # 5. The camera's own name against the file's own clock.
    want = filename_time(name)
    got = _exif_dt(pick(ref, *(VIDEO_DATE if is_video(ref, kind)
                               else PHOTO_DATE))[0])
    if want and got:
        out.append(_clock_finding(name, want, got, ref))

    # 6. The two copies against each other -- the promise the relay is
    #    actually on the hook for.
    a, b = rep.get("immich") or {}, rep.get("outbox") or {}
    if a.get("ok") and b.get("present"):
        if a.get("sha256") == b.get("sha256"):
            out.append({"level": "ok", "text":
                        "The outbox copy is byte-for-byte identical to "
                        "Immich's original."})
        else:
            a_d, b_d = _dates(src, kind, downloaded=True), _dates(dst, kind)
            changed = sorted({k for k in set(a_d) | set(b_d)
                              if a_d.get(k) != b_d.get(k)})
            cfg = settings.load()
            if asset.get("stamped_at"):
                # Told apart from corruption by the ledger, because from here
                # the two look identical -- which is the confusion this whole
                # tool exists to end, and introducing a fresh instance of it
                # while fixing one would be careless.
                out.append({"level": "ok", "text":
                            "The outbox copy differs from Immich's original "
                            "because a missing capture date was written into "
                            f"it here on {str(asset['stamped_at'])[:19]}: "
                            f"{asset.get('stamped_note') or 'date tags'}. "
                            "Immich's own file is untouched."})
            else:
                why = ("date rewriting is on and this asset is flagged as "
                       "corrected"
                       if cfg.fix_dates and asset.get("date_mismatch")
                       else "date rewriting is OFF for this asset, so nothing "
                            "here should have altered it")
                out.append({"level": "bad", "text":
                            f"The outbox copy differs from Immich's original "
                            f"({a.get('bytes')} vs {b.get('bytes')} bytes) — "
                            f"{why}."
                            + (f" Tags that differ: {', '.join(changed)}."
                               if changed else "")})

    # 7. Whatever exiftool wanted to complain about.
    for where, exif in (("Immich's copy", src), ("the outbox copy", dst)):
        w = exif.get("Warning")
        for line in (w if isinstance(w, list) else [w] if w else []):
            out.append({"level": "warn",
                        "text": f"exiftool on {where}: {line}"})

    # 8. What the ledger believes, against what the file says.
    #
    # DateTimeOriginal is local time, so turning it into an instant needs a
    # zone -- the one `zone_source` chose, not zero. Reading "no offset tag"
    # as UTC made every correctly dated photo in a GMT+5 library disagree
    # with Immich by exactly five hours, and said so in the same report that
    # had already found the two agreed. Two contradictory lines, one screen.
    #
    # Nor does it apply to a copy this service has written into: that
    # difference is explained, recorded, and reported above.
    if got and asset.get("exif_taken_at") and not asset.get("stamped_at"):
        led = db.capture_time(asset["exif_taken_at"])
        zkind, zone = zone_source(ref, rep.get("says") or {},
                                  asset.get("taken_at"), name)
        off = zone_hours(zkind, zone, asset.get("taken_at"))
        if led is not None and off is not None:
            file_instant = got.replace(tzinfo=timezone.utc).timestamp() \
                - off * 3600
            if abs(led - file_instant) > 120:
                out.append({"level": "warn", "text":
                            f"The date in the file is not the one Immich read "
                            f"out of it at import — {got:%Y-%m-%d %H:%M:%S} "
                            f"at {offset_text(off)} against "
                            f"{str(asset['exif_taken_at'])[:19].replace('T', ' ')} "
                            "UTC. Either the file has been rewritten since, or "
                            "the date in Immich was corrected there and the "
                            "file never heard about it."})

    if not out:
        out.append({"level": "warn", "text":
                    "Nothing could be compared — see the three copies above "
                    "for which of them could not be read."})
    return out


# ---------------------------------------------------------------------------
# Phase 2: what a correction would be. Nothing here writes, and nothing
# here is reachable from a write path -- it returns a description.
# ---------------------------------------------------------------------------

def offset_text(hours: float) -> str:
    """5.0 as "+05:00", and 5.75 as "+05:45".

    Formatted from whole minutes rather than a rounded hour: India is at
    +05:30 and Nepal at +05:45, and a format that rounds the hour writes
    +06:30 for the first of those.
    """
    sign = "-" if hours < 0 else "+"
    total = int(round(abs(hours) * 60))
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def _needs_correction(v: dict) -> bool:
    """Does the delivered copy carry a date Google Photos will read right?

    Two populations fail, and they fail differently. A tag that is blank or
    missing leaves nothing to read -- some of those are rescued by the
    modification time and some are not. And a file rescued by its mtime is
    still wrong by the capture zone's offset, because an mtime is an instant
    and Google Photos shows it as UTC.
    """
    if not v:
        return False
    if v.get("state") == VALUE:
        return False
    # Undated, or dated only by an mtime that is the wrong number of hours.
    return not v.get("dated") or v.get("level") == "warn"


def propose(rep: dict) -> dict:
    """Exactly what would be written, and where each value came from.

    EXIF gives no choice between "correct the time" and "record the zone":
    DateTimeOriginal is defined as local time with no zone, so the value
    written *is* the wall clock and the offset is what stops it being
    ambiguous. Writing the UTC instant there and an offset beside it says
    the photo was taken five hours earlier than it was.
    """
    out: dict = {"needed": False, "writes": []}
    asset = rep.get("asset") or {}
    says = rep.get("says") or {}
    ob = rep.get("outbox") or {}
    v = ob.get("verdict") or (rep.get("immich") or {}).get("verdict")

    if not v:
        out["why"] = ("Neither copy could be read, so there is nothing to "
                      "base a correction on.")
        return out
    if not _needs_correction(v):
        out["why"] = (f"Nothing to correct: {v['headline'].lower()}.")
        return out
    if not says.get("ok"):
        out["why"] = ("Immich could not be asked what it holds, and every "
                      "value a correction would use comes from there.")
        return out

    ref = (ob.get("exif") if isinstance(ob.get("exif"), dict) else None) or \
        ((rep.get("immich") or {}).get("exif") or {})
    taken_at = asset.get("taken_at")
    name = rep.get("filename") or asset.get("name")
    local, off, zkind = wall_clock(says, ref, taken_at, name)
    zone = zone_source(ref, says, taken_at, name)[1]

    if is_video(ref, asset.get("kind")):
        # QuickTime's CreateDate is UTC by specification -- the opposite of
        # a still, and the one place the instant is the right value.
        when = db.capture_time(taken_at)
        if when is None:
            out["why"] = "Immich holds no capture date for this video."
            return out
        out["needed"] = True
        out["kind"] = "video"
        out["writes"] = [{
            "tag": "QuickTime:CreateDate",
            "value": datetime.fromtimestamp(when, timezone.utc).strftime(EXIF_FMT),
            "from": "Immich's fileCreatedAt, the UTC instant",
            "why": "QuickTime records this in UTC by specification, unlike "
                   "every EXIF date tag, so the instant is the right value "
                   "and no offset belongs beside it."}]
        out["why"] = v["headline"]
        return out

    if local is None:
        out["why"] = ("Immich holds no capture date for this file, so there "
                      "is nothing to write.")
        return out
    if off is None:
        out["why"] = (
            "The time zone is not known: the file carries none, Immich has "
            "no coordinates to derive one from, and this library's rule "
            "does not cover a photo of this date. DateTimeOriginal is local "
            "time, so without a zone there is no way to say what to write — "
            "the instant is known and the wall clock is not. Set the rule in "
            "Settings, or leave this one alone.")
        return out

    src = {
        "file": "the file's own OffsetTimeOriginal",
        "gps": f"Immich's timeZone ({says.get('time_zone')}), derived from "
               f"the coordinates in the file",
        "immich": f"Immich's timeZone ({says.get('time_zone')})",
        "assumed": f"this library's rule — taken before "
                   f"{settings.load().assume_zone_before}, so {zone}",
        "name": f"the filename, which is {zone} from Immich's instant — the "
                f"camera wrote the local clock into the name, so the gap "
                f"between the two is the zone it was taken at",
    }[zkind]
    clock = {
        "assumed": f"Immich's fileCreatedAt, the capture instant, plus {zone}. "
                   "Under the rule Immich's own zone is set aside, and so is "
                   "the localDateTime it converted through that zone",
        "name": "the time in the filename, which is the local clock the "
                "camera was reading. Immich's own conversion is not used: "
                "it converted through a zone this outranks",
    }.get(zkind,
          "Immich's localDateTime, the wall clock, with its Z discarded")
    stamp = offset_text(off)

    out["needed"] = True
    out["kind"] = "photo"
    out["why"] = v["headline"]
    out["writes"] = [
        {"tag": "DateTimeOriginal", "value": local.strftime(EXIF_FMT),
         "from": clock,
         "why": "EXIF defines this as local time with no zone, which is why "
                "the value is the wall clock and not the instant. This is "
                "the tag Google Photos reads."},
        {"tag": "OffsetTimeOriginal", "value": stamp, "from": src,
         "why": "What stops the line above being ambiguous. Without it the "
                "file is right only for as long as somebody remembers where "
                "it was taken."},
        {"tag": "CreateDate", "value": local.strftime(EXIF_FMT),
         "from": clock,
         "why": "The digitised date, same clock and same rule. Written so "
                "the offset below modifies a tag that exists."},
        {"tag": "OffsetTimeDigitized", "value": stamp, "from": src,
         "why": "The pair of the line above."},
    ]
    out["untouched"] = (
        "Nothing else. The image data is not re-encoded, no other tag is "
        "written, and a file whose DateTimeOriginal already holds a value "
        "is never overwritten.")
    return out


# ---------------------------------------------------------------------------
# Phase 4: classifying a file on its way past, while its bytes are in hand.
#
# Immich's metadata cannot answer this. Its date fields are filled from the
# Takeout sidecar at import and say nothing about what is in the file --
# which is the whole reason a library of undated photos reads as zero on the
# Problems tab. Only the bytes know, and the one moment they are here is
# during delivery, in the temporary file, before the rename into the outbox.
# ---------------------------------------------------------------------------

BLANK_FAULT, ABSENT_FAULT, UNFIXABLE, FINE = "blank", "absent", "unfixable", "ok"
# A fault nobody kept. Builds before 2.16.2 stored "unfixable" *instead of*
# blank or absent, so a row that has since become fixable cannot say which.
UNRECORDED = "unrecorded"


async def classify(path: str, row: dict) -> dict:
    """What is wrong with this file's date, and what would put it right.

    Returns a verdict the feeder can act on without knowing any of this:
    `hold` says whether to keep it back, and `writes` is what would be
    written if it goes on. Never raises -- a file that cannot be classified
    is delivered exactly as it would have been before any of this existed,
    because a diagnostic that can stop a backup is worse than no diagnostic.
    """
    out = {"hold": False, "kind": FINE, "zone": None, "writes": [],
           "why": "", "checked_at": db.now(), "checked_sum": row.get("checksum")}
    try:
        exif = read_exif(path)
        if "error" in exif:
            out["why"] = exif["error"]
            return out

        kind = row.get("kind")
        video = is_video(exif, kind)
        state, raw, _ = date_state(exif, *(VIDEO_DATE if video else PHOTO_DATE))

        # Asked for every file, not only the faulty ones. A photo with a
        # perfectly good date can still have gone to Google Photos with no
        # location, and that is the same sidecar and the same loss -- so
        # counting only the ones held back would miss nearly all of it. It
        # costs one small metadata call beside a download of the whole file.
        says = await immich.asset_detail(row["id"])
        if says.get("ok"):
            out["says"] = says          # kept, so a verdict can be redone
        out["gaps"] = [g["what"] for g in gaps(exif, says, kind)]

        if state == VALUE:
            # It carries its own date. Nothing here second-guesses that;
            # a date that disagrees with Immich is fix_dates' business.
            out["why"] = f"carries {str(raw).strip()}"
            return out
        zkind, zone = zone_source(exif, says if says.get("ok") else {},
                                  row.get("taken_at"), row.get("filename"))
        out["zone"] = zkind
        out["kind"] = BLANK_FAULT if state == BLANK else ABSENT_FAULT

        prop = propose({
            "filename": row.get("filename"),
            "asset": {"id": row.get("id"), "kind": kind,
                      "taken_at": row.get("taken_at")},
            "says": says,
            "outbox": {"present": True, "exif": exif,
                       "verdict": verdict(exif, kind, says=says,
                                          taken_at=row.get("taken_at"),
                                          name=row.get("filename"))},
        })
        if not prop.get("needed"):
            # Nothing can be written: no zone to be had, or Immich holds no
            # date either. Held all the same, because it is going to land
            # wrong and saying so is the point -- and with no writes, which
            # is what marks it as the ceiling rather than as work waiting to
            # be signed off.
            #
            # The fault is kept regardless. It used to be overwritten with
            # "unfixable", which is a statement about the settings rather
            # than the file -- so once a rule reached the file, nothing could
            # say any more whether its tag had been empty or missing.
            out["hold"] = True
            out["why"] = prop.get("why", "")
            return out

        out["writes"] = prop["writes"]
        out["hold"] = True
        out["why"] = prop.get("why", "")
        return out
    except Exception as exc:  # noqa: BLE001
        out["why"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return out


def rejudge(row: dict) -> dict:
    """Work a held file's verdict out again from what was kept about it.

    A held file is never fetched a second time, so its stored answer is
    frozen at whatever the build and the settings said that day -- and a
    rule changed afterwards never reaches it. Everything needed is already
    here: the fault came from the file and does not change, and the zone
    comes from Immich and the rule, both of which are in hand.

    `file` and `gps` are left alone. Those are readings, and no setting
    improves on them.

    What comes back is what a sign-off writes, not only what the page draws:
    `/api/dates/release` calls this again at the moment of signing and
    stores the result. It used to redraw the page and leave the row alone,
    so a file re-judged on screen was signed off carrying the answer the
    screen had just corrected.

    `stale` marks the one case nothing here can redo -- read before Immich's
    answer was kept, and outside the rule -- where only reading the file
    again can say. `revised_from` is the value the earlier answer proposed,
    when this one differs from it.
    """
    out = dict(row)
    out["stale"] = False
    # Independent of every tag, of Immich's zone and of the rule, so it is
    # worked out for every row including the ones judged no further.
    told = name_reading(row.get("filename"), row.get("taken_at"),
                        (row.get("kind") or "").upper() == "VIDEO")
    if told["verdict"] != "none":
        out["name_says"] = {
            "verdict": told["verdict"],
            "when": told["when"].strftime("%Y-%m-%d %H:%M:%S") if told["when"]
                    else None,
            "gap": None if told["gap"] is None else round(told["gap"], 2),
            "offset": told["offset"]}
    if row.get("hold_zone") in ("file", "gps"):
        return out
    says = row.get("says") or _from_the_ledger(row)
    if not says:
        out["stale"] = True
        return out

    kind_was = row.get("hold_kind")
    exif = {"EXIF:DateTimeOriginal": ""} if kind_was == BLANK_FAULT else {}
    if (row.get("kind") or "").upper() == "VIDEO":
        exif["File:MIMEType"] = "video/mp4"
    prop = propose({
        "filename": row.get("filename"),
        "asset": {"id": row.get("id"), "kind": row.get("kind"),
                  "taken_at": row.get("taken_at")},
        "says": says,
        "outbox": {"present": True, "exif": exif,
                   "verdict": verdict(exif, row.get("kind"), says=says,
                                      taken_at=row.get("taken_at"),
                                      name=row.get("filename"))},
    })
    out["hold_zone"] = zone_source(exif, says, row.get("taken_at"),
                                   row.get("filename"))[0]
    out["writes"] = prop.get("writes") or []
    if not out["writes"]:
        out["why"] = prop.get("why", "")
    elif kind_was in (UNFIXABLE, None):
        # It was the ceiling and is not any more -- and the build that said
        # so kept only that, not whether the tag was empty or missing. The
        # correction is the same either way; the label is not known.
        out["hold_kind"] = UNRECORDED

    was = row.get("writes") or []
    if was and _headline(was) != _headline(out["writes"]):
        out["revised_from"] = _headline(was)
    return out


def _from_the_ledger(row: dict) -> dict | None:
    """What a rule verdict needs, for a row held before Immich's answer was
    kept.

    Under the rule the wall clock is the capture instant plus the rule's
    offset, and nothing else -- Immich's zone and the `localDateTime` it
    converted through that zone are exactly what the rule sets aside. The
    ledger already holds the instant: `taken_at` is Immich's `fileCreatedAt`,
    copied at scan time. So nothing needs fetching.

    A coordinate or an offset in the file would have outranked the rule,
    but either would have filed the row under `gps` or `file` when it was
    read, and those never reach here. Outside the rule the zone is Immich's
    own, which only a fresh read supplies -- so this declines, and the row
    is marked stale instead of being given an answer it has no basis for.
    """
    taken = row.get("taken_at")
    if not taken or not _assumed(taken):
        return None
    return {"ok": True, "file_created_at": taken, "local_date_time": taken}


def _headline(writes: list[dict]) -> str | None:
    """The one value in a correction that decides where the photo lands."""
    for w in writes or []:
        if w.get("tag") in ("DateTimeOriginal", "QuickTime:CreateDate"):
            return w.get("value")
    return None


# ---------------------------------------------------------------------------
# Phase 3: applying one correction. The only place in this module that
# writes, and it writes to the outbox copy and nothing else.
# ---------------------------------------------------------------------------

def write_tags(path: str, writes: list[dict]) -> tuple[bool, str]:
    """Write the proposed tags into a file, in place.

    Only ever called on a file nothing else can see yet -- the `.partial-`
    the feeder is still filling, or the temporary copy `_stamp` makes. A
    file already in the outbox is edited through `_stamp`, which stages and
    renames, because Syncthing is watching that directory.
    """
    if not writes:
        return True, ""
    args = [f"-{w['tag']}={w['value']}" for w in writes]
    try:
        r = subprocess.run(
            ["exiftool", "-overwrite_original", "-P", *args, "-q", path],
            capture_output=True, timeout=180, check=False)
    except FileNotFoundError:
        return False, ("exiftool is not installed in this image, so nothing "
                       "can be written.")
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.returncode != 0:
        detail = (r.stderr or b"").decode(errors="replace").strip()[:200]
        return False, f"exiftool refused the file: {detail}"
    return True, ""


def _stamp(path: str, writes: list[dict]) -> tuple[bool, str]:
    """Write the proposed tags into a copy, then move it over the original.

    Never in place. The outbox is a Syncthing folder, so a file edited where
    it lies is a file Syncthing may start transferring halfway through the
    edit. The delivery path solved this already and this follows it exactly:
    a `.partial-` dotfile inside the outbox -- inside, because `/mnt/user` is
    a FUSE overlay and a rename across two of its directories fails with
    EXDEV -- and `os.replace`, which within one directory is atomic.

    `sweep_partials()` already knows both names this can leave behind, its
    own and exiftool's, so a crash mid-write cleans up on the next cycle.
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=config.OUTBOX_DIR,
                                   prefix=".partial-", suffix=".part")
        os.close(fd)
        shutil.copy2(path, tmp)          # copy2: the mtime comes with it
        ok, why = write_tags(tmp, writes)
        if not ok:
            return False, why
        os.replace(tmp, path)
        os.chmod(path, 0o664)
        tmp = None
        return True, ""
    except FileNotFoundError:
        return False, ("exiftool is not installed in this image, so nothing "
                       "can be written. Everything else still works.")
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def apply_correction(filename: str) -> dict:
    """Write the proposal into the outbox copy of one file.

    Everything is re-derived here rather than trusted from the page: the
    trace runs again, the proposal is computed again, and the file is read
    again afterwards to confirm the tag is in it. A proposal is a
    description of a file as it was, and the file can have moved on.
    """
    rep = await trace(filename)
    out: dict = {"filename": rep.get("filename"), "trace": rep}
    if rep.get("problem"):
        out["ok"] = False
        out["text"] = rep["problem"]
        return out

    # Asked before the proposal, because it is the more useful answer and
    # it holds whatever Immich had to say: with nothing in the outbox there
    # is nothing this may write to, and why the proposal came out empty is
    # a second-order question.
    ob = rep.get("outbox") or {}
    if not ob.get("present"):
        out["ok"] = False
        out["text"] = ("There is no outbox copy to correct. Send it first — "
                       "the correction is applied to the file on its way to "
                       "the phone, never to Immich's original.")
        return out

    prop = rep.get("proposal") or {}
    if not prop.get("needed"):
        out["ok"] = False
        out["text"] = ("Nothing to write. " + (prop.get("why") or ""))
        return out

    # Re-read rather than trust the report: never overwrite a date that is
    # already there. The check that matters is made against the file, at the
    # moment of writing.
    exif = read_exif(ob["path"])
    if "error" in exif:
        out["ok"] = False
        out["text"] = f"Could not read the outbox copy: {exif['error']}"
        return out
    video = is_video(exif, (rep.get("asset") or {}).get("kind"))
    state, raw, _ = date_state(exif, *(VIDEO_DATE if video else PHOTO_DATE))
    if state == VALUE:
        out["ok"] = False
        out["text"] = (f"The outbox copy already carries a date "
                       f"({str(raw).strip()}), and a date that is there is "
                       "never overwritten. Whatever the report said, the "
                       "file has moved on since.")
        return out

    ok, why = _stamp(ob["path"], prop["writes"])
    if not ok:
        out["ok"] = False
        out["text"] = f"Nothing was written: {why}"
        return out

    # The mtime carries the date for anything that cannot read the tag, and
    # exiftool's -P preserves whatever the copy had. Set it deliberately
    # rather than relying on that.
    feeder.stamp_capture_time(ob["path"], (rep.get("asset") or {}).get("taken_at"))

    note = ", ".join(f"{w['tag']}={w['value']}" for w in prop["writes"])
    c = db.connect()
    with db._lock:  # noqa: SLF001
        # Both: the sentence is for a person reading a trace, and the JSON
        # is what a future "push this into Immich" would replay. A record
        # kept only as prose would have to be parsed back into tags.
        c.execute("UPDATE assets SET stamped_at = ?, stamped_note = ?, "
                  "                  hold_writes = ? WHERE id = ?",
                  (db.now(), note, json.dumps(prop["writes"]),
                   rep["asset"]["id"]))
        c.commit()
        db._bump()  # noqa: SLF001
    db.log("info", f"{filename}: wrote a missing capture date into the "
                   f"outbox copy — {note}")

    # Read it back. A write nobody verified is a claim.
    after = read_exif(ob["path"])
    st2, raw2, _ = date_state(after, *(VIDEO_DATE if video else PHOTO_DATE))
    out["ok"] = st2 == VALUE
    out["text"] = (
        f"Written, and read back: the outbox copy now carries "
        f"{str(raw2).strip()}. Syncthing will take it to the phone. It is no "
        "longer byte-for-byte identical to Immich's original, deliberately, "
        "and the ledger records that so a later trace does not read it as "
        "damage." if out["ok"] else
        "exiftool reported success but the tag is not in the file when read "
        "back. Nothing here can explain that; treat the file as untouched "
        "and look at the log.")
    out["written"] = prop["writes"]
    return out


async def trace(filename: str, send: bool = False,
                asset_id: str | None = None) -> dict:
    """The whole report for one file."""
    rep: dict = {"filename": (filename or "").strip(), "asked_at": db.now()}
    if not rep["filename"] and not asset_id:
        rep["problem"] = "Type a filename first — the name as Immich has it, " \
                         "for example PXL_20230101_025759225.jpg."
        return rep

    row, near, namesakes = find(rep["filename"], asset_id)
    if namesakes:
        # Answering about one of six photographs with the same name, without
        # saying which, is worse than not answering.
        rep["problem"] = (f"{len(namesakes)} files in the ledger are called "
                          f"{rep['filename']!r} — a camera restarts its "
                          "counter, so a name is not an identity here. Which "
                          "one?")
        rep["choices"] = namesakes
        return rep
    if row is None:
        rep["problem"] = (f"No asset in the ledger is called "
                          f"{rep['filename']!r}. Names are matched exactly as "
                          "Immich holds them, so a path or a renamed copy will "
                          "not be found.")
        rep["suggestions"] = near
        if near:
            rep["problem"] += f" {len(near)} near match(es) below."
        return rep

    rep["filename"] = row.get("filename") or rep["filename"]
    rep["asset"] = {k: row.get(k) for k in
                    ("id", "filename", "state", "kind", "size", "taken_at",
                     "exif_taken_at", "date_mismatch", "forced", "outbox_name",
                     "attempts", "last_error", "missing_at",
                     "stamped_at", "stamped_note")}

    if send:
        rep["sent"] = await _send_now(row)
        row = dict(db.connect().execute(
            "SELECT * FROM assets WHERE id = ?", (row["id"],)).fetchone())
        rep["asset"]["outbox_name"] = row.get("outbox_name")
        rep["asset"]["state"] = row.get("state")

    # What Immich itself holds. A photo imported from a Takeout sidecar can
    # show a perfectly good date here while the file carries none at all,
    # and without this side of it there is no way to see that from a screen.
    rep["says"] = await immich.asset_detail(row["id"])

    rep["immich"] = await _immich_copy(row["id"], int(row.get("size") or 0))
    rep["outbox"] = _outbox_copy(row.get("outbox_name"))
    rep["phone"] = await _phone_copy(row.get("outbox_name"))
    kind = row.get("kind")
    for key in ("immich", "outbox"):
        exif = rep[key].get("exif")
        # Only when there is something to have read. An empty `dates` on a
        # copy that could not be opened reads as "no dates in the file",
        # which is a different and much calmer statement than the truth.
        if isinstance(exif, dict) and "error" not in exif:
            got = key == "immich"      # fetched to a temp file, not read in place
            rep[key]["dates"] = _dates(exif, kind, downloaded=got)
            rep[key]["tags"] = _tag_table(exif, kind, downloaded=got)
            # Only a copy read in place has a modification time worth
            # anything; Immich's was downloaded moments ago.
            rep[key]["verdict"] = verdict(exif, kind,
                                          mtime=rep[key].get("mtime"),
                                          taken_at=row.get("taken_at"),
                                          says=rep.get("says"),
                                          downloaded=got,
                                          name=row.get("filename"))
    # One corrected wall clock, computed once and handed to the page, so it
    # cannot print a different time from the findings beneath it.
    if (rep.get("says") or {}).get("ok"):
        # Same order as _findings, and for the same reason: the corrected
        # copy is the one whose zone the page should be reading.
        ref = (rep["outbox"].get("exif") if isinstance(
            rep["outbox"].get("exif"), dict) else None) or {}
        if not ref or "error" in ref:
            ref = (rep["immich"].get("exif") or {})
        local, off, zkind = wall_clock(rep["says"], ref, row.get("taken_at"),
                                       row.get("filename"))
        rep["says"]["wall_clock"] = (
            local.strftime("%Y-%m-%d %H:%M:%S") if local else None)
        rep["says"]["zone_used"] = zone_source(ref, rep["says"],
                                               row.get("taken_at"),
                                               row.get("filename"))[1]
        rep["says"]["zone_from"] = zkind

    rep["findings"] = _findings(rep)
    # Computed every time and revealed on request: it reads nothing the
    # trace has not already read, so a second round trip would only buy a
    # second download of Immich's copy.
    rep["proposal"] = propose(rep)
    return rep


def _why_not_sendable(row: dict) -> str | None:
    """The reason this asset will not go out, or None if it will.

    `forced` bypasses the date window and nothing else: claim_batch still
    excludes confirmed assets, motion components, video when video is off,
    anything over the size ceiling and anything held back by a date
    mismatch. Every one of those made the button do nothing, and it said so
    nowhere -- which is the exact failure this tool was built to end,
    committed by the tool itself.
    """
    cfg = settings.load()
    state = row.get("state")
    # A confirmed asset may be sent again from here, and only from here.
    #
    # Invariant 4 exists because re-sending duplicates a photo. That is true
    # of a file whose bytes have changed and false of one whose have not:
    # Google Photos matches an upload against what it already holds, so an
    # identical file is recognised, not added, and Free up space clears it
    # again on the next run. Which makes a deliberate re-send the only way
    # to see what actually leaves this building for a file whose outbox copy
    # was cleared months ago -- and those are the files worth asking about,
    # since a wrong date is noticed in Google Photos, long after the fact.
    #
    # The condition is the bytes, so that is what is checked. Nothing
    # automatic re-sends anything: claim_batch still excludes 'confirmed',
    # and this is a button in Tools pressed at one named file.
    if state == "confirmed" and cfg.fix_dates and feeder.needs_date_fix(
            row.get("taken_at"), row.get("exif_taken_at")):
        return ("it is already confirmed and 'Write corrected dates' would "
                "alter it on the way out. A changed file is a new photo to "
                "Google Photos, so this one really would arrive as a "
                "duplicate rather than being recognised. Turn that setting "
                "off to send it untouched")
    if state == "confirmed" and row.get("stamped_at"):
        # The exception above rests on the bytes being unchanged. They are
        # not: a date was written into this one, so Google Photos sees a file
        # it has never held and adds it rather than recognising it.
        return ("it is already confirmed and a capture date was written into "
                "it here, so it is no longer the file Google Photos already "
                "holds. Sending it again would add a second copy rather than "
                "being recognised as the one it has")
    if row.get("missing_at"):
        return ("Immich no longer serves the original: the asset is in the "
                "ledger but its file is offline or moved out of an external "
                "library")
    if db.motion_parts_among([row["id"]]):
        return ("it is the video half of a motion photo. The still carries "
                "the clip inside it, and relaying the component on its own "
                "would put a stray video in Google Photos")
    if (row.get("kind") or "").upper() == "VIDEO" and not cfg.include_video:
        return "video is switched off in Settings"
    size = int(row.get("size") or 0)
    if size and size > cfg.max_asset_bytes:
        return (f"it is {size / 1e6:.0f} MB, over the {cfg.max_asset_mb} MB "
                "per-file ceiling in Settings")
    if row.get("date_mismatch") and not cfg.fix_dates:
        return ("its date was corrected in Immich and 'Write corrected "
                "dates' is off, so sending it would hand Google Photos the "
                "stale date — which it then keeps")
    if cfg.paused:
        return "the relay is paused"
    ready, detail = feeder.outbox_ready()
    if not ready:
        return f"the outbox is not there: {detail}"
    return None


async def _send_now(row: dict) -> dict:
    """Push this one asset through, so there is a second copy to compare.

    It used to return ok:True whether or not a byte moved, and the page
    never showed the answer either way. A file blocked by any of nine
    different conditions came back looking exactly like a file that had
    never been asked for.
    """
    # A name in the ledger is not a file in the outbox. A confirmed asset
    # keeps its `outbox_name` for good -- the file left the outbox because
    # Google Photos cleared it off the phone, which is *how* it was
    # confirmed -- so asking the ledger here would announce "already in the
    # outbox" about a file that demonstrably is not, and on exactly the
    # kind of file somebody traces: one they found in Google Photos wearing
    # the wrong date.
    name = row.get("outbox_name")
    if name and os.path.exists(os.path.join(config.OUTBOX_DIR, name)):
        return {"ok": True, "moved": False, "text":
                f"Already in the outbox as {name}, so nothing was sent — the "
                "two copies below are the ones already there."}

    why = _why_not_sendable(row)
    if why:
        text = f"Not sent, because {why}"
        return {"ok": False, "moved": False,
                "text": text if text.endswith(".") else text + "."}

    c = db.connect()
    with db._lock:  # noqa: SLF001
        # Unconditionally 'pending', confirmed included. That CASE was the
        # guard, and _why_not_sendable is the guard now -- a narrower one,
        # testing whether the bytes will change rather than whether the file
        # has been somewhere.
        c.execute("UPDATE assets SET forced=1, attempts=0, last_error=NULL, "
                  "state='pending' WHERE id = ?", (row["id"],))
        c.commit()
        db._bump()  # noqa: SLF001
    try:
        async with feeder.CYCLE_LOCK:
            _, used = feeder.reconcile()
            await feeder.top_up(used)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "moved": False, "text":
                f"The send failed: {type(exc).__name__}: {str(exc)[:200]}"}

    # Did anything actually move? top_up reports no per-asset outcome, and
    # the cap can decline this file without raising anything at all.
    #
    # The name is not the evidence -- `outbox_name` is recorded when the
    # transfer is set up and survives the download failing, so a file that
    # never arrived still carries one. Only the file being on disk means it
    # was sent. (Nothing is at risk from that either way: confirmation
    # needs state='queued' AND seen_on_phone=1, and a failed asset is
    # neither. But reporting "Sent" over an empty outbox is its own lie.)
    after = db.connect().execute("SELECT * FROM assets WHERE id = ?",
                                 (row["id"],)).fetchone()
    after = dict(after) if after else {}
    if after.get("last_error") or after.get("state") == "failed":
        return {"ok": False, "moved": False, "text":
                "The send failed: "
                + (after.get("last_error") or "no reason was recorded")}
    name = after.get("outbox_name")
    if name and os.path.exists(os.path.join(config.OUTBOX_DIR, name)):
        again = (" This one was confirmed already, so it has gone out a "
                 "second time — byte for byte the same file, which Google "
                 "Photos recognises rather than adds, and Free up space "
                 "clears again on its next run."
                 if row.get("state") == "confirmed" else "")
        return {"ok": True, "moved": True, "text":
                f"Sent. It is in the outbox as {name}." + again}
    if name:
        return {"ok": False, "moved": False, "text":
                f"The ledger reserved the name {name} but no file is in the "
                "outbox, and nothing recorded an error — check the log for "
                "this cycle."}

    # list_outbox, not reconcile: reconcile is what confirms assets from
    # their absence, and running it a second time to read a byte count
    # would be doing the ledger's most consequential work for a number.
    cfg = settings.load()
    _, used = feeder.list_outbox()
    if used + int(row.get("size") or 0) > cfg.outbox_max_bytes:
        return {"ok": False, "moved": False, "text":
                f"The outbox is full — {used / config.GB:.1f} of "
                f"{cfg.outbox_max_gb} GB in use, and this file needs "
                f"{int(row.get('size') or 0) / 1e6:.0f} MB. It makes room as "
                "Google Photos clears the phone, so try again after a "
                "free-up."}
    return {"ok": False, "moved": False, "text":
            "Nothing was written and nothing recorded an error, which should "
            "not happen — check the log for this cycle."}
