package com.tigercode.evcollector

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class CooldownAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != ACTION_COOLDOWN_FINISHED) return
        if (!AppPreferences.isScanning(context)) return
        CollectorKeepAliveService.resumeAfterCooldown(context)
    }

    companion object {
        const val ACTION_COOLDOWN_FINISHED =
            "com.tigercode.evcollector.action.COOLDOWN_FINISHED"
    }
}
