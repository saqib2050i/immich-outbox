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

**2a. The file is passed through byte for byte — with two exceptions, both
deliberate and both recorded.**
When a date has been corrected in Immich, that correction lives in Immich's
database and `/original` still serves the untouched file, so Google Photos
would use the stale embedded date. `rewrite_capture_date()` writes the
corrected date in, and only then. It runs on the temp file after the size
check, so integrity is verified against Immich before anything is altered
and Syncthing never sees a partial edit. A file whose own date agrees with
Immich, or which carries no date at all, is never touched by it. Turned off
with the `fix_dates` setting.

The second is `diagnose.apply_correction()`, one file at a time, from a
button in Tools. It writes a capture date into a file that has *none* — the
opposite population to the rule above, and the reason Google Photos dates a
Takeout-imported library to the day it was uploaded. It never overwrites a
date that is already there, and it checks that against the file at the
moment of writing rather than against the report on screen.

Both write to the outbox copy and never to Immich, and both go through a
`.partial-` dotfile inside the outbox followed by `os.replace`, so
Syncthing never sees a partial edit. `sweep_partials()` already knows both
names a crash can leave behind.

**A deliberate correction is recorded, because otherwise it is
indistinguishable from damage.** `stamped_at` and `stamped_note` in the
ledger are what let `trace()` say "this differs from Immich because a date
was written into it here" instead of raising the alarm above — and telling
a corrected file from a corrupted one is the entire purpose of that tool,
so adding a fresh way to confuse the two while fixing one would be
careless. An unexplained difference is still an alarm.

It also narrows the re-send exception under invariant 4: that rests on the
bytes being unchanged, and a stamped file's are not, so a confirmed file
carrying `stamped_at` is refused again.

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
- **A send button counts `resting`, never `remaining`.** Forcing does not
  change a file's state, so `remaining` reads the same before and after and
  the button cannot report its own success -- it said "Send 1,620" over a
  year that had just been asked for, and pressing again did the same
  nothing. `resting` is what nobody has asked for yet, so asking takes it to
  zero and the button disappears. The control repaints with the figures for
  the same reason: the timeline rebuilds its structure only when months
  appear or vanish, so anything left to a rebuild keeps a stale number.
- **Sending is one builder, called from three places.** Year, month and
  category each drew their own controls and drifted: the year had none, the
  month's lived inside its own expanded body, and a category could send but
  never send again. `sendControls()` draws the pair everywhere, and the
  row-level ones carry `whenshut` so they disappear once you are looking at
  what is inside. They sit inside a `<summary>`, so the wrapper stops click
  propagation -- otherwise every press would toggle the row instead.
- **Confirmations live in the button, not in `confirm()`.** A browser
  dialog is a different window asking about a page you can no longer see.
  `armed()` turns the first press into "Sure? ..." and disarms itself after
  four seconds, so a stray press leaves nothing loaded. Only the two
  destructive controls use it; a plain send is one press.
- **An approval has to survive into the next fetch.** A signed-off file goes
  back to `pending`, and the fill that picks it up would otherwise read it,
  reach the same conclusion, and hold it again -- a loop in which the
  correction is never written. `approved_at` says the decision was taken;
  the feeder writes the tags recorded for the file rather than classifying
  it a second time, and spends the approval in the same breath so it cannot
  fire twice. A correction that cannot be written fails the file rather than
  delivering it uncorrected: delivered, it is in Google Photos wearing the
  wrong date for good, and a failure can be retried.
- **Nothing lists which settings are checkboxes.** There was such a list,
  and a checkbox whose key was missing from it was completely inert:
  `fillSettings` wrote the stored value into `.value` rather than
  `.checked`, so it always drew unticked, and `readSettings` sent that same
  `.value` back instead of what had been clicked. It could be ticked, saved,
  and reported as saved without anything reading it. `el.type` is the
  question now; two tests keep it that way.
