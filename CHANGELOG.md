# Changelog

The number in `VERSION` is the only place a release is named. It is
`x.y.z`, and the parts are meant literally:

- **x** — an overhaul: the shape of the thing changed.
- **y** — a feature: it does something it could not do before.
- **z** — a fix: it does what it always claimed to.

Bump it in the same pull request as the change, so the published image and
the entry below can never disagree.

## 2.8.1

**A cross beside a file the pipeline handles.** Tracing an undated file
showed two verdicts: *Immich's original — would fall back to upload time*
with a cross, and *In the outbox — dated by its modification time* with a
tick, one line apart on the same photo. Both true, and the first read as a
fault. Immich's copy is fetched here to a temporary file, so it has no
delivered modification time to be judged on; that verdict is about its
metadata alone, and it now says so and points at the copy the phone
actually receives.

## 2.8.0

**The modification-time fallback is only ever right at UTC, and most of
this library is not.** An mtime is an instant carrying no zone, and Google
Photos displays it as UTC — `Snapchat-618209934.jpg` came back labelled
`GMT+00:00`. So a photo taken at 11:30 in Karachi and riding on its mtime
shows 06:30, on both that screen and Immich's, and the two agreeing is not
evidence either is right. It is the same number twice.

Which retracts, again, half of what 2.7.3 concluded. That file was called
correctly dated on the strength of Immich and Google Photos matching. They
match because both are showing the same instant. The date is right; the
time of day is five hours out.

The verdict now separates the three readings of a file riding on its mtime:
right at UTC, *N* hours out with the zone named, or zone unknown and so not
answerable. Where the photo was taken before dawn, it says the day is wrong
too — local 02:00 at +05:00 is 21:00 the day before.

**Two new settings say where its owner was living**, because nothing in a
file without a zone or coordinates can. `assume_zone_before` and
`assume_zone_offset` — 2026-03-04 and +05:00 here, the date the move
happened. The precedence is strict and each step outranks the next: the
file's own `OffsetTimeOriginal`, honoured whatever the date; coordinates,
since a photo taken on a trip says so itself; anything else Immich holds;
then the rule. Blank either half and nothing is assumed.

`zone_source()` also tells apart two things that were one string before.
Immich derives a zone from GPS when the file has no offset tag and reports
UTC when it has neither, so `Asia/Karachi` on a photo with coordinates is a
finding and `UTC+0` on one without is Immich saying it does not know.

## 2.7.3

**The verdict was wrong about a file that was fine.**
`Snapchat-618209934.jpg` carries no date tag of any kind — nothing but
`Software: Picasa` — and the trace said *would fall back to upload time*.
Google Photos had already dated it **Jan 1 2024, 6:30 AM**, correctly, to
the same second as the outbox copy's modification time.

`feeder.stamp_capture_time()` has always set every delivered file's mtime
to Immich's capture instant, precisely because Google Photos falls back to
one when there is no tag; Syncthing preserves it to the phone. That is what
it was built for and it works. The verdict simply did not look.

It looks now. A file with no usable tag whose delivered copy carries the
capture instant as its mtime is reported as *dated by its modification
time, not its metadata* — dated, with the mechanism named, and with its two
specific weaknesses stated: an mtime does not survive anything that
rewrites the file, and it carries no zone, so what Google Photos displays
for a photo taken outside UTC is not settled by anything here. The
confirmed case is UTC+0, where the two readings cannot be told apart.

Only a copy **read in place** is credited with its mtime. Immich's is
fetched to a temp file moments earlier and is always today.

**And only a tag that is absent.** A blank one is not rescued, which was
measured rather than reasoned: two files from this library took the same
route with the same correct modification time and landed nine hundred days
apart — `Snapchat-618209934.jpg`, tag absent, on Jan 1 2024 6:30 AM, and
`PXL_20240101_062038690.jpg`, tag present and empty, on today at 12:33 PM.
A blank tag evidently reads to the media scanner as metadata it cannot
parse, and it never reaches the mtime.

