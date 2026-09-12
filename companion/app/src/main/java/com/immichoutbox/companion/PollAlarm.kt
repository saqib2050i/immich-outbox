package com.immichoutbox.companion

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock

/**
 * The thing that wakes the phone up to check in.
 *
 * The first version scheduled the next poll with `Handler.postDelayed`,
 * which was simply wrong: a Handler callback cannot wake a sleeping CPU. On
 * a phone sitting with its screen off -- the entire intended use -- the
 * callback is deferred until something else happens to wake the device, so
 * the app never checked in at all. Measured on a Pixel 1: a 60-second
 * interval produced no check-in in over 100 seconds, screen off and plugged
 * in, with the process alive the whole time. That is why it only ever
 * worked when the phone was picked up and "Check in now" was pressed.
 *
 * AlarmManager is the only thing that can wake the device on a timer.
 *
 * `setAndAllowWhileIdle` rather than the exact variant on purpose: exact
 * alarms need SCHEDULE_EXACT_ALARM from Android 12, and this app's whole
 * claim is a permission list short enough to read. Nothing here needs to
 * happen at a precise moment -- "in about a minute" is the requirement. The
 * cost is that in deep Doze, which needs the phone unplugged, the system
 * holds these to roughly one every nine minutes. A phone on a charger never
 * enters that state.
 */
class PollAlarm : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val service = FreeSpaceService.instance
        if (service != null) {
            // The service takes a CPU wake lock before it starts, so the
            // phone stays up for the check-in and any run that follows.
            service.pollNow()
        } else {
            // Accessibility access is off, or the system has not started us
            // yet. Nothing can be done, so ask again later rather than
            // letting the chain die -- the alarm is the only thing keeping
            // this app alive on a schedule.
            schedule(context, RETRY_SECONDS)
        }
    }

    companion object {
        private const val RETRY_SECONDS = 300
        private const val REQUEST = 4711

        private fun intentFor(context: Context): PendingIntent =
            PendingIntent.getBroadcast(
                context, REQUEST,
                Intent(context, PollAlarm::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        /**
         * Is a check-in already armed?
         *
         * FLAG_NO_CREATE returns null when no matching PendingIntent
         * exists, which is the only way to ask. Without it a reconnect --
         * an app update, a reboot, anything that reconfigures accessibility
         * -- rescheduled the next poll to five seconds, so a flurry of
         * reconnects became a flurry of polls.
         */
        fun pending(context: Context): Boolean =
            PendingIntent.getBroadcast(
                context, REQUEST,
                Intent(context, PollAlarm::class.java),
                PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE) != null

        fun schedule(context: Context, seconds: Int) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            // Elapsed time rather than wall clock: a clock correction must
            // not send the next check-in hours away, or into a busy loop.
            val at = SystemClock.elapsedRealtime() + seconds.coerceAtLeast(30) * 1000L
            am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, intentFor(context))
        }

        fun cancel(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            am.cancel(intentFor(context))
        }
    }
}
