# Immich outbox feeder

The missing middleman. Immich and Google Photos both stay exactly as they
are — this only decides *what* to send next and remembers what has already
gone, which is the one job nothing off-the-shelf does.

```
Immich ──► outbox folder ──Syncthing──► Pixel 1 ──► Google Photos
              ▲                        DCIM/ImmichQueue   (original quality)
              └──────── deletions flow back ────────┘
                        (Smart Storage, after 30 days)
```

**On the Pixel you install nothing but Syncthing.** No custom app, no ADB,
no accessibility service, no root. It survives reboots on its own.

## Why a ledger is needed at all

Point any Immich exporter at a folder and it decides what to skip by looking
at what's already on disk. But this folder is *meant* to empty — so the next
run re-sends everything, forever. The ledger is what breaks that loop: it
records that an asset made the trip, even after the file is long gone.

Settings live in the database and are edited on the dashboard — the
environment variables in `docker-compose.yml` are only the initial defaults.
Changes apply on the next cycle; no restart.

| State | Meaning |
|---|---|
| `pending` | in Immich, never sent |
| `queued` | in the outbox, mirrored to the phone |
| `confirmed` | gone from the outbox → backed up |
| `failed` | download errored; retried up to 5 times |
| `skipped` | excluded on purpose (oversized, video when off, or a motion-photo clip) |

### Motion photos

Immich stores a Pixel motion photo as two assets: the still `.MP.jpg`, which
still contains the embedded clip, and an extracted `.MP.mp4` component. Only
the still is ever relayed. Sending the component as well puts a stray
silent video in Google Photos beside the photo, and the still already
carries the motion, so nothing is lost by skipping it.

Components are identified by the `livePhotoVideoId` link on the still, not
by filename, so this holds regardless of how they are named. They are the
one thing the service will delete from the outbox — safe, because a
component is never counted as backed up, so removing it cannot be mistaken
for confirmation.

**Confirmation is evidence.** This service never deletes from the outbox.
Only the phone deletes, only Smart Storage deletes there, and Smart Storage
only removes copies Google Photos has verified it holds. So a file
disappearing is proof of backup, not an assumption.

**The cap is the flow control.** The outbox mirrors the phone's queue
folder, so capping the outbox caps the phone. If backup stalls, the outbox
stops draining, the feeder finds no room, everything waits. Immich still
holds every original, so the worst case is a delay, never a loss.

## 1. Server (Unraid)

Push this repo to GitHub once and CI publishes a multi-arch image to GHCR —
see [PUBLISH.md](PUBLISH.md). Then on the server there is no checkout and no
build:

```bash
mkdir -p /mnt/user/appdata/immich-outbox \
         /mnt/user/photo-outbox \
         /mnt/user/photo-outbox-spool
# edit docker-compose.yml: replace YOUR-GITHUB-USERNAME, set IMMICH_URL
docker compose up -d
```

To build from source instead:
`docker compose -f docker-compose.dev.yml up -d --build`

### The API key

Immich → Account Settings → API Keys. Tick exactly three permissions:

| Permission | Used for |
|---|---|
| `asset.read` | `POST /search/metadata` — finding what is in your library |
| `asset.download` | `GET /assets/{id}/original` — fetching the originals |
| `server.about` | reading the server version, so the right request format is used |

Nothing that writes: no `asset.update`, `asset.delete`, `asset.upload`, no
album or job permissions. The relay only ever reads, which is why Immich
stays a safe source of truth. Don't reach for `all` when debugging — a
read-only key means a bug here cannot damage your library.

Make it on your own user account, not an admin one.

### Checking it works

The dashboard tests the connection every cycle and on demand, and shows a
banner across the top when something is wrong. It runs the same four calls
the relay does and tells you which one failed:

- **Reachable** — wrong address, wrong port, or no route from the container
- **Version** — falls back to assuming v3 if it cannot read it
- **Read library** — `asset.read`
- **Download originals** — `asset.download`, checked with a one-byte range
  request rather than pulling a whole photo

A rejected key (401) and a missing permission (403) are reported
differently, because the fixes are different.

Dashboard on `:8099`.

Keep the spool on the same filesystem as the outbox. Downloads land there
first and are moved in atomically — a half-written file inside the outbox
would get synced and uploaded as-is.