That makes blank-versus-missing the thing that *predicts* where a photo
lands rather than just describing what is in it, and it narrows the fault:
files with no date tag at all fall through to the mtime, and the one
observed doing so landed correctly — though that file was UTC+0, where the
instant and the wall clock are the same number, so "no tag is fine" is not
yet established for a photo taken anywhere else. The report
now says, on a blank-tag file, why the correct `FileModifyDate` two rows
below the verdict does not save it.

This also retracts a correction made in 2.7.1. `needs_date_fix()` once said
a dateless file is safe because its modification time carries the date;
that was called a disproven premise and removed. It is true, it is
implemented, and it is back — with the caveat that a weaker carrier is not
no carrier, and treating it as none overstates how much of this library is
actually mis-dated.

## 2.7.2

**"Send it, then trace" had nine ways to do nothing and reported none of
them.** `forced` bypasses the date window and nothing else, so `claim_batch`
still excludes a confirmed asset, a motion component, video when video is
off, anything over the size ceiling and anything a date mismatch holds
back; `top_up` declines when the relay is paused, when the outbox is not
mounted, and when the cap is reached. And a file already in the outbox
skipped the send branch entirely. Each of those returned `ok: True`, and
the page never read that field anyway — so the button was pressed, nothing
happened, and the report said nothing about it. That is the exact failure
this tool was built to end, committed by the tool itself.

Each condition is now checked by name and the reason is the first line of
the report, above everything else, because it explains what is underneath
it.

**It also called a failed download a success.** `outbox_name` is recorded
when the transfer is set up and survives the download failing, so a file
that never arrived still carries a name — and the check asked for the name
rather than the file. Pressing send with Immich unreachable reported *Sent.
It is in the outbox as never_sent.jpg* over an empty outbox. It looks at
the file on disk now.

Nothing was ever at risk from that row: confirmation requires
`state='queued' AND seen_on_phone=1`, and a failed asset is neither. The
ledger was right; the report was lying about it.

**"Send it, then trace" will now re-send a confirmed file.** It refused,
on the strength of invariant 4 — already-confirmed assets are never
re-sent, because that duplicates the photo. But the reason is narrower than
the rule: duplication happens when the *bytes* change. Google Photos
matches an upload against what it already holds, so a byte-identical file
is recognised rather than added, and Free up space clears it again on its
next run.

Which matters, because a wrong date is noticed *in Google Photos*, months
later, by which time the outbox copy is long gone. Refusing meant there was
no way to see what actually left the building for exactly the files worth
asking about. It refuses only when the file would be altered on the way out
— `fix_dates` together with a real date mismatch — which is the one case
where it genuinely would arrive as a second photo.

Nothing automatic changed: `claim_batch` still excludes `state='confirmed'`
outright, and a test says so. This is one button, in Tools, aimed at one
named file.

**And a name in the ledger is not a file in the outbox.** A confirmed asset
keeps its `outbox_name` for good — the file left the outbox *because*
Google Photos cleared it off the phone, which is how it was confirmed — so
reading the ledger announced "already in the outbox" about a file that
demonstrably was not, while the outbox section three lines below correctly
said it was gone. And it did so on exactly the kind of file somebody
traces: one found in Google Photos wearing the wrong date, which is how it
came to anyone's attention in the first place. That file now gets the
reason it deserves — verified, cleared, never re-sent, and only Immich's
original left to read.

Also: `_send_now` was calling `reconcile()` a second time to read a byte
count. That is the function that confirms assets from their absence, and it
is not a way to ask how full the outbox is. It uses `list_outbox()`.

## 2.7.1

The first trace of a real broken file, `PXL_20240101_062038690.jpg`, and
three things it showed.

**It said the same paragraph twice** — once in the running list of findings
and again as its own card. The verdict is tagged now and the page draws it
in one place. It stays in the findings list, which is what anything reading
the API gets and the thing that must never come back empty.

**It invented a difference between two identical files.** Immich's copy is
fetched to a temporary file, so its modification time is when the download
happened — today, always. That sat beside the outbox copy's real mtime,
marked as differing, directly underneath the finding saying the two are
byte for byte identical. A downloaded copy now reports that tag as not
meaningful, and the comparison skips it.

**It called Immich's record "the file claimed".** That line shows
`exifInfo.dateTimeOriginal`, which is a UTC instant despite being named
after the EXIF tag, and which on a Takeout import came from the sidecar —
so it holds a date for a file that carries none. Labelling it as the file's
own claim was the exact confusion the whole tool exists to end.

