package com.immichoutbox.companion

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.util.TypedValue
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * Setup, and nothing else. Four fields, three buttons, and a line saying
 * what happened last.
 *
 * The layout is built in code rather than XML so that the whole app is four
 * Kotlin files with no resources to cross-reference and no dependencies to
 * audit. Somebody about to grant an accessibility service should be able to
 * read all of it in one sitting.
 */
class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var urlField: EditText
    private lateinit var tokenField: EditText
    private lateinit var nameField: EditText
    private lateinit var statusLine: TextView
    private lateinit var accessLine: TextView
    private lateinit var updateLine: TextView
    private lateinit var updateBtn: Button

    private val ticker = Handler(Looper.getMainLooper())

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        prefs = Prefs(this)
        setContentView(buildLayout())
        urlField.setText(prefs.serverUrl)
        tokenField.setText(prefs.token)
        nameField.setText(prefs.deviceName)
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onPause() {
        ticker.removeCallbacksAndMessages(null)
        super.onPause()
    }

    private fun refresh() {
        val on = accessibilityEnabled()
        accessLine.text = if (on)
            "Accessibility access: granted"
        else
            "Accessibility access: NOT granted — the app cannot press the button without it."
        accessLine.setTextColor(if (on) OK_GREEN else WARN_RED)

        statusLine.text = prefs.lastStatus.ifEmpty { "Nothing has happened yet." }
        showUpdate()
        ticker.removeCallbacksAndMessages(null)
        ticker.postDelayed({ refresh() }, 2000)
    }

    /**
     * Offer the update; never perform it.
     *
     * Installing an APK from inside the app would need
     * REQUEST_INSTALL_PACKAGES -- the permission that lets an app install
     * other apps. The whole claim this app makes is that its permission list
     * is short enough to read and contains nothing dangerous, and that claim
     * is worth more than saving a tap. So it opens the server's install page
     * in the browser and lets Android's own installer, and the person
     * holding the phone, do the rest.
     */
    private fun showUpdate() {
        val latest = prefs.latestVersion
        val stale = latest.isNotEmpty() && latest != Relay.VERSION
        updateLine.text = if (stale)
            "Update available: $latest (this is ${Relay.VERSION})"
        else
            "Version ${Relay.VERSION} — up to date"
        updateLine.setTextColor(if (stale) UPDATE_BLUE else Color.GRAY)
        updateBtn.visibility = if (stale) View.VISIBLE else View.GONE
    }

    private fun openInstallPage() {
        val base = prefs.serverUrl
        if (base.isEmpty()) {
            prefs.lastStatus = "Set the relay address first."
            refresh()
            return
        }
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("$base/app")))
        } catch (e: Exception) {
            prefs.lastStatus = "No browser to open ${'$'}base/app"
            refresh()
        }
    }

    private fun save() {
        prefs.serverUrl = urlField.text.toString()
        prefs.token = tokenField.text.toString()
        prefs.deviceName = nameField.text.toString()
        prefs.lastStatus = "Saved. Checking in…"
        refresh()
        FreeSpaceService.instance?.pollNow()
    }

    /**
     * Whether the user has switched this service on in Settings.
     *
     * Read from the system setting rather than from our own service object:
     * the object is null both when access was never granted and when the
     * system has simply not started us yet, and telling someone they have
     * not granted a permission they did grant sends them round in circles.
     */
    private fun accessibilityEnabled(): Boolean {
        val flat = Settings.Secure.getString(
            contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val mine = "$packageName/${FreeSpaceService::class.java.name}"
        val mineShort = "$packageName/.${FreeSpaceService::class.java.simpleName}"
        return flat.split(':').any { it.equals(mine, true) || it.equals(mineShort, true) }
    }

    // ---- layout ---------------------------------------------------------

    private fun buildLayout(): View {
        val pad = dp(20)
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        column.addView(heading("Photo relay companion"))
        column.addView(body(
            "Presses Google Photos' \"Free up space\" when the relay asks, so " +
            "the outbox drains in minutes instead of waiting about a month for " +
            "Smart Storage.\n\n" +
            "It never deletes a photo itself. Google Photos decides what has " +
            "been backed up and is safe to remove, exactly as it does when you " +
            "press the button yourself. This app holds no storage permission, " +
            "so it could not delete one if it tried."))

        column.addView(label("Relay address"))
        urlField = field("http://192.168.1.2:8099", InputType.TYPE_TEXT_VARIATION_URI)
        column.addView(urlField)

        column.addView(label("Pairing code"))
        column.addView(hint("Dashboard → Settings → Phone companion → Show pairing code."))
        tokenField = field("paste the code", InputType.TYPE_CLASS_TEXT)
        column.addView(tokenField)

        column.addView(label("Name for this phone"))
        nameField = field("Pixel", InputType.TYPE_CLASS_TEXT)
        column.addView(nameField)

        column.addView(button("Save and check in") { save() })

        accessLine = body("")
        column.addView(accessLine)
        column.addView(button("Open accessibility settings") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })

        column.addView(label("Last activity"))
        statusLine = body("")
        column.addView(statusLine)

        column.addView(label("App version"))
        updateLine = body("")
        column.addView(updateLine)
        updateBtn = button("Get the new version") { openInstallPage() }
        updateBtn.visibility = View.GONE
        column.addView(updateBtn)

        column.addView(button("Check in now") {
            val service = FreeSpaceService.instance
            if (service == null) {
                prefs.lastStatus = "The service is not running — grant accessibility access."
                refresh()
            } else {
                service.pollNow()
            }
        })

        return ScrollView(this).apply {
            addView(column, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT))
        }
    }

    private fun heading(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
        setPadding(0, 0, 0, dp(12))
    }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
        setPadding(0, dp(18), 0, dp(4))
    }

    private fun hint(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
        setTextColor(Color.GRAY)
        setPadding(0, 0, 0, dp(4))
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        setPadding(0, dp(8), 0, dp(8))
    }

    private fun field(placeholder: String, type: Int) = EditText(this).apply {
        hint = placeholder
        inputType = type
        setSingleLine(true)
    }

    private fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(12) }
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    private companion object {
        val OK_GREEN = Color.parseColor("#1B7F3B")
        val WARN_RED = Color.parseColor("#B3261E")
        val UPDATE_BLUE = Color.parseColor("#1B4FD8")
    }
}
