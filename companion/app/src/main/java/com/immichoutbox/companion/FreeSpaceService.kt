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

        val before = relay.freeBytes()
        launch.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(launch)
        sleep(3000)

        var clicked: Rect? = null
        var items = 0
        var openedMenu = false

        for (step in 0 until MAX_STEPS) {
            val nodes = visibleNodes()

            if (clicked == null) {
                items = itemCountIn(nodes).takeIf { it > 0 } ?: items
                val target = match(nodes, triggers, skip = null)
                if (target != null) {
                    clicked = boundsOf(target)
                    if (!tap(target)) {
                        return Outcome(false, "Found \"${textOf(target)}\" but could not tap it.")
                    }
                    sleep(1500)
                    continue
                }

                // Not on this screen. On most versions the entry lives
                // behind the account button in the top corner, so try there
                // once before giving up.
                if (!openedMenu) {
                    val menu = match(nodes, MENU_LABELS, skip = null)
                    if (menu != null && tap(menu)) {
                        openedMenu = true
                        sleep(1500)
                        continue
                    }
                }
                sleep(POLL_MS)
                continue
            }

            // The button has been pressed; a confirmation usually follows.
            items = itemCountIn(nodes).takeIf { it > 0 } ?: items
            val confirm = match(nodes, confirmWords, skip = clicked)
            if (confirm != null) {
                if (!tap(confirm)) {
                    return Outcome(false, "Could not tap the confirmation.")
                }
                sleep(4000)
                val freed = freedSince(before)
                return Outcome(true, describe(items, freed), items, freed)
            }
            sleep(POLL_MS)
        }

        if (clicked != null) {
            // Pressed, and no confirmation appeared. Some versions free
            // space immediately, so this is a success if the figure moved.
            val freed = freedSince(before)
            return if (freed > 0) Outcome(true, describe(items, freed), items, freed)
                   else Outcome(true, "Pressed the button; nothing needed clearing.", 0, 0)
        }

        // The useful failure: say what was actually on screen, so the labels
        // can be corrected in the dashboard without guesswork.
        val seen = visibleNodes().mapNotNull { textOf(it).takeIf(String::isNotBlank) }
            .distinct().take(12).joinToString(" | ")
        return Outcome(false,
            "Could not find a button matching ${triggers.joinToString(", ")}. " +
            "On screen: ${seen.ifEmpty { "nothing readable" }}")
    }

    private fun describe(items: Int, freed: Long): String {
        val size = if (freed > 0) formatBytes(freed) else "no space"
        return if (items > 0) "Cleared $items item(s), $size freed."
               else "Freed $size."
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

    /**
     * The first node whose label contains one of these words.
     *
     * `skip` is the button already pressed: "Free up space" and the "Free
     * up" on the dialog that follows both match the same word, so without
     * this the service would find the trigger again and sit there tapping
     * it. Bounds identify it -- the same label can legitimately appear
     * twice on one screen.
     */
    private fun match(nodes: List<AccessibilityNodeInfo>, words: List<String>,
                      skip: Rect?): AccessibilityNodeInfo? {
        for (node in nodes) {
            val label = textOf(node).lowercase()
            if (label.isBlank()) continue
            if (words.none { label.contains(it.lowercase()) }) continue
            if (skip != null && boundsOf(node) == skip) continue
            return node
        }
        return null
    }

    /** "41 items" somewhere on screen, for a nicer line in the dashboard. */
    private fun itemCountIn(nodes: List<AccessibilityNodeInfo>): Int {
        val re = Regex("""(\d[\d,]*)\s*(items?|photos?|videos?)""", RegexOption.IGNORE_CASE)
        for (node in nodes) {
            val m = re.find(textOf(node)) ?: continue
            m.groupValues[1].replace(",", "").toIntOrNull()?.let { return it }
        }
        return 0
    }

    /** Tap a node, or the nearest ancestor that is actually clickable. */
    private fun tap(node: AccessibilityNodeInfo): Boolean {
        var target: AccessibilityNodeInfo? = node
        var hops = 0
        while (target != null && hops < 6) {
            if (target.isClickable) {
                return target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            }
            target = target.parent
            hops++
        }
        return false
    }

    private fun sleep(ms: Long) = try {
        Thread.sleep(ms)
    } catch (e: InterruptedException) {
        Thread.currentThread().interrupt()
    }

    companion object {
        const val PHOTOS = "com.google.android.apps.photos"

        /** Used only if the relay sends none, e.g. it is unreachable. */
        val DEFAULT_LABELS = listOf("free up space", "free up device storage")
        val DEFAULT_CONFIRM = listOf("free up", "allow", "delete", "ok", "continue")
        val MENU_LABELS = listOf("account", "profile", "signed in", "open account menu")

        private const val MAX_STEPS = 30
        private const val MAX_NODES = 600
        private const val POLL_MS = 700L

        @Volatile
        var instance: FreeSpaceService? = null
            private set

        fun formatBytes(n: Long): String {
            if (n < 1024) return "$n B"
            val units = listOf("KB", "MB", "GB", "TB")
            var value = n.toDouble() / 1024
            var i = 0
            while (value >= 1024 && i < units.size - 1) {
                value /= 1024; i++
            }
            return String.format(java.util.Locale.US, "%.1f %s", value, units[i])
        }
    }
}
