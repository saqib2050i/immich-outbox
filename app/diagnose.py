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
import subprocess
import tempfile
from datetime import datetime

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

# The Pixel camera names files in UTC and records the zone separately, so
# `PXL_20230101_025759` with an offset of +05:00 is a photo taken at 07:57
# local -- and the name being five hours off is the file being *right*.
# Older Google Camera builds, Samsung and most everything else wrote the
# local wall clock into the name instead.
UTC_NAMED = ("pxl_",)
LOCAL_NAMED = ("img_", "vid_", "mvimg_", "dsc_")


def filename_time(name: str) -> datetime | None:
    m = NAME_TIME.search(name)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def filename_clock(name: str) -> str:
    """Which clock the camera was reading when it named the file.

    Only ever used to explain a reading, never to decide one. Both
    conventions are checked against the file regardless -- a name that fits
    either is not evidence of anything being wrong, and asserting a
    convention would manufacture faults out of correct files.
    """
    base = os.path.basename(name or "").lower()
    if base.startswith(UTC_NAMED):
        return "utc"
    if base.startswith(LOCAL_NAMED):
        return "local"
    return "unknown"


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


def find(filename: str) -> tuple[dict | None, list[str]]:
    """The asset with that name, or the nearest things to it.

    A name that matches nothing is the most likely thing to happen at this
    box, and it used to be indistinguishable from a file with no problems.
    """
    name = (filename or "").strip()
    if not name:
        return None, []
    c = db.connect()
    row = c.execute("SELECT * FROM assets WHERE filename = ?", (name,)).fetchone()
    if row is None:
        row = c.execute("SELECT * FROM assets WHERE filename = ? COLLATE NOCASE",
                        (name,)).fetchone()
    if row is not None:
        return dict(row), []

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
    return None, [r["filename"] for r in near]


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


def _offset_hours(value) -> float | None:
    """"+05:00" as 5.0. The zone the file says its own clock was in."""
    if not isinstance(value, str):
        return None
    m = re.match(r"([+-])(\d{2}):?(\d{2})", value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) + int(m.group(3)) / 60)


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


