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

**4. Nothing automatic re-sends a confirmed asset.** They are in Google
Photos, and `claim_batch` excludes `state='confirmed'` outright, so no
cycle, window, sweep or setting can put one back in the queue.

The reason behind the rule is narrower than the rule, and the one exception
turns on it. Re-sending duplicates a photo *when the bytes have changed*:
Google Photos matches an upload against what it already holds, so a file
that is byte for byte identical is recognised rather than added, and Free
up space clears it again on its next run. So `diagnose._send_now()` — one
button, in Tools, aimed at one named file — will send a confirmed asset
again, and refuses only when the file would be altered on the way out
(`fix_dates` and a real date mismatch), which is the case where it really
would arrive as a second photo.

That exception exists because a wrong date is noticed *in Google Photos*,
months after the fact, by which time the outbox copy is long gone — and
without it there is no way to see what actually left the building for
precisely the files worth asking about. Note the trap in the other
direction: once anything starts stamping files on their way out, a re-sent
file is no longer identical, and this exception has to be re-examined
rather than inherited.

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

**A redraw must never throw away what the reader was doing.** Every ledger
write pushes an SSE event, so the dashboard redraws several times a second
while anything is moving. Rebuilding a list or a tree on each of those
destroys open `<details>`, scroll position, and — because restoring `open`
fires `toggle` — refetches whatever the toggle loads. Library was unusable
for the whole duration of a send because of exactly this. So the timeline
keeps two signatures: the *shape* (which years and months exist, changing
when a scan finds something) rebuilds, and the *figures* (changing per file)
are written into the existing nodes by `paintFigures`. Anything that redraws
on a revision bump needs the same split, or at least a signature guard that
leaves the DOM alone when the payload is identical.

Sans for prose, mono for values: sizes, counts, times, filenames, paths.
The page was mono throughout and read like a log file. And no glyph outside
ASCII is load-bearing — the carets were U+25B8, which the old mono stack had
and a system sans stack does not, so they rendered as full stops until they
were drawn with borders instead.

It is used on a phone, so the narrow breakpoints are not an afterthought:
a year's figures are one unbreakable ~400px string, and left inline they
took the whole page's horizontal scrollbar with them.

## Tracing one file

`app/diagnose.py`, reachable from Tools. It fetches Immich's original to a
temp file, reads the outbox copy, and puts the date tags side by side.
Reach for it before believing anything about where a file's metadata went
wrong — "it arrived damaged" and "this service damaged it" look identical
from every other screen here, and telling them apart by hand costs a USB
cable and an afternoon.

The phone's copy is confirmed rather than read, and must stay that way: the
companion holds no storage permission, which is what makes "it cannot
delete a photo" an Android guarantee. Syncthing hashes every block it
moves, so a device it lists as holding a file has a byte-identical copy.

**The send button must account for itself.** `forced` bypasses the date
window and *nothing else*: `claim_batch` still excludes confirmed assets,
motion components, video when video is off, oversized files and anything a
date mismatch holds back, and `top_up` declines when paused, unmounted, or
at the cap. `_why_not_sendable()` checks each by name before the ledger is
touched, because all nine used to return `ok: True` into a field the page
never rendered.

And `outbox_name` is not evidence a file arrived — it is recorded when the
transfer is set up and survives the download failing. Ask the filesystem.
The ledger itself is safe either way (confirmation needs `state='queued'`
**and** `seen_on_phone=1`), but a report that says "Sent" over an empty
outbox is the failure this tool exists to catch, wearing its own uniform.

**Never assume which clock a filename was written by.** The Pixel camera
names files in **UTC** and records the zone separately, so `PXL_20230101_025759`
with an offset of `+05:00` and a `DateTimeOriginal` of `07:57:59` is a
correct file — the name being five hours behind is the file being right.
Older Google Camera builds, Samsung and most everything else wrote the local
wall clock into the name. Reading the first convention as the second turned
an entirely correct library into a five-hour fault and would have fired on
almost every photo in it. So `_clock_finding` tries both readings, asserts
neither, and calls a fault only when neither fits; a file carrying no zone
at all is reported as unverifiable rather than accused.

**A tag can be present and say nothing.** Much of this library carries
`DateTimeOriginal` in the file with its bytes blank — the date lived in a
Google Takeout sidecar, Immich read it into its own database at import, and
the file went to Google Photos carrying an empty field. Immich shows the
right date; Google reads the file, finds none, and dates the photo to the
upload. It does not fall back to the filename: `PXL_20240105_034733992.jpg`
landed on today with a perfectly good date in its own name.

`is_blank` covers the three shapes real files use — a NUL-filled tag
(exiftool renders `""`), a space-filled one, and an mp4 whose
`creation_time` is zero (`0000:00:00 00:00:00`) — and blank is kept
separate from missing throughout, because they have one consequence and two
causes. `_dates()` used to drop anything falsy, which made the fault
invisible in the one tool built to find it.

