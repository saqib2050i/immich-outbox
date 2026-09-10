# Changelog

The number in `VERSION` is the only place a release is named. It is
`x.y.z`, and the parts are meant literally:

- **x** — an overhaul: the shape of the thing changed.
- **y** — a feature: it does something it could not do before.
- **z** — a fix: it does what it always claimed to.

Bump it in the same pull request as the change, so the published image and
the entry below can never disagree.

## 1.6.0

**A month now says whether its remainder is going anywhere.** "96 left" told
you nothing: outside every date window a file sits pending by design, and
reading that as a backlog is what made the whole ledger look like a queue.
Months distinguish what is **to send** from what is **only if you ask**, and
a test checks the split against what the feeder will actually claim, so the
row cannot promise a send that never happens.

**Individual files can be excluded from Library.** The case the month
buttons cannot serve: a screenshot or a photo of a document inside a window
you otherwise want. The list says why each file is where it is — "to send",
"only if you ask", "not being sent", "in the outbox", "backed up" — because
`pending` alone does not distinguish being on the way from going nowhere.
Files Immich no longer has are left out, since they can be neither sent nor
excluded.

The file list also survives the redraw that follows acting on it. Excluding
one file redraws the timeline, which rebuilds every month body; without
holding that state the list shut itself after each exclusion and had to be
reopened to do the next one.

## 1.5.0

**Library becomes the place months are managed.** First step of moving that
job off the Queue tab, where a view of the whole ledger was being presented
as a backlog.

- **Send a month again, including what is already backed up.** Nothing else
  could: a scan will not touch an existing row and confirmation is
  permanent, so a month deleted from Google Photos had no way back. Sending
  a file Google Photos already holds is not harmful — it hashes the upload
  and treats a match as already backed up — so this is a cost, not a risk.
  It stays behind an explicit flag and says what it will do first.
- **Exclude a month from Library**, beside the month it is about, instead of
  sweeping the backlog from another tab. Sending the month is the undo.
- The month row now says how many files are **not being sent**, which was
  the one figure that let a month look finished without having gone.

**Three send paths queued assets Immich no longer has.** `force_send`,
`force_send_month` and `dismiss_waiting` had no `missing_at` filter, while
the screens that count for them all do. So "Send the whole month (53)" acted
on 53 where the screen showed 33; `claim_batch` refuses them, so the extra
20 sat pending for good. The same ghost count as before, in the control
Library is being built around. All three now agree with what is on screen.

## 1.4.1

Queue page.

- **The badge is the backlog again.** 1.4.0 changed it to the outbox count
  on the belief that the outbox was all this tab listed. It is the *second*
  panel — the tab opens on the waiting backlog — so a badge of 34 sat over a
  list of 294.
- **The outbox figures are no longer capped by the list.** `count` and
  `bytes` were computed from `queue_contents()`, which returns at most 500
  rows. A 16 GB outbox of ordinary photos is a few thousand files, so Queue
  reported "500 in the outbox" while Overview read the real number off the
  disk and the two pages disagreed. The list is still capped — it now says
  so.
- **Fill progress no longer restarts partway through.** It was created and
  destroyed inside `_fetch_batch`, which runs once per claim, so a fill
  needing five claims showed the bar vanish and start again at zero five
  times. It now spans the whole fill, and "12 of 40 files in this batch" —
  counting a claim nobody asked about, in the units you were told were
  arbitrary — reads "files this cycle".
- **The tab and its first panel stopped sharing a name.** "Queue" over
  "Queue" over "In the outbox" is why "why is it called queue?" kept coming
  back; the first panel is now "Waiting to be sent".
- **The description matches the design.** It still said files "move a batch
  at a time", two designs out of date — they move in separate lanes for
  photos and videos, several at once, filling until the outbox is full.

## 1.4.0

**The settings page was rendering on every tab.** `.setwrap{display:grid}`
without `.tab-on` tied `[data-tab]{display:none}` on specificity and beat it
on source order, so 3,289px of settings form sat between the Overview cards
and the activity log — on all five other tabs. Scrolling down from Overview
landed you in *Include videos*. The Overview page is now 1,567px instead of
4,722px. Introduced in 1.2.0, by the fix that stopped the tab switcher
flattening that same grid.

**Three figures on the front page were wrong**, all the same way — counting
rows the ledger holds but Immich will never serve again:

- The pipeline bar was drawn 934 wide while the legend beneath it read 294,
  so most of the Waiting block stood for assets that are going nowhere. The
  two are two lines apart; the earlier fix corrected the number and missed
  the bar.
- **Still to send** claimed 13.2 GB when 9.9 GB was sendable. `pending_bytes`
  was the only count without the `missing_at IS NULL` filter the other
  queries carry. The Library tab's finish estimate reads the same figure, so
  it was over by the same 3.35 GB.
- The Queue badge counted 294 while the tab it labels listed 34. It now
  counts what that tab actually shows.

**The Phone card could not report how much was freed.** It read the item
count, and the app reports bytes now, so every successful run said "done".
It says "2.0 GB freed".

**Two additions to Overview**, both answering "why is nothing happening":

- The status sentence the server already writes — *"Waiting for the next
  check, up to 10 minutes away. 15.7 GB free in the outbox."* — was only
  ever visible on the Queue tab. It is the best single line the dashboard
  produces and it now leads the front page.
