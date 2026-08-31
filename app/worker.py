"""Background scanner.

Two passes, because neither alone is safe:

  incremental  every SCAN_INTERVAL_MIN, looks back INCREMENTAL_LOOKBACK_DAYS.
               Catches new photos fast.
  full         every FULL_SCAN_INTERVAL_HOURS, walks the whole library.
               Catches old imports (scans, WhatsApp dumps, camera imports)
               whose capture date is far in the past and which an
               incremental window would miss forever.

Both use INSERT OR IGNORE, so an asset is only ever queued once.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from . import config, db, immich


async def _scan(taken_after: str | None, label: str) -> None:
    """One pass over Immich.

    A full pass also refreshes what it already knows and reconciles against
    what Immich still holds. An incremental pass only looks at a recent
    window, so it can neither refresh the rest nor conclude anything about
    what is absent from it.
    """
    full = label == "full"
    added = 0
    seen: set[str] = set()
    try:
        async for page in immich.list_assets(taken_after=taken_after):
            added += db.upsert_assets(page, refresh=full)
            if full:
                seen.update(row["id"] for row in page)
    except Exception as exc:  # noqa: BLE001
        # Deliberately no reconciliation on a failed scan: a half-finished
        # pass would look exactly like a library that had lost everything
        # it had not reached yet.
        db.log("error", f"{label} scan failed: {exc}")
        return

    if full and seen:
        gone = db.mark_missing(seen)
        if gone:
            db.log("scan", f"{gone} asset(s) are no longer in Immich — they are "
                           "kept in the ledger but no longer counted or sent")

    db.set_meta(f"last_{label}_scan", db.now())
    if added:
        db.log("scan", f"{label} scan queued {added} new asset(s)")


async def full_scan() -> None:
    """Scan the whole library now, rather than waiting for the next cycle.

    The request flag is cleared before the scan starts, not after: a rescan
    asked for while this one is running is a request for a *further* scan,
    and clearing afterwards would swallow it.
    """
    db.set_meta("force_full_scan", "0")
    # From the beginning: the ledger holds the whole library and the date
    # windows decide what is released from it.
    await _scan("1970-01-01T00:00:00.000Z", "full")


async def run() -> None:
    from . import settings

    last_full = 0.0
    complained = False
    while True:
        loop_now = asyncio.get_running_loop().time()
        if not settings.load().immich_api_key:
            # Once, not every minute: an unconfigured install would otherwise
            # push the rest of the event log out of the 500-line window.
            if not complained:
                db.log("error", "No Immich API key set — add one in Settings")
                complained = True
            await asyncio.sleep(60)
            continue
        complained = False
        try:
            forced = db.get_meta("force_full_scan") == "1"
            if forced or loop_now - last_full > config.FULL_SCAN_INTERVAL_HOURS * 3600:
                await full_scan()
                last_full = loop_now
            else:
                since = datetime.now(timezone.utc) - timedelta(
                    days=config.INCREMENTAL_LOOKBACK_DAYS
                )
                await _scan(since.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "incremental")
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"scanner loop error: {exc}")

        # Wake often enough that a "Rescan now" from the dashboard is acted
        # on promptly instead of waiting out the whole interval.
        for _ in range(max(1, config.SCAN_INTERVAL_MIN * 4)):
            if db.get_meta("force_full_scan") == "1":
                break
            await asyncio.sleep(15)