## 2. Avoiding duplicates with what is already in Google Photos

Your existing Google Photos copies are Storage Saver. A Storage Saver copy
and the original are different bytes, so Google sees two unrelated items —
re-uploading originals over the top gives you two of everything. There is no
way to upgrade an item in place, and no way to script the cleanup: since
March 2025 the Library API only touches media your own app uploaded, and it
has never had a delete method. Whatever you clear, you clear by hand at
photos.google.com.

Two dials on the dashboard handle this, and they work together:

**New photos** — everything taken on or after a cut-off date, forever. Set
the cut-off to today and new photos flow through the Pixel from now on. No
duplicates, nothing to manage.

**History** — a start/end window you advance one month at a time. The loop:

1. Confirm Immich has that month complete.
2. On **desktop web**, delete that month from Google Photos, then **empty
   the trash** — trashed items keep counting against your quota until you do.
3. Switch the history window on for that month and let the originals flow.
4. When the bar reads *finished*, press **Next month**.

Do the deleting on the web, never in the Photos app on a phone with backup
on, or you will delete the local copies too. Take a Google Takeout before
the first batch — Immich should already have everything, but mass-deleting
your only cloud copy on that assumption deserves one extra backup.

Worth knowing: Storage Saver copies uploaded after 1 June 2021 count against
your quota, and those are what you are paying for. Replacing them with
Pixel-uploaded originals makes them free. Anything from before that date is
already free, so for those the question is only quality and clutter.

## 3. Syncthing

Share `/mnt/user/photo-outbox` with the Pixel, mapped to
`/storage/emulated/0/DCIM/ImmichQueue`.

Three settings that matter:

- **Folder type: Send & Receive on both sides.** Receive-only would fight
  the phone's deletions instead of accepting them, and the outbox would
  never drain.
- **File versioning: None.** Otherwise deleted files move to `.stversions`
  and the space is never actually freed.
- On Android, use **Syncthing-Fork** and enable the media-scanner
  notification, or Google Photos won't see new files for hours.

## 4. Google Photos on the Pixel

- Backup **on**, upload size **Original quality**
- Back up device folders → **ImmichQueue** only
- Turn Photos backup **off on your main phone**, or you'll get duplicate
  low-quality copies beside the originals

## 5. Smart Storage — the part that does the deleting

Settings → Storage → **Smart Storage** → Remove backed up photos & videos →
**30 days**.

This is native to the Pixel and only removes what Google Photos has
confirmed. Nothing else on the phone needs configuring, and there is no
automation to break.

## Sizing

The outbox cap is the only number worth thinking about. It caps the phone,
and because Smart Storage holds each file for 30 days after backup, it's
also roughly your monthly throughput.

| Cap | Phone holds | Roughly per month |
|---|---|---|
| 6 GB | 6 GB | 6 GB |
| 10 GB | 10 GB | 10 GB (default) |
| 14 GB | 14 GB | 14 GB — tight on a 32 GB device |

A 32 GB Pixel 1 has around 20 GB usable, so 10 GB leaves real headroom.
For a big first backfill you can raise it temporarily and use Google Photos'
manual **Free up space** to drain faster, then set it back and let Smart
Storage take over.

## Before you commit to a backfill

Set `MIN_TAKEN_AT` to last week, let twenty photos through, then check
Google One storage. If usage climbed, the Pixel exemption isn't applying and
none of the rest matters.

## Statistics

The dashboard breaks the library down by what Storage Saver would have cost
you, since that is the only thing that decides whether relaying an item is
worth anything:

| Bucket | Gains from this? |
|---|---|
| 4K video | Yes — Storage Saver caps video at 1080p |
| 1440p video | Yes |
| 1080p video | No, already at the ceiling |
| 720p and below | No |
| Photos over 16 MP | Yes — Storage Saver resizes these to 16 MP |
| Photos 16 MP or less | No |

Each row shows file count, size, total duration for video, and a bar for how
much of that bucket is confirmed. Rows that gain quality are marked.

Video is bucketed by its **short side**, so portrait 4K clips are counted as
4K rather than falling into a lower bucket.

