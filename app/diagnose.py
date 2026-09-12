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
    "-DateTimeOriginal", "-CreateDate", "-ModifyDate",
    "-OffsetTime", "-OffsetTimeOriginal", "-OffsetTimeDigitized",
    "-SubSecDateTimeOriginal", "-GPSDateTime",
    "-XMP:DateTimeOriginal", "-XMP:CreateDate",
    "-QuickTime:CreateDate", "-QuickTime:ModifyDate",
    "-Make", "-Model", "-Software", "-MIMEType",
    "-ImageWidth", "-ImageHeight", "-FileModifyDate",
    "-a", "-Warning",
]

# What the camera called it. These names carry the local wall-clock time of
# the shutter, which is the only independent witness there is once a file's
# own EXIF is in doubt.
NAME_TIME = re.compile(
    r"(?:^|[^0-9])(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")


def filename_time(name: str) -> datetime | None:
    m = NAME_TIME.search(name)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


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


def _dates(exif: dict) -> dict:
    """Just the tags that decide what date Google Photos gives a file."""
    keep = ("DateTimeOriginal", "CreateDate", "ModifyDate", "OffsetTimeOriginal",
            "OffsetTime", "SubSecDateTimeOriginal", "FileModifyDate",
            "XMP:DateTimeOriginal", "QuickTime:CreateDate")
    return {k: exif[k] for k in keep if k in exif and exif[k] not in (None, "")}


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

    for where, block in (("Immich", rep.get("immich") or {}),
                         ("the outbox", rep.get("outbox") or {})):
        err = (block.get("exif") or {}).get("error") or block.get("error")
        if err:
            out.append({"level": "bad", "text": f"Could not read {where}: {err}"})

    # 1. The camera's own name against the file's own clock. The name is the
    #    only independent witness once the EXIF is in doubt.
    want = filename_time(name)
    ref = src if src and "error" not in src else dst
    got = _exif_dt(ref.get("DateTimeOriginal"))
    if want and got:
        drift = (got - want).total_seconds() / 3600
        off = _offset_hours(ref.get("OffsetTimeOriginal") or ref.get("OffsetTime"))
        if abs(drift) < 1 / 60:
            out.append({"level": "ok", "text":
                        f"The name and the file agree: {want:%Y-%m-%d %H:%M:%S}."})
        elif off is not None and abs(drift - off) < 1 / 60:
            out.append({"level": "bad", "text":
                        f"DateTimeOriginal is {drift:+g}h from the time in the "
                        f"name, which is exactly this file's own "
                        f"{ref.get('OffsetTimeOriginal') or ref.get('OffsetTime')} "
                        "offset. A UTC time has been written into "
                        "DateTimeOriginal, which EXIF defines as local time. "
                        "Google Photos will date this file that far out."})
        else:
            out.append({"level": "bad", "text":
                        f"DateTimeOriginal is {drift:+g}h from the "
                        f"{want:%H:%M:%S} in the file's name."})
    elif want and not got:
        out.append({"level": "warn", "text":
                    "The file carries no DateTimeOriginal, so Google Photos "
                    "will date it by its modification time instead."})

    # 2. The two copies against each other -- the question the relay is
    #    actually on the hook for.
    a, b = rep.get("immich") or {}, rep.get("outbox") or {}
    if a.get("ok") and b.get("present"):
        if a.get("sha256") == b.get("sha256"):
            out.append({"level": "ok", "text":
                        "The outbox copy is byte-for-byte identical to "
                        "Immich's original."})
        else:
            changed = sorted({k for k in set(_dates(src)) | set(_dates(dst))
                              if src.get(k) != dst.get(k)})
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

    # 3. Whatever exiftool wanted to complain about.
    for where, exif in (("Immich's copy", src), ("the outbox copy", dst)):
        w = exif.get("Warning")
        for line in (w if isinstance(w, list) else [w] if w else []):
            out.append({"level": "warn",
                        "text": f"exiftool on {where}: {line}"})

    # 4. What the ledger believes, against what the file says.
    if got and asset.get("exif_taken_at"):
        led = db.capture_time(asset["exif_taken_at"])
        off = _offset_hours(ref.get("OffsetTimeOriginal") or ref.get("OffsetTime")) or 0
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

    if send and not row.get("outbox_name"):
        rep["sent"] = await _send_now(row["id"])
        row = dict(db.connect().execute(
            "SELECT * FROM assets WHERE id = ?", (row["id"],)).fetchone())
        rep["asset"]["outbox_name"] = row.get("outbox_name")
        rep["asset"]["state"] = row.get("state")

    rep["immich"] = await _immich_copy(row["id"], int(row.get("size") or 0))
    rep["outbox"] = _outbox_copy(row.get("outbox_name"))
    rep["phone"] = await _phone_copy(row.get("outbox_name"))
    for key in ("immich", "outbox"):
        exif = rep[key].get("exif")
        # Only when there is something to have read. An empty `dates` on a
        # copy that could not be opened reads as "no dates in the file",
        # which is a different and much calmer statement than the truth.
        if isinstance(exif, dict) and "error" not in exif:
            rep[key]["dates"] = _dates(exif)
    rep["findings"] = _findings(rep)
    return rep


async def _send_now(asset_id: str) -> dict:
    """Push this one asset through, so there is a second copy to compare."""
    c = db.connect()
    with db._lock:  # noqa: SLF001
        c.execute("UPDATE assets SET forced=1, attempts=0, last_error=NULL, "
                  "state=CASE WHEN state='confirmed' THEN state ELSE 'pending' END "
                  "WHERE id = ?", (asset_id,))
        c.commit()
        db._bump()  # noqa: SLF001
    try:
        async with feeder.CYCLE_LOCK:
            _, used = feeder.reconcile()
            await feeder.top_up(used)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
