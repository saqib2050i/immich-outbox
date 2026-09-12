package com.immichoutbox.companion

/**
 * Reading Google Photos' screens.
 *
 * Deliberately free of Android imports so it can be unit-tested on a
 * laptop. This is the fragile half of the app -- it matches text somebody
 * else is free to change -- and it was previously buried inside the
 * accessibility service where nothing could reach it. The first version
 * looked for "free up space" and never found the entry, which is actually
 * called "Free up space on this device".
 *
 * The strings in the test beside this file were read off the real phone.
 */
object Labels {

    /**
     * The blue button that does it: "Free up 29.80 MB".
     *
     * The size is the whole point of the pattern. Without it this also
     * matches the menu entry that leads here and the heading above it, and
     * the service taps the wrong thing.
     */
    val ACTION = Regex("""free up\s+([\d.,]+)\s*(b|kb|mb|gb|tb)\b""",
                       RegexOption.IGNORE_CASE)

    /** The screen after it: "You freed up 29.80 MB". */
    val FREED = Regex("""freed up\s+([\d.,]+)\s*(b|kb|mb|gb|tb)\b""",
                      RegexOption.IGNORE_CASE)

    /** Everything backed up is already off the phone. A success, not a fault. */
    const val NOTHING = "nothing to free up"

    /** "29,80" -- a decimal comma, not a thousands separator, which never
     *  leaves exactly two digits behind it. */
    private val DECIMAL_COMMA = Regex("""\d+,\d{1,2}""")

    /** Used only when the relay sends none, e.g. it could not be reached. */
    val ENTRY = listOf(
        "free up space on this device", "free up space", "free up device storage")
    val CONFIRM = listOf("free up", "allow", "delete", "ok", "continue")
    val MENU = listOf(
        "account and settings", "account", "profile", "signed in", "open account menu")

    // ---- how far along Google Photos says its own backup is --------------
    //
    // Read off a Pixel 1. Collapsed, the pill on Photos' home screen says
    // "Backing up photos". Expanded it becomes a panel:
    //
    //     Backing up 250 photos
    //     2 hours, 26 min remaining
    //     Keep the app open for faster backup
    //
    // That last line is Google's own argument for dwelling, and the count is
    // the diagnostic: a slow backup and a stopped one are indistinguishable
    // from the server until you can watch the figure fail to move.

    /** Marks the backup panel. Used when the relay sends none. */
    val BACKUP = listOf("backing up", "backup in progress", "uploading")

    /** "Backing up 250 photos". */
    val BACKING = Regex("""backing up\s+([\d,]+)\s+(?:photo|video|item|file)s?""",
                        RegexOption.IGNORE_CASE)

    /** "2 hours, 26 min remaining", or just "26 min remaining". */
    val ETA = Regex(
        """(?:(\d+)\s*h(?:ou)?rs?)?[\s,]*(?:(\d+)\s*min(?:ute)?s?)?\s*remaining""",
        RegexOption.IGNORE_CASE)

    class Backup(
        val active: Boolean,
        val remaining: Int,
        val etaMinutes: Int,
        /** What the screen actually said, carried back so a renamed label
         *  shows up in the dashboard as text rather than as silence. */
        val detail: String,
    )

    fun backupState(texts: List<String>, marks: List<String>): Backup {
        val words = marks.ifEmpty { BACKUP }
        var active = false
        var remaining = 0
        var eta = 0
        val said = LinkedHashSet<String>()

        for (text in texts) {
            var hit = false
            if (matches(text, words)) { active = true; hit = true }
            BACKING.find(text)?.let {
                active = true; hit = true
                remaining = it.groupValues[1].replace(",", "").toIntOrNull() ?: 0
            }
            ETA.find(text)?.let { m ->
                val h = m.groupValues[1].toIntOrNull()
                val mins = m.groupValues[2].toIntOrNull()
                // Everything in the pattern is optional, so the bare word
                // "remaining" matches it. Only a figure counts -- and a
                // storage screen's "902 MB remaining" carries no h or min.
                if (h != null || mins != null) {
                    eta = (h ?: 0) * 60 + (mins ?: 0); hit = true
                }
            }
            if (hit) said.add(text)
        }
        return Backup(active, remaining, eta, said.take(3).joinToString(" · "))
    }

    fun matches(text: String, words: List<String>): Boolean {
        val lower = text.lowercase()
        return words.any { it.isNotBlank() && lower.contains(it.lowercase()) }
    }

    fun isNothingToDo(text: String): Boolean = text.contains(NOTHING, ignoreCase = true)

    /**
     * Are we still standing on one of the free-up screens, or on the menu
     * that leads to them?
     *
     * Asked at the end of a run, to decide how many times to press Back.
     * Google Photos resumes wherever it was left, so a run that walks away
     * from the result screen hands the next run a screen that already says
     * "You freed up 29.80 MB" -- and the next run reads that as its own
     * result, reports a figure nothing earned, and frees nothing. The
     * outbox then stays full while the dashboard says all is well, which is
     * the worst way for this to fail.
     *
     * The entry words come from the server, because they are a setting.
     */
    fun isFreeUpScreen(texts: List<String>, entry: List<String>): Boolean =
        texts.any { text ->
            ACTION.containsMatchIn(text) || FREED.containsMatchIn(text) ||
                isNothingToDo(text) || matches(text, entry)
        }

    /** The figure Google Photos itself reports, which beats measuring free
     *  space: anything else on the phone writing a file mid-run would spoil
     *  that measurement. */
    fun freedBytes(text: String): Long? {
        val m = FREED.find(text) ?: return null
        return bytes(m.groupValues[1], m.groupValues[2])
    }

    fun bytes(amount: String, unit: String): Long {
        // The figure is whatever is free at that instant -- a few MB one day,
        // several GB the next -- so it is always captured, never matched.
        //
        // A comma is a thousands separator in "1,234 MB" but a decimal point
        // in "29,80 MB", which is how much of Europe writes it. Stripping it
        // blindly turned 29.8 MB into 2,980 MB.
        val cleaned = if (DECIMAL_COMMA.matches(amount))
            amount.replace(",", ".") else amount.replace(",", "")
        val n = cleaned.toDoubleOrNull() ?: return 0
        val scale = when (unit.lowercase()) {
            "kb" -> 1L shl 10
            "mb" -> 1L shl 20
            "gb" -> 1L shl 30
            "tb" -> 1L shl 40
            else -> 1L
        }
        return (n * scale).toLong()
    }

    fun format(n: Long): String {
        if (n < 1024) return "$n B"
        val units = listOf("KB", "MB", "GB", "TB")
        var value = n.toDouble() / 1024
        var i = 0
        while (value >= 1024 && i < units.size - 1) { value /= 1024; i++ }
        return String.format(java.util.Locale.US, "%.1f %s", value, units[i])
    }
}
