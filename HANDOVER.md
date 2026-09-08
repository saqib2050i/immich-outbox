# Handover — immich-outbox

Written for a fresh session picking this up to **propose features and a UI
overhaul**. Read `CLAUDE.md` first for the architecture and the hard-won
details; this file covers what that one does not: current state, what the UI
actually is today, and which ideas are dead on arrival and why.

Date of handover: 2026-08-30.

---

## 1. What the thing is, in one paragraph

A relay that gets full-quality originals out of Immich and into Google Photos
by routing them through a Pixel 1, which still has unlimited original-quality
backup. This service is **only the ledger and the outbox feeder**. Syncthing
moves the bytes to the phone, Google Photos uploads them, and Google's Smart
Storage deletes the local copy once it has verified the backup. FastAPI +
SQLite + a single vanilla-JS HTML file, no build step, deployed as a GHCR image
onto Unraid. Single user, on a LAN.

The consequence that shapes everything: **the service has no way to ask Google
Photos what it holds.** A file vanishing from the outbox is the only evidence a
backup happened. That one fact is why so much of the code looks paranoid.

---

## 2. Where things stand right now

`main` is at the merge of PR #2. Four PRs, all raised in the last session:

| PR | What | State |
|----|------|-------|
| #1 | docs: CLAUDE.md matched to the code | merged to `main` |
| #2 | fix: boot against a pre-`outbox_name` database | merged to `main` |
| #3 | feat: live queue, cancel, pause/resume | merged, but into the #2 *branch* |
| #4 | chore: carry #3 across to `main` | **open — needs merging** |

**Check #4 first.** PR #3 was stacked on the #2 branch and merged there after
#2 had already gone to `main`, so the queue feature is not on `main` and not in
the published image. If `git show origin/main:app/main.py | grep /api/queue`
comes back empty, #4 is still outstanding and anything you build on top of the
queue will be built on sand.

Test suite: **110 tests, ~5s.** CI runs it and refuses to publish an image if it
fails.

```bash
docker run --rm -v "$PWD":/srv -w /srv python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Use Docker: the code needs Python 3.10+ and the dev machine here has 3.9.

There is no `gh` CLI and no Homebrew on this machine. PRs in the last session
were opened against the GitHub REST API using the credential already in the
macOS keychain, piped from `git credential fill` straight into `curl -K -` so
the token is never printed or stored.

---

## 3. The invariants — read these before proposing anything

These are in `CLAUDE.md` too, but they are the reason most obvious feature ideas
are wrong, so they bear repeating in the context of *design*.

1. **The service never deletes from the outbox.** Absence is how a backup is
   recorded, so deleting a file silently marks it as backed up when it is not.
   Two sanctioned exceptions, both of which write the ledger first:
   `purge_motion_parts()` and the new `feeder.cancel()`.
2. **Absence only counts when the outbox is genuinely there.**
   `feeder.outbox_ready()` keeps `.immich-outbox-mounted` inside the outbox so
   it disappears when the mount does. Without it, a bind mount that failed to
   come up is indistinguishable from Google Photos having verified the whole
   queue. Nothing may delete that marker.
3. **The outbox cap is the only flow control.** The outbox mirrors the phone's
   queue folder, so capping it caps the phone. Never write past the cap.
4. **Immich is read-only.** Three permissions, no write scope, ever.
5. **Confirmed assets are never re-sent.** Re-sending makes a duplicate in
   Google Photos, which is the exact cost the user is trying to eliminate.

Anything touching `app/feeder.py` or `db.confirm_absent()` is load-bearing.
`tests/test_outbox_guard.py` and `tests/test_relay.py` exist to catch exactly
these mistakes — if a change makes them fail, the change is wrong, not the test.

---

## 4. Ideas that are already dead, and why

Save yourself proposing these:

- **"Show what's actually in Google Photos" / reconcile against the cloud.**
  Impossible. The Library API was restricted to app-created media in March 2025.
  A Google Takeout importer is the only route and has been discussed, not built.
  It is the single highest-value unbuilt feature, and it is a big one.
- **"Delete from the outbox to free space" / tidy-up features.** See invariant 1.
- **"Upload straight to Google Photos, skip the phone."** The whole point is that
  the Pixel 1 has unlimited original-quality backup and the API does not.
- **"Speed it up."** Throughput is structurally capped: Smart Storage holds each
  file ~30 days after backup, so you move roughly one outbox-worth per month.
  `db.structural_rate()` computes this. A UI that implies you can go faster by
  turning a knob is lying — the honest move is to *show* the ceiling.
- **A frontend framework / build step.** Deliberate: no npm, no bundler. The
  dashboard is one file served directly. Keep it that way unless the user
  explicitly asks otherwise.
- **Multi-user, roles, sharing.** One user, one LAN, sessions in memory.

---

## 5. The UI as it exists

**One file: `app/static/dashboard.html`, 1,866 lines** — 318 CSS, 1,161 JS, the
rest markup. Plus `app/static/login.html` (82 lines). No build, no dependencies,
no external requests.

### Structure (in DOM order)

| Section | id | What it shows |
|---|---|---|
| Header + status pill | — | live/polling badge, connection state |
| Banner | `#banner` | one-line "what's wrong / what to do" |
| Alert box | `#alertBox` | active alerts from `alerts.evaluate()` |
| Pipeline belt | — | three-segment bar: Waiting / In the outbox / Backed up |
| Outbox card | — | gauge, holding, used-of-cap, last cycle, folder, mount problem |
| Immich card | — | server, version, scans, connection checks |
| **Queue** | `#nav-queue` | **new in #3** — per-file list, cancel, pause/resume, current fetch |
| Needs attention | `#nav-attention` | stuck and failed assets |
| Backfill window | — | month stepper with progress |
| Settings | `#settings` | every key in `settings.SPEC` |
| Timeline | `#nav-timeline` | per-month bars, click to drill in |
| Library | `#nav-library` | resolution buckets, what original quality buys |
| Tools | `#nav-tools` | `<details>` — resets, backups, danger zone |