**A still and a video are judged by different clocks.** `DateTimeOriginal`
is local time with no zone, so it is compared against Immich's
`localDateTime`, the wall clock. QuickTime's `CreateDate` is UTC by
specification, so it is compared against `fileCreatedAt`, the instant.
Swapping them shifts every GMT+5 photo in this library by five hours, which
is the same error the filename check had to be corrected for.

**Immich's three dates are not interchangeable, and one is named to
deceive.** Taken from the live response for the confirmed file:

    fileCreatedAt              2024-01-05T03:47:33.000Z   the UTC instant
    localDateTime              2024-01-05T08:47:33.000Z   the wall clock
    exifInfo.timeZone          Asia/Karachi
    exifInfo.dateTimeOriginal  2024-01-05T03:47:33+00:00  the instant again

`localDateTime` is the wall clock where the shutter fired and the `Z` on it
is an artefact of the transport, not a zone — parse it naively and discard
the suffix, or a library that honours it shifts the photo by the offset.
It is the only one of the four that belongs in `DateTimeOriginal`.

`exifInfo.dateTimeOriginal` is named after the EXIF tag and is **not** it:
the API serves a UTC instant. Writing that value into the tag puts every
Pakistan-era photo five hours early. It is called `exif_original_utc` in
`asset_detail()` so the mistake cannot be made by autocomplete.

`exifInfo.make` and `.model` come back as `""` rather than null on a file
whose EXIF was blanked — the same blank-versus-missing distinction, one
layer up, and `or None` is what handles it.

**A zone Immich reports is not always a zone Immich knows.** It derives one
from GPS when the file carries no offset tag, and reports UTC when it has
neither -- the same string, opposite facts, five hours apart for most of
this library:

    PXL_20240101_062038690   Model Town, Punjab, Pakistan  -> Asia/Karachi
    Snapchat-618209934       no coordinates at all         -> "UTC+0"

`zone_source()` separates them into four: the file's own offset (best --
honour it whatever the date), GPS-derived, something else Immich holds, and
nothing at all. In that last case the wall clock Immich shows *is* the UTC
instant wearing a local label, so a photo taken at 11:20 in Karachi reads
as 06:20 in Immich and 06:20 in Google Photos -- and the two agreeing is
not evidence either is right. It is the same number twice.

Which is why "Snapchat-618209934.jpg is correctly dated" was a weaker claim
than it looked: Google Photos and Immich agree because both are displaying
the same UTC instant, not because anybody established the photo was taken
at Greenwich. It was taken in Pakistan, and it is five hours early on both
screens.

That is a decision rather than a reading, so it lives in two settings --
`assume_zone_before` and `assume_zone_offset`, here 2026-03-04 and +05:00,
the date its owner left Pakistan. A file carrying a zone of its own is
honoured whatever its date, coordinates outrank the rule because a photo
taken on a trip says so itself, and the rule applies only when there is
nothing else. Blank either half and it assumes nothing.

**The modification-time fallback is only ever right at UTC.** An mtime is
an instant with no zone and Google Photos displays it as UTC -- the
Snapchat file came back labelled GMT+00:00. So every file relying on that
fallback shows its capture zone's offset early: nothing at Greenwich, five
hours across most of this library, and a day early as well for anything
taken between midnight and 05:00 local. `verdict()` says which of the three
it is, and says when the zone it used came from the rule rather than the
file.

**Blank poisons the fallback; absent does not.** Two files from this
library, the same route, both stamped with a correct modification time,
landing nine hundred days apart:

    Snapchat-618209934.jpg   tag absent          -> Jan 1 2024, 6:30 AM
    PXL_20240101_062038690   tag present, empty  -> today, 12:33 PM

One variable. A blank tag evidently reads to Android's media scanner as
metadata it cannot parse, and it never reaches the modification time; an
absent tag falls through cleanly. Which makes the blank-versus-missing
distinction the thing that *predicts* where a photo lands, not merely a
description of what is in it — so `verdict()` rescues a MISSING tag with a
good mtime and never a BLANK one, and says out loud why the correct
`FileModifyDate` two rows below it does not help.

It also narrows what needs fixing — with one caveat not yet measured. A
file with no date tag at all falls through to the mtime, and the one
observed doing so landed correctly. But that file was taken at UTC+0, where
the instant and the wall clock are the same number. An mtime is an absolute
instant carrying no zone, so a Karachi-era photo taken at 11:20:38 +05:00
is stamped 06:20:38Z, and Google Photos showed the Snapchat file's mtime
as GMT+00:00 — which predicts that such a photo displays five hours early,
and on the wrong day entirely when it was taken before 05:00 local. That is
a prediction from two data points, not a measurement. Until somebody looks
up an absent-tag Karachi file in Google Photos, "no tag is fine" is only
established for UTC.

