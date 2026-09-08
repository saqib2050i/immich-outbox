package com.immichoutbox.companion

import android.content.Context

/**
 * Everything this app knows, which is deliberately almost nothing: where the
 * relay is, and how to prove it is allowed to talk to it.
 *
 * No photo ever passes through here, and no path to one is stored. The app
 * holds no storage permission, so it could not read one if it tried.
 */
class Prefs(context: Context) {

    private val sp = context.getSharedPreferences("companion", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString(KEY_URL, "") ?: ""
        set(v) {
            // Trailing slashes turn every request path into a double slash,
            // which some servers route and some do not.
            sp.edit().putString(KEY_URL, v.trim().trimEnd('/')).apply()
        }

    var token: String
        get() = sp.getString(KEY_TOKEN, "") ?: ""
        set(v) = sp.edit().putString(KEY_TOKEN, v.trim()).apply()

    var deviceName: String
        get() = sp.getString(KEY_NAME, android.os.Build.MODEL ?: "phone") ?: "phone"
        set(v) = sp.edit().putString(KEY_NAME, v.trim()).apply()

    /** Last thing that happened, for the setup screen to show. */
    var lastStatus: String
        get() = sp.getString(KEY_STATUS, "") ?: ""
        set(v) = sp.edit().putString(KEY_STATUS, v).apply()

    /** The version the relay is serving, learned on a check-in. */
    var latestVersion: String
        get() = sp.getString(KEY_LATEST, "") ?: ""
        set(v) = sp.edit().putString(KEY_LATEST, v).apply()

    val configured: Boolean
        get() = serverUrl.isNotEmpty() && token.isNotEmpty()

    private companion object {
        const val KEY_URL = "server_url"
        const val KEY_TOKEN = "token"
        const val KEY_NAME = "device_name"
        const val KEY_STATUS = "last_status"
        const val KEY_LATEST = "latest_version"
    }
}