Also in this release, written before the merge and missed by it:

- **Immich holding a date the file does not is stated out loud**, along
  with the reason the Problems tab shows nothing: the mismatch figures
  compare `fileCreatedAt` against `exifInfo.dateTimeOriginal`, and on a
  Takeout import both were filled from the same sidecar, so they agree. A
  library of undated files reads as zero there. `needs_date_fix()` also
  explains which fault it can and cannot see.
- Checked against the live Immich response, which carries three traps in
  one payload: `localDateTime` is the wall clock with a misleading `Z`,
  `exifInfo.dateTimeOriginal` is a UTC instant despite its name, and
  `make`/`model` arrive as `""` rather than null. A file with the instant
  written into `DateTimeOriginal` is reported as five hours early, so the
  detector catches that mistake being made.

The real file turned out to be blanked all the way through — not just
`DateTimeOriginal` but `CreateDate`, `ModifyDate`, both offsets, `Make`,
`Model` and `Software`, every one present and empty. So there is no spare
date to recover from and **no zone in the file at all**: the offset has to
come from Immich's `timeZone`, which matters for what comes next.

Still detection only. Nothing is written to any file.

## 2.7.0

**The trace says whether Google Photos will date a file, or file it under
the upload.** Many photos in this library carry `DateTimeOriginal` present
and *empty* — the tag is in the file, its twenty bytes blank. Immich shows
the right date because it holds one of its own, put there by a Google
Takeout sidecar at import; Google Photos reads the file, finds nothing, and
files the photo under today. Confirmed on
`PXL_20240105_034733992.jpg`, which landed on the upload date despite a
perfectly good date sitting in its own filename, which Google did not use.

The tool could not see it. `_dates()` dropped anything falsy one line
before the report was assembled, so a blanked tag came back identical to a
tag that was never there, and the dashboard drew the same em-dash for both.
A file with the exact fault produced a clean-looking report. So:

- **Present-but-empty is now its own state**, apart from missing, and the
  two are never merged — same consequence, different causes. It covers all
  three shapes real files use: a NUL-filled tag, a space-filled one, and an
  mp4 whose `creation_time` is zero and reads `0000:00:00 00:00:00`.
- **Every copy gets a verdict**, in those words: *would be dated correctly
  by Google*, or *would fall back to upload time*, with the reason and the
  tag it was decided by. Both copies get one, because the answer is allowed
  to differ between them — which is the whole point of stamping one on its
  way past.
- **Videos are reported, not skipped.** They are not the fault here, and a
  tool silent about them cannot be used to prove that. A video is judged by
  QuickTime's `CreateDate`, which is UTC by specification, where a still is
  judged by `DateTimeOriginal`, which is local time with no zone at all.
- **What Immich holds is shown beside what the file holds**, fetched per
  trace: `localDateTime`, `fileCreatedAt` and the zone. The gap between the
  two columns *is* the fault, and it was previously invisible.
- Tags are read group-qualified now. Without that, exiftool returns
  `EXIF:DateTimeOriginal` and `XMP:DateTimeOriginal` under one name and the
  second quietly wins — which would report a date in the tag Google reads
  when the value came from one it does not.

Detection only. Nothing is written to any file, and the correction itself
is the next step.

## 2.6.0

**Tools can trace one file from Immich to the phone and say where it
changes.** Built after five hours went missing from a library and there was
no way to tell whether the relay had done it: Immich's copy needed an API
call, the outbox copy needed a shell on the server, and nothing could put
the two side by side. The answer took an afternoon, a USB cable and a
hand-written EXIF parser. It should have taken one text box.

- **Paste a filename, press Trace.** It fetches Immich's original to a
  temporary file, reads it, throws it away, reads the outbox copy, and shows
  the date tags from both in one table with the differences marked.
- **The phone's copy is confirmed, not read.** The companion declares no
  storage permission — that is what makes "it cannot delete a photo" an
  Android guarantee — and reading a photo's metadata would need exactly
  that. Syncthing hashes every block it transfers, so a device it lists as
  holding the file has a byte-identical copy, which is a stronger statement
  than a re-read.