def verdict(exif: dict, kind: str | None = None) -> dict:
    """Will Google Photos date this file, or file it under the upload?

    The only question that matters about a file on its way to the phone,
    and it is decided by one tag: DateTimeOriginal for a still, QuickTime's
    CreateDate for a video. Everything else in the file is a note.

    A confirmed case sets the bar: PXL_20240105_034733992.jpg carries
    DateTimeOriginal present and empty, Immich shows it correctly from a
    Takeout sidecar, and Google Photos filed it under the day it was
    uploaded -- with a perfectly parseable date sitting in the filename,
    which Google did not use. So a name is never counted as a date here.
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
    extra = ""
    if others:
        extra = (" The file does carry a date elsewhere — "
                 + ", ".join(f"{o['tag']} {o['value'].strip()}" for o in others)
                 + " — but that is not the tag Google Photos reads.")
    return {"dated": False, "level": "bad", "tag": tag, "state": state,
            "value": None if raw is None else str(raw), "others": others,
            "headline": "Would fall back to upload time",
            "reason": why + extra + " Google Photos will file it under the day "
                      "it was uploaded."}


def _agrees_with_immich(exif: dict, says: dict, kind: str | None) -> dict | None:
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
        a, b = _exif_dt(raw), _naive(says.get("local_date_time"))
        mine = a.timestamp() if a else None
        theirs = b.timestamp() if b else None
        label = "Immich's localDateTime, the wall clock where it was taken"
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
    ref = src if src and "error" not in src else dst

    # 1. The question the whole thing is for: will Google Photos read a date
    #    out of this, or file it under the upload? Asked of each copy that
    #    could be read, because the answer is allowed to differ between them
    #    -- that is the entire point of stamping one on its way past.
    for where, exif in (("Immich's original", src), ("the outbox copy", dst)):
        if not exif or "error" in exif:
            continue
        v = verdict(exif, kind)
        # Tagged, because the dashboard draws these as their own cards and
        # a reader should not be told the same thing twice. The line stays
        # in the findings list: that list is the machine-readable answer and
        # the thing that must never come back empty.
        out.append({"level": v["level"], "kind": "verdict",
                    "text": f"{where}: {v['headline']}. {v['reason']}"})

    # 2. And having a date is not the same as having the right one.
    if ref and "error" not in ref:
        agree = _agrees_with_immich(ref, rep.get("says") or {}, kind)
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
            when = str(says["local_date_time"]).replace("T", " ")[:19]
            zone = says.get("time_zone")
            out.append({"level": "warn", "text":
                        f"Immich knows when this was taken — {when}"
                        + (f" in {zone}" if zone else ", with no zone recorded")
                        + " — and the file does not. That date is Immich's own "
                        "record, read from a Google Takeout sidecar at import, "
                        "and it never reached the file. Nothing on the "
                        "Problems tab can see this: the mismatch figures "
                        "compare two fields that were both filled from that "
                        "same sidecar, so they agree, and a library of "
                        "undated files reads as zero."})

    # 4. The camera's own name against the file's own clock.
    want = filename_time(name)
    got = _exif_dt(pick(ref, *(VIDEO_DATE if is_video(ref, kind)
                               else PHOTO_DATE))[0])
    if want and got:
        out.append(_clock_finding(name, want, got, ref))

    # 5. The two copies against each other -- the promise the relay is
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
            why = ("date rewriting is on and this asset is flagged as corrected"
                   if cfg.fix_dates and asset.get("date_mismatch")
                   else "date rewriting is OFF for this asset, so nothing here "
                        "should have altered it")
            out.append({"level": "bad", "text":
                        f"The outbox copy differs from Immich's original "
                        f"({a.get('bytes')} vs {b.get('bytes')} bytes) — {why}."
                        + (f" Tags that differ: {', '.join(changed)}."
                           if changed else "")})

    # 6. Whatever exiftool wanted to complain about.
    for where, exif in (("Immich's copy", src), ("the outbox copy", dst)):
        w = exif.get("Warning")
        for line in (w if isinstance(w, list) else [w] if w else []):
            out.append({"level": "warn",
                        "text": f"exiftool on {where}: {line}"})

    # 7. What the ledger believes, against what the file says.
    if got and asset.get("exif_taken_at"):
        led = db.capture_time(asset["exif_taken_at"])
        off = _offset_hours(pick(ref, "EXIF:OffsetTimeOriginal",
                                 "EXIF:OffsetTime")[0]) or 0
        file_instant = got.timestamp() - off * 3600 - (
            datetime.utcfromtimestamp(0).timestamp())
        if led is not None and abs(led - file_instant) > 120:
            out.append({"level": "warn", "text":
                        "Immich's recorded capture time and the file's own "
                        "disagree. That is the shape of a date corrected in "
                        "Immich, which is what 'Write corrected dates' exists "
                        "to carry into the file."})

    if not out:
        out.append({"level": "warn", "text":
                    "Nothing could be compared — see the three copies above "
                    "for which of them could not be read."})
    return out


async def trace(filename: str, send: bool = False) -> dict:
    """The whole report for one file."""
    rep: dict = {"filename": (filename or "").strip(), "asked_at": db.now()}
    if not rep["filename"]:
        rep["problem"] = "Type a filename first — the name as Immich has it, " \
                         "for example PXL_20230101_025759225.jpg."
        return rep

    row, near = find(rep["filename"])
    if row is None:
        rep["problem"] = (f"No asset in the ledger is called "
                          f"{rep['filename']!r}. Names are matched exactly as "
                          "Immich holds them, so a path or a renamed copy will "
                          "not be found.")
        rep["suggestions"] = near
        if near:
            rep["problem"] += f" {len(near)} near match(es) below."
        return rep

    rep["asset"] = {k: row.get(k) for k in
                    ("id", "filename", "state", "kind", "size", "taken_at",
                     "exif_taken_at", "date_mismatch", "forced", "outbox_name",
                     "attempts", "last_error", "missing_at")}

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
            rep[key]["verdict"] = verdict(exif, kind)
    rep["findings"] = _findings(rep)
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
    if state == "confirmed":
        return ("it is already confirmed — in Google Photos — and this "
                "service never re-sends a confirmed asset, because that "
                "would put a duplicate in the library")
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
    if row.get("outbox_name"):
        return {"ok": True, "moved": False, "text":
                f"Already in the outbox as {row['outbox_name']}, so nothing "
                "was sent — the two copies below are the ones already there."}

    why = _why_not_sendable(row)
    if why:
        return {"ok": False, "moved": False, "text":
                f"Not sent, because {why}."}

    c = db.connect()
    with db._lock:  # noqa: SLF001
        c.execute("UPDATE assets SET forced=1, attempts=0, last_error=NULL, "
                  "state=CASE WHEN state='confirmed' THEN state ELSE 'pending' END "
                  "WHERE id = ?", (row["id"],))
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
        return {"ok": True, "moved": True, "text":
                f"Sent. It is in the outbox as {name}."}
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