**A modification time is a real carrier, and the verdict has to count it.**
`feeder.stamp_capture_time()` sets every delivered file's mtime to Immich's
capture instant, Syncthing preserves it to the phone, and Google Photos
falls back to it when a file has no date tag. That is not a theory:
`Snapchat-618209934.jpg` carries no date tag of any kind — nothing but
`Software: Picasa` — and Google Photos dated it Jan 1 2024, 6:30 AM, the
same second as the outbox copy's mtime.

`verdict()` said "would fall back to upload time" about that file, which
was this tool being wrong out loud about a file that was fine — the exact
failure it exists to prevent, pointed the other way. A missing tag is now
checked against the delivered copy's mtime before any such claim is made.

Two things follow. Only a copy **read in place** may be credited with its
mtime: Immich's is fetched to a temp file and is always today. And the
fallback is weaker than the tag in two specific ways — an mtime does not
survive anything that rewrites the file, and it carries no zone, so what
Google Photos displays for a photo taken outside UTC is not settled by
anything here. The confirmed case happens to be UTC+0, where the two
readings are indistinguishable.

**The mismatch figures cannot see this fault, by construction.**
`needs_date_fix()` compares `fileCreatedAt` against
`exifInfo.dateTimeOriginal`, and on a Takeout import both were filled from
the same sidecar, so they agree and it returns False. A whole library of
undated files reports zero on the Problems tab. Reading the file is the
only thing that tells them apart, which is what `verdict()` does.

**exiftool is read with `-G`.** Without it `EXIF:DateTimeOriginal` and
`XMP:DateTimeOriginal` both come back as `DateTimeOriginal` and the second
silently wins — reporting a date in the tag Google reads when the value came
from one it does not. `label()` strips the group only for the file's own
(EXIF for a still, QuickTime for a video), so two different tags can never
collapse onto one row.

Separately, and still true: `rewrite_capture_date` writes
`datetime.fromtimestamp(stamp, timezone.utc)` into `-AllDates`, which puts a
UTC wall clock into `DateTimeOriginal` — a field EXIF defines as local time.
That shifts every file it touches by the zone's offset. It is latent only
because `fix_dates` is off; switching it on without fixing that would break
correct files.

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

**Deciding to ask is the server's job, not the phone's.** It used to live
inside `companion.poll()`, which runs only when the phone checks in — so the
condition was evaluated *by the phone asking*, and `request()` is what
writes the log line. A phone that had gone quiet produced no entry of any
kind, and fifteen hours of a stalled pipeline left nothing to diagnose from
but an absence. `companion.consider()` runs from `feeder.housekeeping()`
now. Only the battery check stays at poll time, because the phone is the
only one who knows it.

**A free-up is worth asking for whenever the outbox holds anything**, not
only when work is queued behind it. The old rule tested throughput, and the
thing that actually needs the phone is confirmation: a file in the outbox is
not backed up until it disappears, and only Google Photos clearing it makes
it disappear. Once a library is fully queued there is nothing behind the
outbox at all, so the old rule declined and the last batch of every run sat
there for Smart Storage's thirty days.

**What Google Photos says about itself is a note and never evidence.** Its
home screen carries the only progress it reports anywhere — "Backing up 250
photos", "2 hours, 26 min remaining"; it posts nothing to its backup
notification channel, so there is no cheaper route. The companion reads that
during a dwell and sends it back, and it changes what the dashboard shows
and when the server asks. It must never change an asset's state:
confirmation stays in `feeder.reconcile()`, derived from absence. A string
scraped off somebody else's screen that could confirm an asset would forge
the only proof this system has, and a renamed label would do it silently.

**There are two instructions, and only one of them presses anything.**
`FREE` walks Photos to the button; `LOOK` opens it, waits, and reads the
backup panel. That split exists because reading used to ride inside a
free-up, which gave it no schedule of its own — it never happened when the
outbox was empty, which is when Photos most needs opening. A look is also
what lets the server find out whether Photos has finished *before* deciding
to press, and `companion_wait_for_backup` makes it do so.

**The leftovers are the only audit there is.** When a free-up runs while
Photos claims to have finished, everything on the phone should go; files
still in the outbox afterwards are files Google Photos has not taken,
whatever its screen said. Two things make the naive version of this wrong,
and both are handled: `top_up()` refills the outbox on its own cycle, so
`audit()` compares *confirmations* rather than the outbox count; and Photos'
media scanner lags Syncthing, so a single remainder proves nothing and only
a pile that survives several free-ups is worth raising.

**Dwelling is Google's own suggestion.** The same panel says "Keep the app
open for faster backup". A foreground app escapes both Doze and the standby
bucket an app sinks into when nobody opens it, which is why a shelf phone
uploads nothing all day and then starts the moment it is picked up. It is
off by default and gated from the server, because it costs screen time and
the person who wants it off is at a dashboard rather than at the shelf.

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
