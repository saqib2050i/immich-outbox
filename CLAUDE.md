# CLAUDE.md

Context for working on this repo. Read before changing anything.

## What this is

A relay that gets Immich originals into Google Photos at full quality by
routing them through a Pixel 1, which still has unlimited original-quality
backup. The service is only the ledger and the outbox feeder; Syncthing
moves the bytes, Google Photos uploads, Smart Storage clears the phone.

Stack: FastAPI + SQLite + vanilla JS, no frontend build. Deployed as a GHCR
image built by GitHub Actions, run on Unraid.

## Invariants — do not break these

**1. The service never deletes from the outbox.** Confirmation is derived
from files disappearing, and only Smart Storage or Google Photos' Free up
space removes them, and only after verifying the backup. If this service
deletes a file, it silently marks it as backed up when it is not.

The single exception is `purge_motion_parts()`, which is safe because those
assets are `skipped`, never `queued`, so their absence is never read as
confirmation.

**2. The outbox cap is the only flow control.** The outbox mirrors the
phone's queue folder, so capping the outbox caps the phone. Never add a code
path that writes past the cap.

**3. Immich is read-only.** Three permissions: `asset.read`,
`asset.download`, `server.about`. Never add a write scope.

**4. Already-confirmed assets are never re-sent.** They are in Google
Photos; re-sending creates a duplicate.

## Architecture

```
app/config.py     env defaults only; live settings live in the DB
app/settings.py   settings in SQLite, SPEC drives everything, live reload
app/db.py         the ledger; every write bumps a revision for SSE
app/immich.py     the ONLY file with Immich API surface
app/feeder.py     reconcile -> confirm -> top up; housekeeping
app/worker.py     two scan cadences: 30-day window, whole library
app/alerts.py     silent-failure detection + webhook
app/backup.py     ledger snapshots; downloads are credential-stripped
app/auth.py       PBKDF2 passwords, in-memory sessions, host allowlist
app/syncthing.py  optional read-only status
app/static/       dashboard.html, login.html — no build step
```

Asset states: `pending` → `queued` → `confirmed`, plus `failed` and
`skipped`. `forced` bypasses the date windows and jumps the queue.

## Hard-won details

- **Immich v3** changed search semantics: omitting `visibility` used to mean
  timeline-only, now means *any*. A v1-era client silently relays archived
  and hidden photos. `immich.py` detects the version and adapts.
- **v3 can redirect** `/assets/{id}/original`. httpx does not follow
  redirects by default and `raise_for_status()` ignores 3xx, so without
  `follow_redirects=True` a redirect stub gets written as a photo.
- **Unraid `/mnt/user` is a FUSE overlay.** Two folders in the same share can
  sit on different disks, so `os.replace` between them fails with EXDEV.
  Temp files must be written *inside* the outbox and renamed in place.
- **Ownership must be 99:100** (`nobody:users`). Root-owned files cannot be
  deleted by Syncthing, so phone deletions never propagate.
- **Motion photos are two Immich assets.** The still carries the embedded
  clip; the extracted component is identified by `livePhotoVideoId` and must
  never be relayed alone.
- **Filenames are recorded in the ledger** (`outbox_name`), not encoded into
  the name. An older scheme used an `<asset-id>__` prefix, which followed
  files into Google Photos permanently. Legacy names must keep resolving.
- **SQLite's backup API silently no-ops** when copying into a live
  connection. Restore closes the connection and swaps the file, removing
  `-wal` and `-shm` too or they replay over the restored data.
- **Backup filenames carry milliseconds.** Second resolution meant the
  safety copy taken during a restore overwrote the backup being restored
  from — restore appeared to work and changed nothing.
- **Do not `GROUP BY` a column alias** that also names a column in a joined
  table. It binds to the table, and a NULL join column collapses everything
  into one group.

## Bugs that keep recurring

Partial string edits have twice left **duplicate route definitions** where
FastAPI matched the first (dead) one, and once left a function referencing an
undefined name. After editing, check for duplicate `@app.` routes and run the
test snippets below.

## Testing

There is no test suite; verification has been ad-hoc scripts against
`TestClient` with a temp `DB_PATH` and `OUTBOX_DIR`. **A real test suite is
the most valuable next contribution.** Worth covering first:

- fill → drain → refill, asserting nothing is re-sent and the cap holds
- motion components skipped in both scan orders
- backup → change → restore round-trip
- auth: unauthenticated 401s, forced change, host allowlist
- date-window and forced-send eligibility

## Deployment

Push to `main` → Actions builds and publishes
`ghcr.io/saqib2050i/immich-outbox:latest` for amd64 and arm64. On the server:
`docker compose pull && docker compose up -d --force-recreate`.

The GHCR package must be public, or the server needs `docker login ghcr.io`.

## Open items

- Google Photos cannot be read: the Library API was restricted to
  app-created media in March 2025. A Takeout importer is the only way to
  reconcile against the real cloud library — discussed, not built.
- No test suite (see above).
- Sessions are in memory; a restart signs everyone out.
