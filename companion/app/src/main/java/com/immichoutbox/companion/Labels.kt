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