- **A tab needs three things, and the third fails silently.** A panel
  (`data-tab`), a link (`data-for`), and its name in `TABS` -- `setTab`
  falls back to "overview" for anything it does not recognise, so a tab
  missing only the third has a link that appears to do nothing and logs
  nothing. A test now checks the three agree.
- **`summary::before` is a grid item.** Every disclosure row here is a grid
  whose first column is the caret, so the column count must include it. Both
  timeline rows were a column short, which pushed the last child onto a
  second row and into column one — and an `auto` column sizes to its widest
  item, so a figure sitting underneath shoved the year label into the middle
  of the row.

**Nor must a refresh.** Which years, months and file lists are open, the
Dates grouping and sort, and which Dates groups are expanded all live in
`localStorage` through `remember()` / `recall()`, both wrapped because a
private window throws on the first read rather than returning nothing. Two
"open something useful" defaults yield to a choice made on an earlier visit,
not only to one made in this session -- and the Dates one used to reapply on
*every* render, so a group collapsed by hand reopened the moment a file was
signed off out of it.

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

**A held file's verdict must not go stale.** It is never claimed a second
time, so whatever was decided when it was read is what it keeps -- and a
rule changed in Settings afterwards never reaches it. `hold_says` keeps what
Immich said, and `diagnose.rejudge()` works the answer out again from that.
Readings -- coordinates, the file's own offset -- are left alone, because
no setting improves on them.

**What a sign-off writes is what the page showed.** `rejudge()` is called
twice: as the Dates tab draws each row, and again by `/api/dates/release`,
which stores its answer *with* the approval, because the feeder writes
whatever `hold_writes` holds. Until 2.16.2 only the first happened, so a
row re-judged on screen was signed off carrying the stored answer the
screen had just corrected -- and one that had been unfixable was released
with nothing to write and delivered uncorrected. A release with nothing to
write is refused now, and the feeder fails an approval that somehow has
none rather than sending the file on.

**A rule verdict needs nothing but the ledger.** Under the rule the wall
clock is the capture instant plus the rule's offset, and `taken_at` *is*
Immich's `fileCreatedAt` -- so a row read before `hold_says` existed is
re-judged from the ledger alone (`_from_the_ledger`). Only a row outside
the rule with nothing kept is marked `stale`, and only those are offered
"Read N again". It used to key on the missing `hold_says`, which on a
library read before 2.16.0 was every held row there was. Keep that
distinction: a zone that needs Immich's answer cannot be invented from the
ledger, and one that does not need it should not cost a download.

**The fault is not the ceiling.** `hold_kind` is what is wrong with the
file (blank, absent); whether anything can be written is `hold_writes`
being empty. They were one field, `unfixable` overwrote the fault, and a
row a later rule reached could no longer say whether its tag had been empty
or missing -- those surface as `unrecorded`.

**The wall clock follows the zone that was chosen, not the one Immich
chose.** `localDateTime` is `fileCreatedAt` converted through Immich's own
`timeZone`, so it is the wall clock only while that zone is the one in use.
Two cases where it is not: the rule wins *against* Immich's zone, so its
conversion applies the zone that just lost; and an offset in the file that
Immich never saw, because it was written into the outbox copy after import.
Both were wrong while this was keyed on whether Immich knew *a* zone -- the
same question only for as long as the rule could not outrank one.

**Immich's `localDateTime` is the wall clock only where Immich knew a
zone.** It is `fileCreatedAt` converted through `exifInfo.timeZone`, so
where that was the UTC fallback the two are the same number and the true
wall clock is that plus the offset. `_immich_knew_the_zone()` decides, and
asks three things: coordinates, a non-UTC `timeZone`, and finally whether
`localDateTime` and `fileCreatedAt` differ at all -- which they can only do
if Immich applied a zone, and which cannot be out of step with the numbers
beside it.

Keying that on *our* provenance instead was a live bug: correcting a file
gave it an `OffsetTimeOriginal`, which flipped the derivation, and the very
next trace accused the file of being five hours from Immich -- exactly the
correction it had just been given on purpose.