Underneath, the totals give the measured throughput and a completion
estimate built from what has actually been confirmed, not from the outbox
cap. Until the first backups come back — about 30 days after upload, when
Smart Storage clears them — there is no estimate, and it says so rather than
inventing one.

Dimensions come from Immich's EXIF data. An existing database is migrated
automatically and fills in on the next full scan; nothing needs resetting.

### Browsing and sending a category

Each resolution row has **Show files** — the actual files in that bucket,
largest first, with dimensions, size, state and any error, paged 25 at a
time. Each one has **Send this**; the row has **Send all**, which needs a
second click to confirm.

Sending this way sets a `forced` flag: those assets ignore the date windows
entirely and go to the front of the queue. So you can push all your 4K video
through first, whatever the cut-off date says, without touching any other
setting.

Two things it deliberately will not do. It never re-sends an asset that is
already confirmed — that is in Google Photos, and sending it again would
just make a duplicate. And it does not raise the outbox cap: forcing changes
the *order* work happens in, not how much is in flight, so the phone still
cannot overfill.

Useful in practice for pushing what actually gains quality first. Your 4K
video is where Storage Saver cost you the most, and it is usually a small
number of large files — worth doing before thousands of photos that will
take months.

### Month by month

Below the media breakdown, every month in the library, grouped by year and
collapsed. Each year shows its file count, size and percent backed up; open
it for the months, each with photo and video counts, size, a progress bar,
and a **Send this** button that points the backfill window straight at it.

That button is the backfill workflow in one click, but the order still
matters: clear the month from Google Photos *first*, then send it, or you
end up with the Storage Saver copy and the original side by side.

A month whose bar is full is finished — every photo in it is confirmed
backed up at original quality, and it is safe to move on. The currently
selected month is outlined, and open years stay open through the refresh.

## Testing tools

The dashboard has a collapsed **Testing tools** section with everything
needed to re-run a test cleanly. The two safe actions act on one click; the
three destructive ones need a second click to confirm and disarm themselves
after five seconds.

| Action | What it does | Keeps |
|---|---|---|
| Retry failed | Clears the attempt counter | Everything |
| Rescan now | Walks the whole library immediately | Everything |
| Empty the outbox | Deletes what is waiting, re-queues it | Backed-up history, settings |
| Send everything again | Starts the whole library over | Settings, motion-clip knowledge |
| Start fresh | Clears the ledger and rescans | Settings, including the API key |

Settings live in the same database as the ledger but are never wiped, so a
reset cannot silently drop you back to `MIN_TAKEN_AT: 1970` and start
queuing your entire history.

None of this touches Immich, and nothing can remove photos already uploaded
to Google Photos — clear those at photos.google.com. For a genuinely clean
test also empty `DCIM/ImmichQueue` on the Pixel, or Syncthing pushes the old
files straight back.

## Watch out for

- **Pixel 1 is EOL** (last patch 2019). Isolated VLAN, reachable only by
  Syncthing and the internet.
- **A 2016 battery on permanent charge swells.** Put the charger on a smart
  plug and cycle it rather than leaving it pinned at 100% in a drawer.
- **Immich version.** Tested against v3.1. The client detects the server
  version at startup (shown on the dashboard) and adapts: on v3 it filters
  by `visibility`, on v1/v2 by the old `isArchived` flag. This matters —
  in v3, omitting visibility means *any* visibility, so a v1-era client
  would quietly start relaying your archived and hidden photos. All the
  API surface lives in `app/immich.py`; if scans go empty after an upgrade,
  compare with `<IMMICH_URL>/api/docs`.
- **"Needs attention"** lists files sitting in the outbox longer than 45
  days. That means Smart Storage is off, backup is paused, or Photos is
  refusing that file type.
- **Unraid and cross-device renames.** `/mnt/user` is a FUSE overlay: two
  folders in the same share can sit on different physical disks, so a
  rename between them fails with `EXDEV`. That is why the temp file lives
  inside the outbox rather than in a separate spool.
- **The ledger holds your whole library**, and the two date windows only
  decide what is released. Widening a window takes effect on the next cycle
  — no rescan needed.
- **Two scan passes.** A quick 30-day window every 15 minutes, the whole
  library every 6 hours. The full pass is what catches old imports — scans,
  WhatsApp dumps, camera card imports — whose capture date is years back and
  which the quick window would miss forever.
