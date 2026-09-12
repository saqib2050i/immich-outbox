package com.immichoutbox.companion

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Presses Google Photos' "Free up space", and nothing else.
 *
 * The whole app lives in this service because an enabled accessibility
 * service is already a long-lived process that Android starts at boot and
 * restarts if it dies. A separate foreground service to hold the polling
 * loop would add a permanent notification and a second thing to keep alive
 * for no gain: with accessibility switched off the app cannot do its job at
 * all, so there would be nothing worth polling for.
 *
 * What this service can see is limited by `accessibility_service_config.xml`
 * to Google Photos alone. It never reads a photo -- it reads button labels.
 *
 * It also never deletes one. It taps a button, and Google Photos removes
 * only what it has already verified as backed up. That distinction is the
 * whole safety story of the relay this belongs to: on the server, a file
 * disappearing from the outbox is the only evidence that a backup happened,
 * so nothing but Google Photos may be allowed to make one disappear.
 */
class FreeSpaceService : AccessibilityService() {

    private val worker = Executors.newSingleThreadExecutor()
    private val busy = AtomicBoolean(false)

    private lateinit var prefs: Prefs
    private lateinit var relay: Relay

    override fun onServiceConnected() {
        super.onServiceConnected()
        prefs = Prefs(this)
        relay = Relay(this, prefs)
        instance = this
        // Only when there is nothing to overwrite. The system reconnects an
        // accessibility service on an app update, on a reboot, and whenever
        // anything else reconfigures accessibility -- and this line used to
        // wipe the last real status every time, so "Last activity" read
        // "waiting to check in" on a phone that had checked in all night.
        if (prefs.lastStatus.isEmpty()) prefs.lastStatus = "Running. Waiting to check in."
        // Same reason: a reconnect used to drag the next check-in forward to
        // five seconds, so a flurry of them became a flurry of polls.
        if (!PollAlarm.pending(this)) scheduleNextPoll(5)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        // The alarm deliberately survives: the system restarts an enabled
        // accessibility service, and the next firing brings us back with it.
        worker.shutdownNow()
        super.onDestroy()
    }

    // The tree is read on demand rather than followed event by event, so
    // there is nothing to do here. The service still has to exist for the
    // system to keep us connected.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    // ---- the polling loop ----------------------------------------------

    private fun scheduleNextPoll(seconds: Int) = PollAlarm.schedule(this, seconds)

    /** Check in: on the alarm, or from the setup screen's button. */
    fun pollNow() {
        if (!busy.compareAndSet(false, true)) return

        // Hold the CPU for the whole check-in. The alarm woke the phone, but
        // nothing keeps it awake once the broadcast returns, and the poll and
        // any run that follows both happen on another thread afterwards.
        val cpu = holdCpu()
        worker.execute {
            var next = 1800
            try {
                next = checkIn()
            } finally {
                busy.set(false)
                scheduleNextPoll(next)
                try {
                    if (cpu?.isHeld == true) cpu.release()
                } catch (e: Exception) {
                    // An already-released lock is not worth failing over.
                }
            }
        }
    }