**Every finding is about the delivered copy.** `ref` in `_findings()` is
the outbox copy, and Immich's only when there is not one. It used to be the
other way round, and the consequence only appeared once phase 3 existed: a
file that had just been corrected came back saying "Immich knows when this
was taken and the file does not" and "no zone in the file", because the
copy those read was the untouched original -- which can never carry a
correction, since not touching Immich is invariant 3.

For the same reason Immich's own verdict is **context, not a score**, once
a delivered copy exists. It will always read as undated for exactly the
files this corrects, so scoring it pass/fail puts a cross beside a fixed
file. With nothing in the outbox it is the only verdict there is, and then
it is scored normally.

**Proposing a correction (phase 2).** `diagnose.propose()` describes what
would be written and writes nothing -- a test asserts its source contains
no `subprocess`, no `open(`, no `os.utime` and no `UPDATE`. It rides on the
trace rather than a second endpoint, because it reads nothing the trace has
not already read, and the button in Tools only reveals it.

EXIF gives no choice between correcting the time and recording the zone.
`DateTimeOriginal` is *defined* as local time with no zone, so the value
written is the wall clock and `OffsetTimeOriginal` is what stops it being
ambiguous. Writing the UTC instant there with an offset beside it says the
photo was taken five hours earlier than it was.

The wall clock is derived two different ways and using the wrong one is the
five-hour error again:

    zone from GPS or the file   localDateTime is already the wall clock
    zone from the owner's rule  localDateTime is the INSTANT, so add the offset

The second holds because Immich reports UTC when it has nothing to go on,
so its `localDateTime` is the instant wearing a local label. A video is the
third case and the opposite of both: QuickTime's `CreateDate` is UTC by
specification, so it takes `fileCreatedAt` and no offset belongs beside it.

Where the zone cannot be established at all, nothing is proposed and the
report says why -- the instant is known and the wall clock is not, and
there is no honest value for a tag defined as local time.

**A GIF cannot carry EXIF, and nothing here accounts for that yet.** Given
the four tags a still is proposed, exiftool 13.25 writes
`XMP-exif:DateTimeOriginal` and `XMP-xmp:CreateDate`, drops both offsets
without a word, and exits 0 -- so the correction reports four tags written
and the ledger records four, over a file holding two, with no zone.
Whether Google Photos reads XMP in a GIF at all has not been measured.
Measured on a 1x1 GIF; a PNG takes all four, as an eXIf chunk.

**A zone Immich reports is not always a zone Immich knows.** It derives one
from GPS when the file carries no offset tag, and reports UTC when it has
neither -- the same string, opposite facts, five hours apart for most of
this library:

    PXL_20240101_062038690   Model Town, Punjab, Pakistan  -> Asia/Karachi
    Snapchat-618209934       no coordinates at all         -> "UTC+0"

Immich also writes offsets as `UTC+1`, `UTC+05:30`, `UTC-3` where it has no
IANA name, which is neither an EXIF offset nor a zone name. A parser knowing
only `+05:00` reported 42 files as having no zone at all while Immich was
holding one for every one of them.

And a zone Immich reports with no offset tag in the file and no coordinates
is Immich's own default -- in practice the machine that ran the import. A
2022 Karachi photo came back `UTC+1` because that is where its owner lives
now. So the owner's rule outranks it, and the order is what each one is: the
file's own offset (the camera), coordinates (where the shutter was pressed),
the rule (somebody saying where they were living), Immich's `timeZone` (a
server saying where *it* is), nothing.

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
`assume_zone_before` and `assume_zone_offset` -- the date its owner left
Pakistan, and the offset they were living at. Deliberately not written down
here: it is a setting, and a copy of it in a document is a copy that goes
stale without anything failing. A file carrying a zone of its own is
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

