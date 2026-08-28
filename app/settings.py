"""Live settings.

Environment variables are only the initial defaults. Once saved from the
dashboard, values live in the database and take effect on the next cycle --
no container restart.

Two independent windows decide what is eligible to send, and they can both
be on at once:

    ongoing    everything taken on or after a cut-off date, forever.
               This is the "draw a line and move on" mode: new photos flow
               through the Pixel, history is left alone.

    backfill   a start/end date window you advance by hand, one month at a
               time, as you delete the old Storage Saver copies from Google
               Photos. Keeps you from creating duplicates faster than you
               can clear them.

Eligibility is applied when an asset is released to the outbox, never when
it is scanned. The ledger always holds the whole library, so widening a
window releases assets immediately instead of needing a rescan.
"""

from dataclasses import dataclass, asdict
from datetime import date

from . import config, db

# key -> (type, default)
SPEC: dict[str, tuple[type, object]] = {
    "immich_url": (str, config.IMMICH_URL),
    "immich_api_key": (str, config.IMMICH_API_KEY),
    "paused": (bool, False),
    "outbox_max_gb": (int, config.OUTBOX_MAX_BYTES // config.GB),
    "max_batch_files": (int, config.MAX_BATCH_FILES),
    "include_video": (bool, config.INCLUDE_VIDEO),
    "max_asset_mb": (int, config.MAX_ASSET_BYTES // (1024 * 1024)),
    "ongoing_enabled": (bool, True),
    "ongoing_from": (str, config.MIN_TAKEN_AT),
    "backfill_enabled": (bool, False),
    "backfill_start": (str, "2015-01-01"),
    "backfill_end": (str, "2015-01-31"),
}


@dataclass
class Settings:
    immich_url: str
    immich_api_key: str
    paused: bool
    outbox_max_gb: int
    max_batch_files: int
    include_video: bool
    max_asset_mb: int
    ongoing_enabled: bool
    ongoing_from: str
    backfill_enabled: bool
    backfill_start: str
    backfill_end: str

    @property
    def outbox_max_bytes(self) -> int:
        return self.outbox_max_gb * config.GB

    @property
    def max_asset_bytes(self) -> int:
        return self.max_asset_mb * 1024 * 1024

    def as_dict(self) -> dict:
        d = asdict(self)
        d["immich_api_key"] = "set" if self.immich_api_key else ""
        return d


def _cast(kind: type, raw: str):
    if kind is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return str(raw)


def load() -> Settings:
    values = {}
    for key, (kind, default) in SPEC.items():
        stored = db.get_meta(f"cfg_{key}")
        values[key] = _cast(kind, stored) if stored is not None else default
    return Settings(**values)


def save(updates: dict) -> Settings:
    """Persist only known keys. A blank API key means 'leave it alone', so
    the dashboard never has to echo the secret back to save anything else."""
    for key, (kind, _) in SPEC.items():
        if key not in updates:
            continue
        value = updates[key]
        if key == "immich_api_key" and not str(value).strip():
            continue
        if kind is bool:
            value = "true" if value in (True, "true", "on", 1, "1") else "false"
        db.set_meta(f"cfg_{key}", str(value))
    db.log("settings", "settings updated")
    return load()


def _month_bounds(anchor: date) -> tuple[date, date]:
    start = anchor.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, date.fromordinal(end.toordinal() - 1)


def advance_window(direction: int = 1) -> Settings:
    """Move the backfill window one calendar month. The workflow is: clear
    that month from Google Photos, let the originals flow, confirm the
    window is done, then step forward."""
    s = load()
    try:
        cur = date.fromisoformat(s.backfill_start)
    except ValueError:
        cur = date.today().replace(day=1)

    month = cur.month + direction
    year = cur.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    start, end = _month_bounds(date(year, month, 1))

    return save({
        "backfill_start": start.isoformat(),
        "backfill_end": end.isoformat(),
    })
