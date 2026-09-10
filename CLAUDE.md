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
`skipped`. `forced` bypasses the cut-off and jumps the queue.

**One automatic rule.** Everything from `ongoing_from` onwards goes by
itself; everything older waits to be asked for, which is what `forced`
means and what Library's month and file controls set. There used to be a
second window — a start/end range stepped forward by hand — and it was
removed because Library already lists every month with what is left in it,
so the window was a second place for the same decision to live, kept in
step by hand.

The consequence worth knowing when reading old code or ledgers: `pending`
does not mean "queued". Most of the ledger is `pending` by design and going
nowhere. Anything showing a figure has to say which kind it is — the
timeline splits `sending` from `resting` for exactly this reason, and
presenting the whole ledger as a backlog is a mistake this dashboard has
made more than once.

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

## The dashboard's stylesheet

One inline `<style>` in `dashboard.html`, no build step, and `login.html`
keeps its own copy of the tokens because it has to render before there is
anywhere to share one from. Three things in it are load-bearing:

- **`--transit`, `--done`, `--alarm`, `--dim` and `--rule` are referenced
  from inline styles in the script.** Renaming one silently unstyles
  whatever it drew, with no error anywhere.
- **`[data-tab]:not(.tab-on)` hides an inactive tab's panels.** The `:not()`
  is not decoration. A bare `[data-tab]` is one attribute — specificity
  (0,1,0) — so any single class that sets `display` ties it and wins on
  source order. `.grid{display:grid}` did that and put the Overview cards on
  every tab; `.setwrap{display:grid}` did it earlier and put the entire
  settings form there. Two tests guard this.
- **`summary::before` is a grid item.** Every disclosure row here is a grid
  whose first column is the caret, so the column count must include it. Both
  timeline rows were a column short, which pushed the last child onto a
  second row and into column one — and an `auto` column sizes to its widest
  item, so a figure sitting underneath shoved the year label into the middle
  of the row.

Sans for prose, mono for values: sizes, counts, times, filenames, paths.
The page was mono throughout and read like a log file. And no glyph outside
ASCII is load-bearing — the carets were U+25B8, which the old mono stack had
and a system sans stack does not, so they rendered as full stops until they
were drawn with borders instead.

It is used on a phone, so the narrow breakpoints are not an afterthought:
a year's figures are one unbreakable ~400px string, and left inline they
took the whole page's horizontal scrollbar with them.

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

Which makes the idle interval the latency of every command, and it is paid
for out of the phone's battery — so it is split on that axis and no other.
`companion_charging_poll_minutes` (a minute) applies while the phone reports
charging, `companion_idle_poll_minutes` (thirty) while it does not, and the
shelf phone this was built for is never off its cable. A single interval
meant either waking a phone on battery every minute or making a plugged-in
phone wait half an hour, and it did the second. Switched off on the server
is the one state deliberately kept slow: there is nothing to hear, and there
will not be until somebody changes a setting.

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

Scheduling must go through `AlarmManager`, never a `Handler`. A Handler
callback cannot wake a sleeping CPU, so with the screen off — which is the
whole point of a phone on a shelf — the app simply never checks in. It also
must hold a `PARTIAL_WAKE_LOCK` across the check-in, because the alarm wakes
the phone only for the length of the broadcast and the work happens on
another thread after that returns.

**Google Photos resumes where it was left, so a run has to put it back.**
Leaving it on "You freed up 29.80 MB" meant the next run opened onto that
screen and read it as its own result: a success carrying a figure nothing
earned, returned without a button being pressed, and nothing actually
freed — the outbox stayed full behind a dashboard reporting a healthy
phone. `settlePhotos()` backs out to Photos' own screen at the end of every
run, pass or fail, and then brings the companion forward.

That reset is the fix; the guard in `walkPhotos` is what survives it
failing. A finished screen only counts once *this* run has been somewhere —
the freed figure after the button was pressed, "nothing to free up" after
the menu entry was tapped — and one that turns up before that is backed out
of, three times before the run gives up and says so. A visible failure over
a stalled pipeline is the right trade: the report is only ever a log line
(nothing here confirms an asset), but a green tick over an outbox that never
drains is the hardest kind of fault to notice.

Nothing may be tapped by position. The account picture is found by its
description; a positional fallback tapped the memories carousel instead and
opened a slideshow. There is no API to fall back on either — Photos'
`FreeUpSpaceContentProvider` is exported but rejects every caller, adb shell
included, and no deep-link activity reaches the storage screen. Driving the
UI is the only route there is.

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
