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

The corollary: **absence only counts when the outbox is genuinely there.**
`feeder.outbox_ready()` keeps a dotfile marker (`.immich-outbox-mounted`)
inside the outbox, so it disappears exactly when the outbox does. Without
it, a bind mount that did not come up looks identical to Google Photos
having verified the entire queue, and confirmed assets are never re-sent.
Nothing may delete that marker — `empty_outbox()` deliberately skips it —
and `reconcile()` must keep returning early while it is missing.

**2. The outbox cap is the only flow control.** The outbox mirrors the
phone's queue folder, so capping the outbox caps the phone. Never add a code
path that writes past the cap.

**2a. The file is passed through byte for byte — with one exception.**
When a date has been corrected in Immich, that correction lives in Immich's
database and `/original` still serves the untouched file, so Google Photos
would use the stale embedded date. `rewrite_capture_date()` writes the
corrected date in, and only then. It runs on the temp file after the size
check, so integrity is verified against Immich before anything is altered
and Syncthing never sees a partial edit. A file whose own date agrees with
Immich, or which carries no date at all, is never touched. Turned off with
the `fix_dates` setting.

**2b. The companion presses a button; it never deletes.** `companion.py`
and the phone app exist to make files leave the outbox *sooner*, by tapping
Google Photos' own "Free up space" instead of waiting ~30 days for Smart
Storage. Google Photos still decides what has been backed up and is safe to
remove, so invariant 1 holds unchanged. The phone app declares no storage
permission at all, which makes that Android's guarantee rather than a
promise. Nothing the phone reports is ever treated as confirmation —
`companion.record()` writes a log line and touches no asset row.

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
app/companion.py  the Pixel companion: pairing, poll, run bookkeeping
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

`pytest` (91 tests, a few seconds). Every test gets its own `DB_PATH` and
`OUTBOX_DIR` from the `rig` fixture; nothing touches a real Immich or a real
outbox. CI runs it and will not publish an image if it fails.

```
pip install -r requirements-dev.txt && python -m pytest -q
```

The local dev Python may be older than 3.10, which the code needs. If so:

```
docker run --rm -v "$PWD":/srv -w /srv python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

What is covered, by file:

- `test_relay.py` — fill → drain → refill, asserting nothing is re-sent and
  the cap holds; oversize-only-when-empty; truncated and failed downloads;
  filename collisions; legacy `<id>__` names still confirming
- `test_outbox_guard.py` — an outbox that is not really there confirms
  nothing and receives nothing
- `test_motion.py` — components skipped in both scan orders
- `test_eligibility.py` — date windows, forced sends, retry ceiling
- `test_backup.py` — backup → change → restore round-trip, credential
  stripping, name traversal
- `test_auth.py` — unauthenticated 401s, forced change, host allowlist,
  login throttle
- `test_cycle.py` — the cycle lock, and the housekeeping chores
- `test_immich.py` — version-aware request shaping and error parsing
- `test_companion.py` — that the companion confirms nothing and deletes
  nothing, token gating, when it asks by itself, one run at a time
- `test_version.py` — VERSION is the single origin of the number

Writing a new test: `conftest.asset()` builds a ledger row and
`conftest.fake_download()` stands in for `immich.stream_original`, returning
a body of exactly the size the ledger recorded (`top_up` checks the two
against each other). `rig.deliver(n)` simulates Smart Storage clearing files
off the phone.

## Versioning

`VERSION` at the repo root is the only place the number is edited. CI reads
it, stamps it into the image, and refuses to build if it is not `x.y.z` or
if a release tag disagrees with it. `config.py` falls back to reading the
same file, so a source checkout and the image built from it report the same
number — which means the version no longer distinguishes them, and the
dashboard uses the absence of a CI build timestamp for that instead.

Bump it in the pull request that makes the change, and add the entry to
`CHANGELOG.md` in the same commit: `x` for an overhaul, `y` for a feature,
`z` for a fix. A test asserts the changelog documents the current version.

## Deployment

Push to `main` → Actions builds and publishes
`ghcr.io/saqib2050i/immich-outbox:latest` for amd64 and arm64. On the server:
`docker compose pull && docker compose up -d --force-recreate`.

The GHCR package must be public, or the server needs `docker login ghcr.io`.

## The Pixel companion

The phone polls; the server never dials out to it. A phone on DHCP has no
stable address, an inbound listener is a permanently open port on the LAN,
and Doze is far kinder to a scheduled outbound request than to a held-open
socket. The cost is that "free up now" takes effect on the phone's next
check-in, so the server sets the interval and asks for a fast one only when
there is something worth waking up for.

Two endpoints are the phone's (`/api/companion/poll`, `/api/companion/report`),
gated in the middleware by a pairing token rather than a session — the phone
has no browser. Two are the dashboard's, behind the normal session gate.

The button labels the app looks for are a *setting*, not a constant. Google
renames them, and a rename should be a text field in the dashboard, not a
new APK.

CI builds the APK and bundles it into the image at `dist/companion.apk`, so
the phone updates itself from `/app` rather than from a cable. Two things
follow. The app's version comes from the same root `VERSION` file as the
server, deliberately: they speak a protocol, and a pair that disagrees is a
pair nobody has tested. And Android installs an update only over the same
signing key, so CI needs a stable one from `ANDROID_KEYSTORE_BASE64`; absent
it the build is debug-signed and the install page says so, because failing
the release of a *server* over a phone app would be the wrong trade.

Two things about the app that cost a release each. Its reported version
must come from the package manager, never a constant — a `const val VERSION`
sat beside a `versionName` read from the file and the two drifted, so a
freshly installed build announced the old number and was offered an update
it already had, forever. And a screen that is off draws no windows, so an
accessibility service on a shelf phone reads *nothing*; the service wakes
the screen, and when it still sees nothing it distinguishes screen-off,
locked, and Photos-never-came-forward, which need three different fixes and
used to look identical.

The screen matching lives in `companion/.../Labels.kt`, free of Android
imports so it unit-tests on a laptop against strings read off the real
phone. It matches text in somebody else's app, so it is the part most
likely to break without warning, and it is the part that did.

The app itself is in `companion/` — four Kotlin files, no dependencies, and
no storage permission, which is what makes "it cannot delete a photo" an
Android guarantee rather than a claim. `minSdk 29`, because Android 10 was
the Pixel 1's last update. Lint runs with `NewApi` promoted to an error so
an API above that floor fails the build rather than crashing on the one
device that matters.

## Open items

- Google Photos cannot be read: the Library API was restricted to
  app-created media in March 2025. A Takeout importer is the only way to
  reconcile against the real cloud library — discussed, not built.
- Sessions are in memory; a restart signs everyone out.
- `settings.load()` is one SELECT per key and is called on every Immich
  client construction. Harmless at this scale, but it is a single query.
