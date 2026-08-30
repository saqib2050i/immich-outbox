import os


def _b(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _i(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


GB = 1024 ** 3

# --- Immich ---
IMMICH_URL = os.getenv("IMMICH_URL", "http://immich-server:2283").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")

# --- The outbox: this folder is what Syncthing mirrors to the Pixel ---
OUTBOX_DIR = os.getenv("OUTBOX_DIR", "/outbox")
# No longer used. Temp files are written inside OUTBOX_DIR and renamed in
# place, because on Unraid /mnt/user is a FUSE overlay: two folders in the
# same share can live on different disks, so a cross-folder rename fails
# with EXDEV. A rename within one directory is always atomic.

# The single most important number here.
#
# The outbox is a mirror of the phone's queue folder, so this cap is what
# stops the Pixel filling up -- keep it well under the ~20 GB usable on a
# 32 GB device. It is also the throughput ceiling: Smart Storage holds each
# file for 30 days after backup, so you move roughly this much per month.
# Raise it to go faster, but leave the phone real headroom.
OUTBOX_MAX_BYTES = _i("OUTBOX_MAX_GB", 10) * GB
MAX_BATCH_FILES = _i("MAX_BATCH_FILES", 40)
FEED_INTERVAL_MIN = _i("FEED_INTERVAL_MIN", 10)

# --- What gets relayed ---
MIN_TAKEN_AT = os.getenv("MIN_TAKEN_AT", "1970-01-01")
INCLUDE_ARCHIVED = _b("INCLUDE_ARCHIVED", "false")
INCLUDE_VIDEO = _b("INCLUDE_VIDEO", "true")
MAX_ASSET_BYTES = _i("MAX_ASSET_MB", 4096) * 1024 * 1024

# --- Scanning Immich ---
SCAN_INTERVAL_MIN = _i("SCAN_INTERVAL_MIN", 15)
FULL_SCAN_INTERVAL_HOURS = _i("FULL_SCAN_INTERVAL_HOURS", 6)
INCREMENTAL_LOOKBACK_DAYS = _i("INCREMENTAL_LOOKBACK_DAYS", 30)
IMMICH_PAGE_SIZE = _i("IMMICH_PAGE_SIZE", 500)

# --- Health ---
# Smart Storage clears a file 30 days after Google Photos confirms the
# backup. Past this, something is wrong: Smart Storage off, backup paused,
# or Photos refusing the file type.
STUCK_AFTER_DAYS = _i("STUCK_AFTER_DAYS", 45)

DB_PATH = os.getenv("DB_PATH", "/data/bridge.db")

# --- Build identity ---
# Stamped into the image by CI so the dashboard can answer "am I running the
# latest?" without anyone having to inspect the container. Running from a
# source checkout leaves these at their defaults.
APP_VERSION = os.getenv("APP_VERSION", "dev")
APP_REVISION = os.getenv("APP_REVISION", "")
APP_BUILT_AT = os.getenv("APP_BUILT_AT", "")

# Syncthing's own bookkeeping, never treated as photos.
IGNORED = (".stfolder", ".stversions", ".stignore", "lost+found")