- Assets Immich no longer has are stated out loud rather than silently left
  out of every figure. Their absence was what made 2,700 motionless files
  look like a bug.

## 1.3.3

**Release signing never worked.** An unset GitHub secret arrives as an
*empty string*, not as an absent variable, so Kotlin's `?:` never fired and
the key password fell back to `""` rather than the store password. The build
died with `Given final block not properly padded` — which reads like a
corrupt keystore and sent the hunt in the wrong direction entirely.

Also: a mispasted keystore secret failed with `base64: invalid input` and
nothing else. The decode now strips whitespace, checks something decoded,
checks it looks like a keystore, and checks the password and alias open it —
each with a sentence naming the likely cause and the decoded byte count to
compare against the real file.

## 1.3.2

**The phone never woke up to check in.** Polling was scheduled with
`Handler.postDelayed`, which cannot wake a sleeping CPU: with the screen off
— the entire intended use — the callback simply waited until something else
woke the device. Measured on the Pixel: **no check-in in over 100 seconds on
a 60-second interval**, plugged in, process alive throughout. That is why it
only ever worked by picking the phone up and pressing *Check in now*.

Scheduling now goes through `AlarmManager` with a wakeup alarm, and the
check-in holds a CPU lock so the phone stays up for the run that follows.
Verified on the device: screen off and untouched, it woke, collected a
queued request, walked Google Photos, and reported back in 27 seconds.

`setAndAllowWhileIdle` rather than an exact alarm on purpose — exact alarms
need `SCHEDULE_EXACT_ALARM` from Android 12, and nothing here needs to
happen at a precise moment. In deep Doze, which needs the phone unplugged,
the system holds these to about one every nine minutes; a phone on a charger
never enters that state.

**A run could open a memories slideshow instead.** When the account picture
had not rendered yet, a positional fallback tapped the rightmost clickable
thing in the top corner — on the Photos home screen, the last memory card.
Removed: the picture carries a long, stable description ("Signed in as …
Account and settings."), and waiting for the real thing beats guessing.

Confirmed against the device that there is no API to use instead. Photos'
`FreeUpSpaceContentProvider` is exported but rejects every caller before
dispatching a method — even adb shell — and there is no deep-link activity
for the storage screen. The UI walk is the only route.

## 1.3.1

**The app reported the wrong version.** `Relay.kt` carried
`const val VERSION = "1.1.0"` while the build took its `versionName` from
the project's VERSION file, so a freshly installed 1.3.0 announced itself as
1.1.0 and the server offered it an update it already had, permanently. It
now asks Android what is installed, which cannot drift.

**The app could not find the button.** Two causes, both fixed:

- It looked for "free up space", and the menu entry is called **"Free up
  space on this device"**. It now knows the real path — account picture →
  that entry → the blue *Free up 29.80 MB* → *You freed up 29.80 MB* — and
  reads the figure off the screen rather than measuring free space.
- More importantly, it reported *"nothing readable"*, which was true: a
  phone on a shelf has its screen off, and a screen that is off draws no
  windows for an accessibility service to read. It now wakes the screen for
  the length of the run, and when it still sees nothing it says whether the
  screen was off, the phone was locked, or Google Photos simply never came
  to the front — three situations that need three different fixes and used
  to look identical.

**"Nothing to free up" is a success**, not a failure. It means the phone is
already clear of everything Google Photos has backed up, which is the state
the whole system is trying to reach.

The screen-matching now lives in `Labels.kt`, free of Android imports, with
unit tests pinned to strings read off the real phone — CI runs them. It is
the part of this app most likely to break without warning, and it was
previously buried where nothing could test it.

Adds the `WAKE_LOCK` permission: a normal one, granted at install without a
prompt, giving access to no data. The guarantee that the app cannot read or
delete a photo is unchanged.

## 1.3.0

**The relay serves the phone app.** Changing the companion used to mean a
cable and a laptop. Now CI builds it, bundles it into the image, and the
Pixel fetches it from `/app` — so an app change ships exactly like a server
change: push, `docker compose pull`, and the phone offers the update on its
next check-in.

- The app's version now comes from the same `VERSION` file as the server.
  It was hardcoded in `build.gradle.kts` and had already gone stale by a
  release. They speak a protocol to each other, so a pair whose numbers
  disagree is a pair nobody has tested.
- The app is told what the server is serving on every check-in, and says so
  on its own screen. It does not install anything: that would need
  `REQUEST_INSTALL_PACKAGES` — the permission to install other apps — and
  the short permission list is worth more than saving a tap. It opens the
  install page and lets Android's own installer do it.
- The dashboard shows which build is on the phone and which is on the
  server, and says when they differ.
- A server carrying no app build says so plainly rather than serving an
  empty download, which on a phone mid-install looks identical to a corrupt
  one.
- The install page and the download sit behind the session gate, so the
  open paths stay at three.

**One-time setup for seamless updates:** Android installs an update only
over the same signing key, and a CI runner generates a fresh debug key every
build. Add a keystore as a repository secret and updates become one tap —
see `companion/README.md`. Without it everything still works; the first
install is fine and the install page warns that updating needs the old copy
removed. The app build is not allowed to block a server release either way.

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
