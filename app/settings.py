"""Live settings.

Environment variables are only the initial defaults. Once saved from the
dashboard, values live in the database and take effect on the next cycle --
no container restart.

One rule decides what goes automatically:

    ongoing    everything taken on or after a cut-off date, forever.
               New photos flow through the Pixel; history is left alone
               until it is asked for.

Anything older is sent by asking for it in Library, a month or a file at a
time. There was a second window -- a start/end range stepped forward by
hand -- which did the same job less well: Library already lists every month
with what is left in it, and a window you have to remember to advance is a
second place for the same decision to live.

Eligibility is applied when an asset is released to the outbox, never when
it is scanned. The ledger always holds the whole library, so moving the
cut-off releases assets immediately instead of needing a rescan.
"""

from dataclasses import dataclass, asdict

from . import config, db

# key -> (type, default)
SPEC: dict[str, tuple[type, object]] = {
    "immich_url": (str, config.IMMICH_URL),
    "immich_api_key": (str, config.IMMICH_API_KEY),
    "paused": (bool, False),
    "outbox_max_gb": (int, config.OUTBOX_MAX_BYTES // config.GB),
    "max_batch_files": (int, config.MAX_BATCH_FILES),
    # How many files move at once, counted separately so a 4 GB video
    # cannot hold up a queue of photos behind it.
    "photo_workers": (int, 3),
    "video_workers": (int, 1),
    "include_video": (bool, config.INCLUDE_VIDEO),
    # Write a date corrected in Immich into the file itself. The only case
    # where this service alters an original, and only when the file's own
    # date disagrees with Immich's.
    "fix_dates": (bool, False),
    "max_asset_mb": (int, config.MAX_ASSET_BYTES // (1024 * 1024)),
    "ongoing_enabled": (bool, True),
    "ongoing_from": (str, config.MIN_TAKEN_AT),
    # Alerting
    "alert_webhook_url": (str, ""),
    "alert_format": (str, "json"),          # "json" (Gotify, Apprise) or "ntfy"
    "alert_stall_days": (int, 40),          # Smart Storage clears at 30
    "alert_immich_hours": (int, 6),
    "alert_failed_count": (int, 25),
    # Syncthing (optional, read-only status)
    "syncthing_url": (str, ""),
    "syncthing_api_key": (str, ""),
    "syncthing_folder": (str, ""),
    # The Pixel companion. It presses Google Photos' "Free up space" so the
    # outbox drains in minutes rather than on Smart Storage's 30-day clock.
    # It never deletes anything itself -- see companion.py.
    "companion_enabled": (bool, False),
    # Ask by itself whenever the outbox is holding anything the phone could
    # let go of. Not only when work is queued behind it: the files already
    # in the outbox are not backed up until they disappear, and a free-up is
    # the only thing that makes them disappear.
    "companion_auto": (bool, True),
    "companion_min_battery": (int, 30),
    # How long a manual "free up now" can sit before the phone hears about
    # it. The phone dials out and the server never dials in, so this is the
    # latency, and it is bought with the phone's battery. A phone on its
    # charger -- which is the shelf phone this was built for -- can afford
    # to ask every minute; the same phone unplugged cannot, so it keeps the
    # slower one. Charging is the axis because charging is the cost.
    "companion_charging_poll_minutes": (int, 1),
    "companion_idle_poll_minutes": (int, 30),
    # A floor between automatic runs, not a schedule. One free-up unblocks
    # roughly one outbox's worth, and asking again before Google Photos has
    # uploaded the replacements just wakes the phone for nothing.
    "companion_cooldown_minutes": (int, 60),
    # Minutes, not hours. It was twelve hours, set when the phone checked in
    # twice an hour. A phone on a charger now checks in every minute, so
    # twelve hours is 720 missed check-ins before anyone is told.
    "companion_offline_minutes": (int, 60),
    # Stay in Google Photos after a run so its upload can get going. Off by
    # default, and decided here rather than on the phone: it costs screen
    # time, and the server is where you can switch it off without walking to
    # the shelf. Google Photos asks for this itself, in as many words --
    # "Keep the app open for faster backup".
    "companion_dwell_enabled": (bool, False),
    "companion_dwell_seconds": (int, 120),
    # How often to open Google Photos purely to see what it is doing, when
    # there is no free-up to ride along with. The dwell used to have no
    # schedule of its own -- it happened only inside a free-up run, so it
    # never happened at all when the outbox was empty, which is exactly when
    # Photos most needs waking. 0 turns the watching off.
    "companion_watch_minutes": (int, 30),
    # Freeing space while Google Photos is still uploading clears whatever
    # it has finished and leaves the rest, which is fine but wakes the phone
    # for little. Waiting until it says it is done makes each run count --
    # and makes what is left afterwards mean something.
    "companion_wait_for_backup": (bool, True),
    # Empty means "use the built-in list". Editable because Google renames
    # these buttons, and a rename should not need a new APK.
    "companion_labels": (str, ""),
    "companion_confirm_labels": (str, ""),
    # What marks Google Photos' backup panel, so the phone can read how far
    # along it is while it is in there. Same reasoning as the two above.
    "companion_backup_labels": (str, ""),
    # Housekeeping
    "backup_enabled": (bool, True),
}


@dataclass
class Settings:
    immich_url: str
    immich_api_key: str
    paused: bool
    outbox_max_gb: int
    max_batch_files: int
    photo_workers: int
    video_workers: int
    include_video: bool
    fix_dates: bool
    max_asset_mb: int
    ongoing_enabled: bool
    ongoing_from: str
    alert_webhook_url: str
    alert_format: str
    alert_stall_days: int
    alert_immich_hours: int
    alert_failed_count: int
    syncthing_url: str
    syncthing_api_key: str
    syncthing_folder: str
    companion_enabled: bool
    companion_auto: bool
    companion_min_battery: int
    companion_charging_poll_minutes: int
    companion_idle_poll_minutes: int
    companion_cooldown_minutes: int
    companion_offline_minutes: int
    companion_dwell_enabled: bool
    companion_dwell_seconds: int
    companion_watch_minutes: int
    companion_wait_for_backup: bool
    companion_labels: str
    companion_confirm_labels: str
    companion_backup_labels: str
    backup_enabled: bool

    @property
    def lanes(self) -> dict:
        """Concurrent downloads per kind, floored at one so a lane set to
        zero cannot silently stop that kind moving at all."""
        return {"IMAGE": max(1, self.photo_workers),
                "VIDEO": max(1, self.video_workers)}

    @property
    def eligibility(self) -> dict:
        """What may go out right now, in the shape the ledger's queries
        take. Three callers were building this dict independently; a window
        added to one and not the others would silently disagree about what
        is waiting."""
        return {
            "include_video": self.include_video,
            "max_asset_bytes": self.max_asset_bytes,
            "ongoing": self.ongoing_enabled,
            "ongoing_from": self.ongoing_from,
            "fix_dates": self.fix_dates,
        }

    @property
    def outbox_max_bytes(self) -> int:
        return self.outbox_max_gb * config.GB

    @property
    def max_asset_bytes(self) -> int:
        return self.max_asset_mb * 1024 * 1024

    def as_dict(self) -> dict:
        d = asdict(self)
        # Secrets are reported as present, never echoed back.
        d["immich_api_key"] = "set" if self.immich_api_key else ""
        d["syncthing_api_key"] = "set" if self.syncthing_api_key else ""
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
        if key in ("immich_api_key", "syncthing_api_key") and not str(value).strip():
            continue
        if kind is bool:
            value = "true" if value in (True, "true", "on", 1, "1") else "false"
        db.set_meta(f"cfg_{key}", str(value))
    db.log("settings", "settings updated")
    return load()