    private fun holdCpu(): android.os.PowerManager.WakeLock? = try {
        val pm = getSystemService(android.content.Context.POWER_SERVICE)
                as android.os.PowerManager
        // Generous, because a run plus a dwell can take several minutes.
        // It is released in a finally either way; the timeout is only there
        // so a crash cannot pin the CPU awake forever.
        pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "companion:poll")
            .apply { acquire(15 * 60 * 1000L) }
    } catch (e: Exception) {
        null
    }

    private fun checkIn(): Int {
        if (!prefs.configured) {
            prefs.lastStatus = "Not set up yet — add the relay address and pairing code."
            return 300
        }

        val instruction = relay.poll()
        if (instruction == null) {
            // lastStatus already carries why, set by Relay.
            return 300
        }

        prefs.latestVersion = instruction.latestVersion

        val outcome = when (instruction.action) {
            "free" -> { prefs.lastStatus = "Freeing space…"; freeUpSpace(instruction) }
            "look" -> { prefs.lastStatus = "Looking in on Google Photos…"
                        lookAtPhotos(instruction) }
            else -> null
        }
        if (outcome == null) {
            prefs.lastStatus = "Checked in. " + (instruction.reason.ifEmpty { "Nothing to do." })
            return instruction.nextPollSeconds
        }

        prefs.lastStatus = outcome.detail +
            (if (outcome.dwelledSeconds > 0) " Stayed ${outcome.dwelledSeconds}s." else "")
        relay.report(instruction.requestId, instruction.action, outcome.ok,
                     outcome.detail, outcome.items, outcome.freedBytes,
                     outcome.dwelledSeconds, outcome.backup)
        return instruction.nextPollSeconds
    }

    // ---- driving Google Photos -----------------------------------------

    private class Outcome(
        val ok: Boolean,
        val detail: String,
        val items: Int = 0,
        val freedBytes: Long = 0,
    ) {
        /** What Google Photos said about its own backup, if we got to see
         *  it. Attached after the walk, because the only screen that shows
         *  it is the one we return to on the way out. */
        var backup: Labels.Backup? = null

        /** Seconds actually spent standing in front of Google Photos. */
        var dwelledSeconds: Int = 0
    }

    /**
     * Open Google Photos, wait, and read what it says about itself.
     *
     * The cheap half of the job. It presses nothing, so there is no run to
     * go wrong and nothing to undo -- which is what makes it affordable on
     * a schedule, and what lets the server find out whether Photos has
     * finished before deciding to press anything.
     *
     * Opening the app is also the point, not just the means: a foreground
     * app escapes Doze and the standby bucket an app sinks into when nobody
     * opens it, which is why a shelf phone uploads nothing all day and
     * starts the moment it is picked up.
     */
    private fun lookAtPhotos(job: Relay.Instruction): Outcome {
        val launch = packageManager.getLaunchIntentForPackage(PHOTOS)
            ?: return Outcome(false, "Google Photos is not installed on this phone.")

        // Never zero. The panel takes a moment to draw, and reading a blank
        // screen is not the same as reading "nothing is backing up".
        val stay = maxOf(job.dwellSeconds, LOOK_SECONDS)
        val wake = wakeScreen(stay)
        try {
            sleep(1200)
            val state = screenState()
            if (state == LOCKED) {
                return Outcome(false,
                    "The phone is locked, and the companion cannot get past a lock " +
                    "screen. Turn the screen lock off on this phone.")
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(launch)
            sleep(2500)

            // settlePhotos finds no free-up screens to back out of here, so
            // it goes straight to reading and waiting -- which is the whole
            // of a look.
            val seen = settlePhotos(DEFAULT_LABELS, job, stay)
            val outcome = Outcome(seen != null, when {
                seen == null -> "Google Photos never came to the front, so there " +
                                "was nothing to read — ${state}."
                seen.active && seen.remaining > 0 ->
                    "Google Photos is backing up ${seen.remaining} item(s)." +
                    (if (seen.etaMinutes > 0) " About ${seen.etaMinutes} min left." else "")
                seen.active -> "Google Photos is backing up."
                else -> "Google Photos is not backing anything up."
            })
            outcome.backup = seen
            outcome.dwelledSeconds = stay
            return outcome
        } finally {
            try {
                if (wake?.isHeld == true) wake.release()
            } catch (e: Exception) {
                // Releasing an already-released lock is not worth failing over.
            }
        }
    }

    private fun freeUpSpace(job: Relay.Instruction): Outcome {
        val triggers = job.labels.ifEmpty { DEFAULT_LABELS }
        val confirmWords = job.confirmLabels.ifEmpty { DEFAULT_CONFIRM }

        val launch = packageManager.getLaunchIntentForPackage(PHOTOS)
            ?: return Outcome(false, "Google Photos is not installed on this phone.")

        // A phone on a shelf has its screen off, and a screen that is off
        // draws no windows -- there is literally nothing for an
        // accessibility service to read. This is the likeliest reason for a
        // run that reports seeing nothing at all.
        val wake = wakeScreen(job.dwellSeconds)
        try {
            sleep(1200)
            val state = screenState()
            if (state == LOCKED) {
                return Outcome(false,
                    "The phone is locked, and the companion cannot get past a lock " +
                    "screen. Turn the screen lock off on this phone, or unlock it " +
                    "before freeing space.")
            }

            val before = relay.freeBytes()
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(launch)

            val outcome = try {
                walkPhotos(triggers, confirmWords, before, state)
            } catch (e: Exception) {
                Outcome(false, "The run stopped with an error: ${e.message}")
            }
            // However the run went, do not walk away leaving Photos on a
            // screen the next run would misread -- and read its backup panel
            // while we are standing in front of it. See settlePhotos.
            outcome.backup = settlePhotos(triggers, job, job.dwellSeconds)
            outcome.dwelledSeconds = maxOf(job.dwellSeconds, 0)
            return outcome
        } finally {
            try {
                if (wake?.isHeld == true) wake.release()
            } catch (e: Exception) {
                // Releasing an already-released lock is not worth failing a run.
            }
        }
    }

    /**
     * Work the screens through to the end, whichever one we happen to be on.
     *
     * Written as a priority loop rather than a fixed sequence of steps. The
     * path is: the account picture, top right -> "Free up space on this
     * device" -> a blue "Free up 29.80 MB" -> "You freed up 29.80 MB". But
     * that path differs between Google Photos versions, and a rigid state
     * machine breaks the moment one screen is skipped or added. Asking on
     * every pass "what is in front of me, and what is the most finished
     * thing I can act on?" survives both.
     *
     * With one thing the priority loop cannot answer on its own: whether
     * the finished screen in front of it is *this* run's. Google Photos
     * resumes where it was left, so a run that ended on "You freed up
     * 29.80 MB" hands the next one that same screen on its first pass. Read
     * naively it is a success carrying a figure nothing earned, returned
     * without a button ever being pressed -- and since nothing was freed,
     * the outbox stays full while the dashboard reports a healthy phone.
     * So a finished screen only counts once this run has been somewhere,
     * and one that turns up before that is backed out of.
     */
    private fun walkPhotos(triggers: List<String>, confirms: List<String>,
                           before: Long, state: String): Outcome {
        var openedMenu = false
        // Has this run moved at all? A finished screen is only ours if it is.
        var navigated = false
        var pressed = false
        var sawAnything = false
        var escapes = 0
        var lastSeen = ""

        for (step in 0 until MAX_STEPS) {
            val nodes = visibleNodes()
            val texts = nodes.map { textOf(it) }.filter { it.isNotBlank() }
            if (texts.isNotEmpty()) {
                sawAnything = true
                lastSeen = texts.distinct().take(12).joinToString(" | ")
            }

            val freed = texts.firstNotNullOfOrNull { Labels.freedBytes(it) }
            val idle = texts.any { Labels.isNothingToDo(it) }

            // 1. Finished. "You freed up 29.80 MB" carries the exact figure,
            //    which beats diffing free space -- anything else on the
            //    phone writing a file mid-run would corrupt that. Believed
            //    only after we pressed the button that produces it.
            if (freed != null && pressed) {
                return Outcome(true, "Freed ${Labels.format(freed)} on the phone.",
                               0, freed)
            }

            // 2. Also finished, with nothing to do. This is a success: the
            //    phone is already clear of everything Google Photos has
            //    backed up, which is exactly the state we want it in. It
            //    only appears once the menu entry has been tapped, so
            //    seeing it before that means we are looking at the past.
            if (idle && navigated) {
                return Outcome(true,
                    "Nothing to free up — everything backed up is already off the phone.",
                    0, 0)
            }

            // Either of those, left over from last time. Back out of it and
            // walk the path properly rather than believing it.
            if ((freed != null || idle) && escapes < MAX_ESCAPES) {
                escapes++
                performGlobalAction(GLOBAL_ACTION_BACK)
                sleep(1400)
                continue
            }

            // 3. The button that does it, "Free up 29.80 MB". Checked before
            //    the menu entry because "free up" matches both and this one
            //    is further along.
            val action = nodes.firstOrNull { Labels.ACTION.containsMatchIn(textOf(it)) }
            if (action != null) {
                if (tap(action)) { pressed = true; sleep(3000); continue }
            }

            // 4. The menu entry, "Free up space on this device".
            val entry = match(nodes, triggers)
            if (entry != null && tap(entry)) { navigated = true; sleep(2200); continue }

            // 5. The account picture, which is where the entry lives. Found
            //    only by its description, never by position: it carries a
            //    long one ("Signed in as ... Account and settings."), and
            //    the previous positional fallback tapped whatever else sat
            //    in the top corner. On the Photos home screen that is the
            //    memories carousel, so a run would open a slideshow instead.
            //    Waiting for the real thing to appear beats guessing.
            if (!openedMenu) {
                val menu = match(nodes, MENU_LABELS)
                if (menu != null && tap(menu)) {
                    openedMenu = true
                    navigated = true
                    sleep(2000)
                    continue
                }
            }
            sleep(POLL_MS)
        }

        if (pressed) {
            // Pressed, and the confirmation screen never appeared or was
            // missed. Believe the disk rather than the screen.
            val freed = freedSince(before)
            return if (freed > 0)
                Outcome(true, "Freed ${Labels.format(freed)} on the phone.", 0, freed)
            else
                Outcome(true, "Pressed the button; nothing needed clearing.", 0, 0)
        }

        // The useful failure. Which of these two it is decides what to do
        // next, and they used to be indistinguishable.
        if (!sawAnything) {
            return Outcome(false,
                "Could not read the screen at all — ${state}. Google Photos never " +
                "came to the front. Android stops apps opening screens while they " +
                "are in the background, so this usually means the phone was asleep " +
                "or Photos was blocked from starting.")
        }
        if (escapes > 0 && !navigated) {
            // Better a run that says so than one that reports last run's
            // figure: this one is visibly red on the dashboard, and the
            // screen it could not leave is in the message.
            return Outcome(false,
                "Google Photos opened on the result of an earlier run and would not " +
                "go back, so nothing here could be trusted as this run's. " +
                "On screen: $lastSeen")
        }
        return Outcome(false,
            "Google Photos is open, but nothing matched ${triggers.joinToString(", ")}. " +
            "On screen: $lastSeen")
    }

    /**
     * Leave the phone where the next run can start from.
     *
     * Two things happen here, and both are about the run after this one.
     * Photos is walked back off its free-up screens, because it resumes
     * where it was left and the next run would otherwise open straight onto
     * this run's result. Then our own screen is brought forward, so the
     * phone rests on the status line rather than inside somebody else's
     * app -- which is also what a person picking it up wants to see.
     *
     * Bounded, and it gives up the moment Photos is no longer in front:
     * pressing Back past the home screen leaves Photos altogether, and
     * that is a fine place to stop too. Nothing in here may throw. Tidying
     * up must never turn a run that worked into a run that failed.
     *
     * It also happens to end on the one screen that says how far along
     * Google Photos' backup is, so it reads that on the way past -- and, if
     * the server asked for it, stands there a while first.
     */
    private fun settlePhotos(triggers: List<String>, job: Relay.Instruction,
                             stay: Int): Labels.Backup? {
        var seen: Labels.Backup? = null
        try {
            for (step in 0 until MAX_BACKS) {
                val root = rootInActiveWindow ?: break
                if (root.packageName?.toString() != PHOTOS) break
                val texts = visibleNodes().map { textOf(it) }.filter { it.isNotBlank() }
                if (!Labels.isFreeUpScreen(texts, triggers)) break
                if (!performGlobalAction(GLOBAL_ACTION_BACK)) break
                sleep(BACK_MS)
            }
            seen = readBackup(job.backupLabels)
            if (stay > 0) {
                prefs.lastStatus = "Waiting in Google Photos so it can back up…"
                dwell(stay, job.backupLabels)?.let { seen = it }
            }
            startActivity(Intent(this, MainActivity::class.java).addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
        } catch (e: Exception) {
            // Nothing here is worth failing a run over.
        }
        return seen
    }

    /**
     * Stand in front of Google Photos so its upload can get on with it.
     *
     * Not a trick: Photos asks for this itself, on the same panel this
     * reads -- "Keep the app open for faster backup". A foreground app is
     * not throttled by Doze or by the standby bucket an app sinks into when
     * nobody opens it, which is how a phone on a shelf backs up nothing for
     * fifteen hours and then starts the moment it is picked up.
     *
     * Re-read each time round rather than once at the end, because the
     * figure is the useful part and it moves while we watch. An active
     * reading always beats an inactive one: the panel takes a moment to
     * appear, and "no panel" a second after arriving means nothing.
     */
    private fun dwell(seconds: Int, marks: List<String>): Labels.Backup? {
        var best: Labels.Backup? = null
        val until = android.os.SystemClock.elapsedRealtime() + seconds * 1000L
        while (android.os.SystemClock.elapsedRealtime() < until) {
            val now = readBackup(marks)
            if (now != null && (now.active || best == null)) best = now
            sleep(DWELL_STEP_MS)
        }
        return best
    }

    /**
     * What Photos says about its own backup, or null if we are not looking
     * at Photos. Never treated as proof of anything -- see companion.py.
     */
    private fun readBackup(marks: List<String>): Labels.Backup? {
        val root = rootInActiveWindow ?: return null
        if (root.packageName?.toString() != PHOTOS) return null
        val texts = visibleNodes().map { textOf(it) }.filter { it.isNotBlank() }
        return Labels.backupState(texts, marks)
    }

    /** Turn the screen on for the length of the run, and any dwell after. */
    @Suppress("DEPRECATION")
    private fun wakeScreen(dwellSeconds: Int): android.os.PowerManager.WakeLock? = try {
        val pm = getSystemService(android.content.Context.POWER_SERVICE)
                as android.os.PowerManager
        pm.newWakeLock(
            android.os.PowerManager.SCREEN_BRIGHT_WAKE_LOCK or
            android.os.PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "companion:freeup").apply { acquire((180L + dwellSeconds) * 1000L) }
    } catch (e: Exception) {
        null
    }

    /**
     * Enough about the screen to explain a run that saw nothing, without
     * widening what this service is allowed to look at.
     */
    private fun screenState(): String {
        val pm = getSystemService(android.content.Context.POWER_SERVICE)
                as? android.os.PowerManager
        val km = getSystemService(android.content.Context.KEYGUARD_SERVICE)
                as? android.app.KeyguardManager
        return when {
            pm?.isInteractive == false -> "the screen would not come on"
            km?.isKeyguardLocked == true -> LOCKED
            else -> "the screen was on and unlocked"
        }
    }

    private fun freedSince(before: Long): Long {
        val after = relay.freeBytes()
        return if (before > 0 && after > before) after - before else 0
    }

    // ---- reading the screen ---------------------------------------------

    private fun visibleNodes(): List<AccessibilityNodeInfo> {
        val root = rootInActiveWindow ?: return emptyList()
        val out = ArrayList<AccessibilityNodeInfo>()
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        while (queue.isNotEmpty() && out.size < MAX_NODES) {
            val node = queue.removeFirst()
            out.add(node)
            for (i in 0 until node.childCount) {
                node.getChild(i)?.let { queue.add(it) }
            }
        }
        return out
    }

    private fun textOf(node: AccessibilityNodeInfo): String {
        val text = node.text?.toString().orEmpty()
        val desc = node.contentDescription?.toString().orEmpty()
        return if (text.isNotBlank()) text else desc
    }

    private fun boundsOf(node: AccessibilityNodeInfo): Rect {
        val r = Rect()
        node.getBoundsInScreen(r)
        return r
    }

    /** The first node whose label contains one of these words. */
    private fun match(nodes: List<AccessibilityNodeInfo>,
                      words: List<String>): AccessibilityNodeInfo? {
        for (node in nodes) {
            val label = textOf(node).lowercase()
            if (label.isBlank()) continue
            if (words.any { label.contains(it.lowercase()) }) return node
        }
        return null
    }

    /**
     * Tap a node, or the nearest ancestor that is actually clickable.
     *
     * Refuses an ancestor that covers most of the screen. Text on these
     * screens often sits inside a clickable page-sized container -- the
     * heading "Your device storage is 30% full, free up space" is one --
     * and walking up to that taps the page rather than a button, which does
     * something arbitrary rather than nothing.
     */
    private fun tap(node: AccessibilityNodeInfo): Boolean {
        val screen = rootInActiveWindow?.let { boundsOf(it) }
        var target: AccessibilityNodeInfo? = node
        var hops = 0
        while (target != null && hops < 6) {
            if (target.isClickable && !coversMostOf(boundsOf(target), screen)) {
                return target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            }
            target = target.parent
            hops++
        }
        return false
    }

    private fun coversMostOf(box: Rect, screen: Rect?): Boolean {
        if (screen == null || screen.height() <= 0) return false
        return box.height() > screen.height() * 0.6
    }

    private fun sleep(ms: Long) = try {
        Thread.sleep(ms)
    } catch (e: InterruptedException) {
        Thread.currentThread().interrupt()
    }

    companion object {
        const val PHOTOS = "com.google.android.apps.photos"

        // The label matching lives in Labels, where it can be unit-tested
        // against the strings actually on the phone. See LabelsTest.
        val DEFAULT_LABELS = Labels.ENTRY
        val DEFAULT_CONFIRM = Labels.CONFIRM
        val MENU_LABELS = Labels.MENU

        private const val LOCKED = "the phone was locked"

        // Slower than it looks: the Pixel 1 is a 2016 phone and Google
        // Photos is not quick on it.
        private const val MAX_STEPS = 45
        private const val MAX_NODES = 600
        private const val POLL_MS = 700L

        // Backing out of a stale result mid-walk. Three is already more
        // screens than the path has; past that, Back is not working and
        // saying so beats pressing it forever.
        private const val MAX_ESCAPES = 3

        // Backing out at the end. The path is three screens deep, and the
        // loop stops early the moment the free-up screens are gone.
        private const val MAX_BACKS = 6
        private const val BACK_MS = 900L

        // How often to re-read the backup panel while standing in front of
        // it. Often enough to catch the figure moving, rare enough that
        // walking the tree is not the thing keeping the phone busy.
        private const val DWELL_STEP_MS = 5000L

        // The shortest a look may be. Long enough for the panel to draw and
        // be read twice; short enough to run every half hour without the
        // screen being on in any meaningful sense.
        private const val LOOK_SECONDS = 12

        @Volatile
        var instance: FreeSpaceService? = null
            private set

    }
}
