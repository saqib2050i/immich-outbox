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
        val freeSpace: Boolean,
        val requestId: String,
        val reason: String,
        val labels: List<String>,
        val confirmLabels: List<String>,
        val nextPollSeconds: Int,
    )

    /** Check in. Returns null if the relay could not be reached. */
    fun poll(): Instruction? {
        val body = JSONObject()
            .put("device", prefs.deviceName)
            .put("app_version", VERSION)
            .put("android", Build.VERSION.RELEASE ?: "")
            .put("battery", batteryPercent())
            .put("charging", isCharging())
            .put("free_bytes", freeBytes())

        val reply = post("/api/companion/poll", body) ?: return null
        return Instruction(
            freeSpace = reply.optBoolean("free_space", false),
            requestId = reply.optString("request_id", ""),
            reason = reply.optString("reason", ""),
            labels = strings(reply, "labels"),
            confirmLabels = strings(reply, "confirm_labels"),
            // Never faster than a minute, whatever the server says: a
            // misconfigured interval must not turn into a hot loop on a
            // phone.
            nextPollSeconds = reply.optInt("next_poll_seconds", 1800).coerceAtLeast(60),
        )
    }

    fun report(requestId: String, ok: Boolean, detail: String,
               items: Int, freedBytes: Long): Boolean {
        val body = JSONObject()
            .put("request_id", requestId)
            .put("ok", ok)
            .put("detail", detail)
            .put("items", items)
            .put("freed_bytes", freedBytes)
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
        const val VERSION = "1.1.0"
    }
}
