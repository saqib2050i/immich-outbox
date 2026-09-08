# Changelog

The number in `VERSION` is the only place a release is named. It is
`x.y.z`, and the parts are meant literally:

- **x** — an overhaul: the shape of the thing changed.
- **y** — a feature: it does something it could not do before.
- **z** — a fix: it does what it always claimed to.

Bump it in the same pull request as the change, so the published image and
the entry below can never disagree.

## 1.2.0

**The settings page, rebuilt.** It had grown to eight unlabelled blocks in
one column, and the Limits section packed three separate settings into a
single flex row — which is what made it look, accurately, clustered.

- One setting per row, always. A test now enforces it.
- A sticky index beside the panels, so you can jump to a section instead of
  hunting for it.
- Save follows you down the page rather than sitting past eight sections of
  scrolling, and says when there is something unsaved.
- Sections carry a line explaining what they are for.

**You can now tell whether the phone companion works.** Pairing was a code
you typed into the app and then had no way to check: the only sign of life
was a card on another tab, hidden unless the companion was switched on — so
*paired but not enabled*, the one state most needing a diagnosis, showed
nothing anywhere. Settings now carries a live panel: paired or not, last
check-in, battery, free space, last run, and a **Free up space now** button.
It names that state explicitly rather than staying blank.

Also fixed: the settings grid was flattened to a single column by the tab
switcher's `display:block`, and the section index ignored a reduced-motion
preference.

## 1.1.0

**The Pixel companion.** An app on the phone presses Google Photos' own
"Free up space", so the outbox drains in about a minute instead of waiting
roughly thirty days for Smart Storage. That wait was the pipeline's
throughput limit: the outbox cap is the only flow control, so nothing new
goes out until the phone lets go of what it already has.

It never deletes a photo itself. Google Photos still decides what has been
backed up and is safe to remove, so absence of a file remains honest proof
of a backup. The app declares no storage permission at all, which makes
that Android's guarantee rather than a promise — and nothing the phone
reports is treated as confirmation.

- The relay asks by itself exactly when it is blocked: outbox full, with
  files waiting behind it. A cooldown stops it asking again before Google
  Photos has uploaded the replacements.
- A pairing token, shown once in Settings, and stripped from downloaded
  backups with the other credentials.
- The phone going quiet raises an alert — otherwise the symptom is
  indistinguishable from Google Photos simply being slow.
- The button labels the app looks for are editable in Settings, so a
  Google rename does not need a new app.

## 1.0.0

The first release with a version worth comparing. Previously the image
reported `build 44` — a workflow run number, which goes up on every push
and says nothing about what moved.

**Safety**

- The outbox is now proved to be mounted before absence is read as proof of
  backup. A bind mount that did not come up used to look exactly like Google
  Photos having verified the entire queue, which silently confirmed assets
  that were never sent. A marker dotfile inside the outbox disappears
  exactly when the outbox does.
- Assets Immich no longer has are tracked as `missing` rather than counted
  as work forever. All eleven queries agree on this now; `reconciliation()`
  was the one that did not, and it reported 2,705 unsendable assets as
  "waiting its turn".
- Housekeeping — silent-failure alerts and ledger backups — actually runs.
  Both were imported and never called.

**Features**

- A queue you can see and steer: live per-file transfer progress, cancel,
  and pause/resume, replacing a fixed batch of 40 that appeared and
  finished with no indication of either.
- Concurrent downloads with separate lanes for photos and videos, filling
  to the byte cap instead of a fixed file count.
- Capture dates corrected in Immich are written into the file, so Google
  Photos stops dating them the day they arrived. Off by default; affected
  files are collected in their own category to enable and send deliberately.
- A dashboard in tabs — Overview, Queue, Problems, Library, Settings,
  Tools — with the running version and build age on the front page.
- Backlog, failures and missing assets can each be dismissed, and dismissal
  is no longer a one-way door.

**Fixes**

- A `500` from one panel no longer aborts the whole dashboard render.
- The Queue tab stopped flickering between two counts: two functions were
  writing the badge with different numbers on every tick.
- Startup no longer crashes on a database predating the `outbox_name`
  column.
- A crash between the rename and the ledger write left a file on disk that
  the ledger thought was pending; the fill loop then handed it back
  forever.
- Rescans pick up dates corrected in Immich, which `INSERT OR IGNORE` had
  been discarding.

**Testing** — 0 to 227 tests, run by CI, which will not publish an image
if they fail.