- **It does not assume a mismatch.** Cameras disagree about which clock they
  name a file by: the Pixel names in UTC and records the zone separately, so
  a `PXL_` name sitting exactly one offset behind `DateTimeOriginal` is the
  file being *right*. Older phones and cameras wrote the local clock into the
  name instead. Both readings are checked and neither is asserted, so only a
  gap that *neither* explains is called a fault — and a file carrying no zone
  at all is reported as unverifiable rather than accused.
- **It names what is wrong rather than showing numbers.** Bytes that changed
  while date rewriting was off are called out against invariant 2a, with the
  tags that differ. exiftool's own `Warning` tags are surfaced, because
  structural damage appears there and nowhere else.
- **A name that matches nothing says so, with near misses.** That is the
  likeliest thing to happen at this box and it used to be indistinguishable
  from a file with no problems. A pasted path finds the file too.
- **A report is never empty.** A silent result cannot be told apart from one
  nobody computed, which is the whole failure this exists to stop.
- **Send it, then trace** forces a single asset through first, for a file
  that has never been sent and so has no second copy to compare.

Nothing here writes to a file or moves an asset, unless you press the
second button.

## 2.5.0

**The phone can look at Google Photos without pressing anything, and the
dashboard can finally see that it did.**

- **A look is its own instruction.** Until now the only thing the server
  could ask for was a free-up, and reading Photos' backup panel rode along
  inside it. So the dwell had no schedule of its own: it happened at most
  once per cooldown, and *never* when the outbox was empty — which is
  exactly when Photos most needs opening. A look presses nothing, takes
  seconds, and has its own interval (`companion_watch_minutes`, 30).
- **It looks before it presses.** With `companion_wait_for_backup` on (the
  default), a free-up is held back while Photos says it is still uploading.
  Pressing mid-upload clears whatever it has got through and leaves the
  rest, which wakes the phone for a fraction of the job — and makes the
  leftovers meaningless, since they could be unbacked files or simply the
  next ones in Photos' queue.
- **Which turns those leftovers into a finding.** When a free-up runs while
  Photos claims to have finished, everything on the phone should go. Files
  still in the outbox ten minutes later are files the phone is holding that
  Google Photos has not taken, whatever its screen said. Measured by
  confirmations rather than the outbox count, because `top_up()` refills on
  its own cycle. Reported, not acted on — and only alerted once the same
  pile has survived several free-ups, because Photos' media scanner lags
  Syncthing and one remainder proves nothing.
- **The phone says what it did and what it understands.** Reports now carry
  `dwelled_seconds` and which action ran; check-ins carry a `features` list.
  Whether a dwell had happened at all was previously answerable only by
  reading the phone's wake locks over a cable, and whether a phone was new
  enough to obey an instruction was a guess from its version string.
- **The Phone card shows all of it** — dwell setting and whether this phone
  understands it, when the next free-up is due, how long the last run stayed
  in Photos, and anything held back. Plus a **Check Google Photos** button
  beside *Free up space now*: the manual look, with no cooldown and nothing
  to undo.
- **Two service fixes.** `onServiceConnected` overwrote the last real status
  with *"Running. Waiting to check in."* on every reconnect — an app update,
  a reboot, or anything else reconfiguring accessibility — so a phone that
  had checked in all night reported that it never had. It also rescheduled
  the next poll to five seconds each time, turning a flurry of reconnects
  into a flurry of polls. Both now only happen when there is nothing
  already there.

## 2.4.1

**Library stops collapsing under you while a month is sending.** Reported
from production: press "Send the whole month" and the page you are reading
becomes unusable for as long as the send lasts.

- **A figure change no longer rebuilds the timeline.** The feeder writes to
  the ledger once per file and every write pushes an SSE event, so the
  timeline redrew several times a second during a send. Each redraw replaced
  every `<details>`, which slammed the open month shut — and restoring
  `open` fired `toggle`, which refetched the body. The count you were
  watching now simply changes: no element is replaced, so nothing collapses,
  nothing is refetched and nothing you had scrolled to moves. Only a change
  in *which* months exist rebuilds anything, and that happens when a scan
  finds something new.
