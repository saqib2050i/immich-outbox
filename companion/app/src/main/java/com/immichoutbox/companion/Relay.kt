package com.immichoutbox.companion

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.StatFs
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Talking to the relay.
 *
 * HttpURLConnection and org.json rather than a library: the whole app is
 * four files and no dependencies, which is the point. Anyone can read all
 * of it before granting it an accessibility service.
 *
 * The phone always dials out; the relay never dials in. There is no
 * listening socket on this phone, so nothing on the network can reach the
 * app at all.
 */
class Relay(private val context: Context, private val prefs: Prefs) {

    class Instruction(
        /** "free" walks Google Photos to the button and presses it; "look"
         *  only opens it and reads what it says about its own backup. A
         *  look is seconds where a free-up is a minute of tapping, which is
         *  what makes it cheap enough to run on a schedule. */
        val action: String,
        val freeSpace: Boolean,
        val requestId: String,
        val reason: String,
        val labels: List<String>,
        val confirmLabels: List<String>,
        // What marks Google Photos' backup panel, and how long to stand in
        // front of it afterwards. Both are the server's decision: dwelling
        // costs screen time on the phone, and the person who wants it off is
        // at a dashboard rather than at the shelf.
        val backupLabels: List<String>,
        val dwellSeconds: Int,
        val nextPollSeconds: Int,
        // What the server has on offer at /app. Blank when it carries no
        // build. Only ever displayed -- see MainActivity for why this app
        // does not install anything itself.
        val latestVersion: String,
    )

    /** Check in. Returns null if the relay could not be reached. */
    fun poll(): Instruction? {
        val body = JSONObject()
            .put("device", prefs.deviceName)
            .put("app_version", installedVersion(context))
            .put("android", Build.VERSION.RELEASE ?: "")
            .put("battery", batteryPercent())
            .put("charging", isCharging())
            .put("free_bytes", freeBytes())
            // What this build understands. Told rather than left to be
            // guessed from the version string, so the dashboard can say
            // whether an instruction would land or be quietly ignored.
            .put("features", org.json.JSONArray(FEATURES))

        val reply = post("/api/companion/poll", body) ?: return null
        return Instruction(
            // Falls back to the old flag, so this build still does the
            // right thing against a server that predates the distinction.
            action = reply.optString("action",
                if (reply.optBoolean("free_space", false)) "free" else "none"),
            freeSpace = reply.optBoolean("free_space", false),
            requestId = reply.optString("request_id", ""),
            reason = reply.optString("reason", ""),
            labels = strings(reply, "labels"),
            confirmLabels = strings(reply, "confirm_labels"),
            backupLabels = strings(reply, "backup_labels"),
            // Clamped: a typo in a text field must not park the phone with
            // its screen on for an hour.
            dwellSeconds = reply.optInt("dwell_seconds", 0).coerceIn(0, 900),
            // Never faster than a minute, whatever the server says: a
            // misconfigured interval must not turn into a hot loop on a
            // phone.
            nextPollSeconds = reply.optInt("next_poll_seconds", 1800).coerceAtLeast(60),
            latestVersion = reply.optString("latest_version", ""),
        )
    }

    fun report(requestId: String, action: String, ok: Boolean, detail: String,
               items: Int, freedBytes: Long, dwelledSeconds: Int,
               backup: Labels.Backup?): Boolean {
        val body = JSONObject()
            .put("request_id", requestId)
            .put("action", action)
            .put("ok", ok)
            .put("detail", detail)
            .put("items", items)
            .put("freed_bytes", freedBytes)
            // How long it actually stood in front of Google Photos. Whether
            // a dwell had happened at all used to be answerable only by
            // reading the phone's wake locks over a cable.
            .put("dwelled_seconds", dwelledSeconds)
        // What Google Photos said about its own backup. The server treats
        // this as a note and never as evidence -- a file is backed up when
        // it disappears from the outbox, not when a screen says so.
        if (backup != null) {
            body.put("backup", JSONObject()
                .put("active", backup.active)
                .put("remaining", backup.remaining)
                .put("eta_minutes", backup.etaMinutes)
                .put("detail", backup.detail))
        }
        return post("/api/companion/report", body) != null
    }

    private fun post(path: String, body: JSONObject): JSONObject? {
        val base = prefs.serverUrl
        if (base.isEmpty() || prefs.token.isEmpty()) return null

        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 15_000
                readTimeout = 20_000
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("X-Companion-Token", prefs.token)
            }
            conn.outputStream.use { it.write(body.toString().toByteArray()) }

            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use(BufferedReader::readText) ?: ""
            if (code !in 200..299) {
                prefs.lastStatus = "Server said $code: ${text.take(120)}"
                return null
            }
            if (text.isBlank()) JSONObject() else JSONObject(text)
        } catch (e: Exception) {
            prefs.lastStatus = "Could not reach the relay: ${e.message}"
            null
        } finally {
            conn?.disconnect()
        }
    }

    private fun strings(o: JSONObject, key: String): List<String> {
        val arr = o.optJSONArray(key) ?: return emptyList()
        return (0 until arr.length()).map { arr.optString(it, "") }.filter { it.isNotEmpty() }
    }

    private fun batteryPercent(): Int {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
        return bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
    }

    private fun isCharging(): Boolean {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
        return bm?.isCharging ?: false
    }

    /**
     * Free space on the volume the photos live on.
     *
     * Measured through the app's own private directory, which needs no
     * permission but sits on the same volume, so the figure is the same one
     * Settings would show.
     */
    fun freeBytes(): Long = try {
        val dir = context.getExternalFilesDir(null) ?: context.filesDir
        StatFs(dir.absolutePath).availableBytes
    } catch (e: Exception) {
        -1L
    }

    companion object {
        /** Instructions this build knows how to carry out. */
        val FEATURES = listOf("look", "dwell", "backup")

        /**
         * What is actually installed, asked of the package manager.
         *
         * This was a constant, and it lied: the build's versionName came
         * from the project's VERSION file while this stayed pinned at
         * 1.1.0, so a freshly installed 1.3.0 reported 1.1.0 and the server
         * offered it an update it already had, forever. A number that has
         * to be edited in two places will eventually disagree with itself;
         * asking Android what it installed cannot.
         */
        fun installedVersion(context: Context): String = try {
            context.packageManager
                .getPackageInfo(context.packageName, 0).versionName ?: "unknown"
        } catch (e: Exception) {
            "unknown"
        }
    }
}