**A written correction lands, zone and all. Measured.** The first one to be
looked up in Google Photos afterwards: `20211010_155825-COLLAGE.jpg` left
here carrying `DateTimeOriginal 2023:06:15 18:28:01` and
`OffsetTimeOriginal +05:00`, and Google Photos shows *Jun 15, 2023 — Thu,
6:28 PM GMT+05:00*. Both tags were read. The offset especially: a zone
appears on that screen only because one was written into the file, and
`+05:00` is not where the server, the phone or Google is.

It also settles which carrier wins. That file's modification time is the
capture instant, `17:28:01Z`, which Google Photos would have displayed as
5:28 PM GMT+00:00 — the reading it gives every undated file here. It shows
6:28 PM instead, so the tag beat the mtime.

The value itself was wrong, and that is the other half of the lesson: it
was written by a build that ranked the owner's rule above Immich's zone but
still took Immich's clock, so the photo sits in Google Photos four hours
early, wearing a correction this service is proud of. A wrong date written
deliberately is indistinguishable, from the outside, from a wrong date that
arrived — except that this one carries an offset nothing in the original
had. Hence 2.16.2, and hence `revised_from`.

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

CI rebuilds the APK only when `companion/**` has changed, and restores what
was built for those exact sources otherwise -- the version split is what
makes that safe, since the APK no longer moves when the server does, and
nearly every run here is server-only. `restore` and `save` rather than the
combined action, and saved only after a build that worked: the combined one
writes from a post step that runs even when the job failed, which would
store an empty `dist/` under those sources' key and leave every later run
hitting it, shipping no app, and saying nothing.

CI builds the APK and bundles it into the image at `dist/companion.apk`, so
the phone updates itself from `/app` rather than from a cable. Two things
follow. **The app keeps its own version in `companion/VERSION`**, separate
from the server's root `VERSION`. They shared one file so that a pair could
never be untested together -- but the server moves for reasons the app has
no part in, and nine server releases in two days each told the phone it was
out of date over an APK byte-identical to the one already on it. What a
build can actually do is carried by the `features` list in the protocol,
which says so directly; a matching number only looked as though it did.

Bump `companion/VERSION` when something in `companion/` changes. It must
only ever go **up**: `build.gradle.kts` turns it into a `versionCode`
(`major*10000 + minor*100 + patch`) and Android refuses an APK whose code
is not above the installed one. The split nearly shipped the app at 1.0.0 --
code 10000, against 21000 already on the phone -- which Android would have
turned down as a downgrade, silently and forever. A test enforces the
floor. `apk_info()` reports "unknown" rather than falling back to the
server's number, since after the split that fallback would announce an
update every time the server moved. And Android installs an update only over the same
signing key, so CI needs a stable one from `ANDROID_KEYSTORE_BASE64`; absent
it the build is debug-signed and the install page says so, because failing
the release of a *server* over a phone app would be the wrong trade.

Scheduling must go through `AlarmManager`, never a `Handler`. A Handler
callback cannot wake a sleeping CPU, so with the screen off — which is the
whole point of a phone on a shelf — the app simply never checks in. It also
must hold a `PARTIAL_WAKE_LOCK` across the check-in, because the alarm wakes
the phone only for the length of the broadcast and the work happens on
another thread after that returns.

**A free-up is not finished when a figure first appears.** The walk's
budget is `MAX_STEPS` x `POLL_MS`, about half a minute, and clearing several
gigabytes on a 2016 phone takes several minutes -- so it ran out of steps,
fell through to its free-space fallback, measured the disk *mid-operation*
and reported 3.5 GB of a 5.85 GB clear-out as the total. Photos carried on
and nothing told the server.

`waitForQuiet()` waits for free space to stop climbing before either path
reads its figure. Free space is the signal on purpose: it needs no labels,
and the labels are the part of this app most likely to be renamed without
warning -- a progress string is exactly that kind of string, and a disk
getting emptier is not. When the budget runs out with the figure still
moving, `settled` goes false, the run stays a success (the button *was*
pressed) and the dashboard says "at least". A floor reported as a total is
the same class of fault as a stale screen read as a result.

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
