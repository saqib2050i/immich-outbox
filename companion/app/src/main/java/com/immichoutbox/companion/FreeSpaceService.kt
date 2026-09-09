package com.immichoutbox.companion

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
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

    private val handler = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val busy = AtomicBoolean(false)

    private lateinit var prefs: Prefs
    private lateinit var relay: Relay

    override fun onServiceConnected() {
        super.onServiceConnected()
        prefs = Prefs(this)
        relay = Relay(this, prefs)
        instance = this
        prefs.lastStatus = "Running. Waiting to check in."
        scheduleNextPoll(5)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        handler.removeCallbacksAndMessages(null)
        worker.shutdownNow()
        super.onDestroy()
    }

    // The tree is read on demand rather than followed event by event, so
    // there is nothing to do here. The service still has to exist for the
    // system to keep us connected.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    // ---- the polling loop ----------------------------------------------

    private fun scheduleNextPoll(seconds: Int) {
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed({ pollNow() }, seconds * 1000L)
    }

    /** Check in out of band -- the setup screen's "Check in now" button. */
    fun pollNow() {
        if (!busy.compareAndSet(false, true)) return
        worker.execute {
            var next = 1800
            try {
                next = checkIn()
            } finally {
                busy.set(false)
                handler.post { scheduleNextPoll(next) }
            }
        }
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

        if (!instruction.freeSpace) {
            prefs.lastStatus = "Checked in. " + (instruction.reason.ifEmpty { "Nothing to do." })
            return instruction.nextPollSeconds
        }

        prefs.lastStatus = "Freeing space…"
        val outcome = freeUpSpace(instruction.labels, instruction.confirmLabels)
        prefs.lastStatus = outcome.detail
        relay.report(instruction.requestId, outcome.ok, outcome.detail,
                     outcome.items, outcome.freedBytes)
        return instruction.nextPollSeconds
    }

    // ---- driving Google Photos -----------------------------------------

    private class Outcome(
        val ok: Boolean,
        val detail: String,
        val items: Int = 0,
        val freedBytes: Long = 0,
    )

    private fun freeUpSpace(labels: List<String>, confirms: List<String>): Outcome {
        val triggers = labels.ifEmpty { DEFAULT_LABELS }
        val confirmWords = confirms.ifEmpty { DEFAULT_CONFIRM }

        val launch = packageManager.getLaunchIntentForPackage(PHOTOS)
            ?: return Outcome(false, "Google Photos is not installed on this phone.")

        // A phone on a shelf has its screen off, and a screen that is off
        // draws no windows -- there is literally nothing for an
        // accessibility service to read. This is the likeliest reason for a
        // run that reports seeing nothing at all.
        val wake = wakeScreen()
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
            launch.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(launch)

            return walkPhotos(triggers, confirmWords, before, state)
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
     */
    private fun walkPhotos(triggers: List<String>, confirms: List<String>,
                           before: Long, state: String): Outcome {
        var openedMenu = false
        var pressed = false
        var sawAnything = false
        var lastSeen = ""

        for (step in 0 until MAX_STEPS) {
            val nodes = visibleNodes()
            val texts = nodes.map { textOf(it) }.filter { it.isNotBlank() }
            if (texts.isNotEmpty()) {
                sawAnything = true
                lastSeen = texts.distinct().take(12).joinToString(" | ")
            }

            // 1. Finished. "You freed up 29.80 MB" carries the exact figure,
            //    which beats diffing free space -- anything else on the
            //    phone writing a file mid-run would corrupt that.
            for (text in texts) {
                Labels.freedBytes(text)?.let { freed ->
                    return Outcome(true, "Freed ${Labels.format(freed)} on the phone.",
                                   0, freed)
                }
            }

            // 2. Also finished, with nothing to do. This is a success: the
            //    phone is already clear of everything Google Photos has
            //    backed up, which is exactly the state we want it in.
            if (texts.any { Labels.isNothingToDo(it) }) {
                return Outcome(true,
                    "Nothing to free up — everything backed up is already off the phone.",
                    0, 0)
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
            if (entry != null && tap(entry)) { sleep(2200); continue }

            // 5. The account picture in the top corner, which is where the
            //    entry lives. It is an image with no text on some versions,
            //    so fall back to position.
            if (!openedMenu) {
                val menu = match(nodes, MENU_LABELS) ?: topRightTarget(nodes)
                if (menu != null && tap(menu)) {
                    openedMenu = true
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
        return Outcome(false,
            "Google Photos is open, but nothing matched ${triggers.joinToString(", ")}. " +
            "On screen: $lastSeen")
    }

    /** Turn the screen on for the length of the run. */
    @Suppress("DEPRECATION")
    private fun wakeScreen(): android.os.PowerManager.WakeLock? = try {
        val pm = getSystemService(android.content.Context.POWER_SERVICE)
                as android.os.PowerManager
        pm.newWakeLock(
            android.os.PowerManager.SCREEN_BRIGHT_WAKE_LOCK or
            android.os.PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "companion:freeup").apply { acquire(3 * 60 * 1000L) }
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
     * The account picture, found by where it is rather than what it says.
     *
     * It is an image, and on some versions it carries no description at
     * all, so there is nothing to match on. It is reliably the last
     * clickable thing along the top edge.
     */
    private fun topRightTarget(nodes: List<AccessibilityNodeInfo>): AccessibilityNodeInfo? {
        val screen = nodes.firstOrNull()?.let { boundsOf(it) } ?: return null
        if (screen.width() <= 0) return null
        val topBand = screen.top + (screen.height() * 0.14).toInt()
        val rightBand = screen.left + (screen.width() * 0.72).toInt()

        return nodes.filter { node ->
            val b = boundsOf(node)
            node.isClickable && b.width() in 1..(screen.width() / 3) &&
                b.top < topBand && b.centerX() > rightBand
        }.maxByOrNull { boundsOf(it).centerX() }
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

        @Volatile
        var instance: FreeSpaceService? = null
            private set

    }
}
