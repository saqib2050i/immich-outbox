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
| `skipped` | excluded on purpose (oversized, or video when off) |

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

Immich API key: Account Settings → API Keys. Dashboard on `:8099`.

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

## Watch out for

- **Pixel 1 is EOL** (last patch 2019). Isolated VLAN, reachable only by
  Syncthing and the internet.
- **A 2016 battery on permanent charge swells.** Put the charger on a smart
  plug and cycle it rather than leaving it pinned at 100% in a drawer.
- **Immich API drift.** Both endpoints live in `app/immich.py`. If scans go
  empty after a server upgrade, compare with `<IMMICH_URL>/api/docs`.
- **"Needs attention"** lists files sitting in the outbox longer than 45
  days. That means Smart Storage is off, backup is paused, or Photos is
  refusing that file type.
- **The ledger holds your whole library**, and the two date windows only
  decide what is released. Widening a window takes effect on the next cycle
  — no rescan needed.
- **Two scan passes.** A quick 30-day window every 15 minutes, the whole
  library every 6 hours. The full pass is what catches old imports — scans,
  WhatsApp dumps, camera card imports — whose capture date is years back and
  which the quick window would miss forever.