### Design language

Deliberate and worth preserving even in an overhaul — it is a *print-ish,
instrument-panel* look, not a generic SaaS dashboard.

```
--paper  #DCE0DA   cool grey-green stock, not cream
--panel  #E6E9E3
--ink    #111E1A   bottle-green black
--rule   #A6AFA6
--dim    #5C6660
--transit #1B4FD8  cobalt: moving
--done   #5E7C00   olive-chartreuse: landed
--alarm  #A82118   burnt red: stuck
```

Dark theme is a `prefers-color-scheme` override of the same tokens. Mono for
data and filenames, system sans for prose. Semantic colour is doing real work:
cobalt = in transit, olive = landed, red = stuck. Keep that mapping.

### How it updates

`GET /api/events` is an SSE stream. `db._revision` bumps on **every ledger
write**; the stream pushes `changed` and the page calls `tick()` → `render()`.
Polling every 5s is the fallback when the stream is down; 30s when it is up.

Two things follow that matter for any UI work:

- `render()` refetches `/api/status` **and** `/api/queue` on every change. During
  a batch that is now several times a second. Anything you add to `render()` is
  in that hot path.
- Any new `set_meta()` you add bumps the revision and therefore triggers a
  re-render for every connected browser. `syncthing.py` used to write a key
  nobody read purely as a side effect; that was removed for this reason.

### Known UI weaknesses (fair game for the overhaul)

- **The Settings section is a wall of ~21 inputs** with no grouping or
  progressive disclosure. It is the least considered part of the page.
- **The page is one long scroll** with a jump nav; no routing, no deep links
  beyond anchors.
- **Mobile is untested.** The user checks this from a phone in practice. There
  are a couple of `@media` rules and that is it.
- **Accessibility is thin.** Some `aria-label`s, one `role="status"`. No focus
  management, no keyboard path through the queue list.
- **No empty-state design** beyond a couple of `.empty` strings.
- **The Timeline and Library sections overlap conceptually** — both are
  "what is in my library, by month/kind" and a user has to work out the
  difference.
- **Screenshots could not be captured** in the last session's browser tooling
  (blank frames), so the Queue section is verified by DOM inspection only.
  **Look at it in a real browser before redesigning around it.**

---

## 6. Where the seams are, if you want to build features

- `app/settings.py` — `SPEC` is the single source of truth for settings. Add a
  key there and it gets persistence, casting, live reload and the API for free.
  The dashboard renders inputs from it.
- `app/db.py` — every write goes through here and bumps the revision. `BUCKET_SQL`
  and `GAIN_SQL` define "does original quality actually buy anything for this
  asset" (photos > 16 MP, video > 1080p, i.e. what Storage Saver would have
  degraded). Reuse them rather than redefining the rule.
- `app/main.py` — 35 routes. Everything is behind a session cookie except
  `/login`, `/api/login`, `/healthz`.
- `app/feeder.py` — the cycle. `CYCLE_LOCK` must be held by anything that
  reconciles or tops up; four dashboard routes learned that the hard way.
- `app/alerts.py` — `evaluate()` returns the active problems. New failure modes
  should become alerts here rather than bespoke UI.

### Feature ground that is genuinely open

Not recommendations, just unclaimed territory that does not violate anything:

- Takeout importer to reconcile against the real cloud library (the big one)
- Per-month or per-album targeting beyond the current backfill stepper
- A "what will happen next" projection — the data is already in
  `db.throughput()` and `db.structural_rate()` but the UI barely uses it
- Better failure triage: `problems()` returns rows, the UI lists them flatly
- Notifications beyond the single webhook
- Settings redesign (see weaknesses above)

---

## 7. Working agreements from the last session

- **Verify, do not assume.** Every fix last session was confirmed by reverting it
  and watching the test fail, or by driving a running instance. The one thing
  that could not be verified — the mount guard against a real Unraid FUSE
  overlay — was called out as reasoned-not-observed rather than glossed.
- **`e564d1e` is unreviewed.** ~1,774 lines went onto `main` as a commit titled
  "update", outside the PR flow. A boot-blocking bug had been sitting in
  `db.py` since `fe2ca4e` partly because nobody had opened the
  old-database path. Treat `main` as less reviewed than the PR history suggests.
- Branch off `main`, never commit to it directly, and do not stack PRs on
  unmerged branches — that is what stranded #3.