- **The month body cache is no longer emptied on every redraw.** It existed
  so reopening a month would not flash *Loading…*, and it was cleared on
  every change — which is precisely when it was needed.
- **The outbox list gets the same guard.** Milder, because selection is held
  outside the DOM and survives, but it was rebuilt on every event too and
  threw away the scroll position of whoever was reading it.
- An open month's body is deliberately left as it was. It is a panel you
  opened and may be part-way through clicking; replacing its contents under
  the cursor is the same rudeness at a smaller scale. It reloads when you
  collapse and reopen it, or when you press something in it.

## 2.4.0

**The companion stops going quiet, and starts saying what Google Photos is
doing.** Diagnosed from a live pipeline that moved nothing for fifteen
hours while every panel on the dashboard looked healthy.

- **A free-up is asked for whenever the outbox is holding anything.** The
  old rule was "the outbox is full *and* work is queued behind it", on the
  reasoning that freeing space buys nothing if nothing will refill. True for
  throughput, false for confirmation: a file in the outbox is not backed up
  until it disappears, and only a free-up makes it disappear. So once a
  library was fully queued there was nothing behind the outbox, the rule
  declined, and the last batch of every run sat on the phone until Smart
  Storage's thirty-day clock reached it. Measured: 1,518 files, 15.3 GB,
  twelve hours, not one request made.
- **And it is asked for on the server's own cycle.** The decision used to
  live inside the phone's check-in, so it was evaluated *by the phone
  asking* — and `request()` is what writes the log line. A quiet phone
  therefore produced no entry of any kind, and an absence is the worst thing
  to have to diagnose from. `feeder.housekeeping()` decides now; the phone
  only collects. The one judgement left at poll time is the battery, which
  is the only fact the phone knows and the server does not.
- **"Warn if silent for" is in minutes, and defaults to 60.** It was twelve
  hours, set when the phone checked in twice an hour. Since 2.2.0 a phone on
  a charger checks in every minute, so twelve hours was 720 missed check-ins
  before anybody was told.
- **The phone can stay in Google Photos after a run**, off by default and
  switched from the server. Photos uploads far faster in the foreground, and
  says so itself on the same screen: *"Keep the app open for faster backup"*.
  Doze and the standby bucket an app sinks into when nobody opens it are why
  a shelf phone backs up nothing all day and then starts the moment it is
  picked up.
- **And while it is there, it reads how far along the backup is** —
  *"Backing up 250 photos"*, *"2 hours, 26 min remaining"* — and reports the
  count, the estimate and the raw line. The dashboard shows it; a new alert
  fires when the count has not moved for three runs, because a slow backup
  and a stopped one are otherwise identical from the server. The labels are
  a setting, like the button labels already were.
- **None of it confirms anything.** What Photos says about its own backup
  changes what the dashboard shows and when the server bothers asking. It
  never changes an asset's state. Confirmation stays in `feeder.reconcile()`,
  derived from files that are no longer on disk — a string scraped off
  somebody else's screen that could mark an asset backed up would forge the
  only proof this system has, and a renamed label would do it silently.

## 2.3.0

**The dashboard is rebuilt to look like something from this decade, and to
work on a phone.** No feature moved, was added, or was taken away — every
control is where it was. This is the stylesheet and the rows that would not
fit.

- **Sans for prose, mono for values.** The whole page was set in a monospace
  face, which made every sentence read like a log line. Mono now earns its
  place only where digits have to line up or a string will be copied: sizes,
  counts, times, filenames, paths.
- **Dark first, with a real light mode.** Both are defined properly rather
  than one being an override of the other, and the browser chrome follows
  the scheme instead of a single hard-coded colour.
- **It fits a 390px screen.** It did not: the timeline alone ran 274px off
  the right edge and took the page's horizontal scrollbar with it, because a
  year's figures are one unbreakable 400px string. Those now drop to their
  own line and wrap. So do a category's name, a file's size, and a queued
  file's details — each of which was either clipped, crushed to one word per
  line, or hidden outright. Controls are thumb-sized rather than mouse-sized.
