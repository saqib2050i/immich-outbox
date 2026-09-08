# Changelog

The number in `VERSION` is the only place a release is named. It is
`x.y.z`, and the parts are meant literally:

- **x** — an overhaul: the shape of the thing changed.
- **y** — a feature: it does something it could not do before.
- **z** — a fix: it does what it always claimed to.

Bump it in the same pull request as the change, so the published image and
the entry below can never disagree.

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