- **The tab bar is one scrolling strip** rather than three wrapped rows.
- **Two layout bugs that predate this.** `summary::before` is itself a grid
  item, so both timeline rows were a column short: the year's figures were
  pushed onto a second row and into column one, and an `auto` column sizes
  to its widest item — which is why the year label sat in the middle of the
  row. The month's "worth upgrading" tag wrapped the same way, hard against
  the left margin. And the disclosure triangles were U+25B8, which the mono
  stack had and a system sans stack does not: they are drawn now, not typed.
- **A tab's panels stay hidden.** `[data-tab]{display:none}` is one
  attribute, so any single class setting `display` ties it and wins on
  source order. `.grid{display:grid}` did exactly that during this rebuild
  and put the three Overview cards on Library. It is `:not(.tab-on)` now,
  and a test refuses the weaker form.
- The sign-in page is on the same palette, which it never was.

## 2.2.0

**The phone stops reading last run's screen, and stops waiting half an hour
to be told anything.**

- **A run leaves Google Photos where the next run can start from.** It used
  to walk away on "You freed up 29.80 MB", and Photos resumes where it was
  left — so the next run opened straight onto that screen, read it as its
  own result, and returned a figure nothing had earned without pressing a
  button. Nothing was freed, so the outbox stayed full behind a dashboard
  reporting a healthy phone. A run now backs out to Photos' own screen and
  brings the companion forward, so the phone rests on its status line.
- **And it refuses to believe a finished screen it did not arrive at.** The
  reset above is the fix; this is what survives the reset failing. A result
  screen counts only once the button has been pressed *this* run, a
  "nothing to free up" only once the menu entry has been tapped, and one
  that turns up before either is backed out of. If it cannot be escaped the
  run says so, in red, quoting the screen — which beats a green tick over a
  stalled pipeline.
- **A phone on its charger checks in every minute.** The phone dials out and
  the relay never dials in, so the check-in interval *is* how long "Free up
  now" waits, and it was 30 minutes. That interval is paid for out of the
  phone's battery, so it is now two settings split on the one axis that
  matters: a minute while charging, the old half hour while not. The shelf
  phone this was built for is never off its cable. Switched off entirely,
  the phone keeps the slow interval — being woken every minute to be told
  there is nothing to do is the interval spent on nothing.

## 2.1.0

**Library is laid out as the control surface it became.** It was built as a
read-only timeline and inherited the job of managing months without the
layout changing to suit.

- **Months lead the page.** The panel where sending happens sat third,
  behind two panels of reference material — about three screens down, and
  every year collapsed, so the first control was 3,814px from the top. It is
  2,712px now and the first thing on the tab.
- **A summary says what is outstanding** without expanding anything:
  *200 to send · 33 in the outbox · 95 only if you ask*. Every year starts
  collapsed, so that total used to be two clicks and a scroll away.
- **The newest year holding something to send opens by itself**, once. A
  year opened or closed by hand disables that, so the default never fights a
  deliberate choice.
- **Each month has a progress bar**, as years already did — backed up, in
  the outbox, going out, in pipeline order. What is left unpainted is
  resting or excluded, so a nearly empty bar means nothing is going to
  happen in that month without asking.

## 2.0.0

**One rule decides what sends by itself.** Everything from the cut-off date
onwards; everything older waits to be asked for in Library, a month or a
file at a time. The backfill window — a start/end range stepped forward by
hand — is gone. Library already lists every month with what is left in it,
so the window was a second place for the same decision to live, kept in
step manually.

**The Queue tab is about the queue again.** It showed the entire ledger by
month and called it a backlog, which is what made twelve thousand untouched
photos look like work outstanding. It now shows what is moving, what is in
the outbox, and files whose dates need correcting. The month view is
Library's.

**The Dismissed panel is gone.** Excluded files show as "not being sent"
against their month, where they can be sent again — which is where the
decision was made. Tools keeps one bulk undo for reversing a sweep.

Also gone: the "point the backfill window here" button, the Previous/Next
month stepper, and the whole-backlog dismiss buttons, none of which have a
job any more.

**Upgrading.** Stored `cfg_backfill_*` values are ignored rather than
migrated — `settings.load()` reads only the keys it knows. Anything the
backfill window was releasing stops going automatically and appears in
Library as "only if you ask", where sending the month starts it again.

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
