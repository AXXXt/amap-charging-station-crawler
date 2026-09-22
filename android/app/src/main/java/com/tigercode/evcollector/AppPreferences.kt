package com.tigercode.evcollector

import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.Settings
import com.tigercode.evcollector.core.engine.CollectionPacingPolicy

object AppPreferences {
    // 默认使用已部署到云服务器的调度服务，避免手机受局域网和电脑开关机影响。
    private const val DEFAULT_SERVER_URL = "http://116.62.103.230:8800/"
    private const val PREFS = "ev_collector_prefs"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_DEVICE_CODE = "device_code"
    private const val KEY_ACTIVATION_CODE = "activation_code"
    private const val KEY_DEVICE_NAME = "device_name"
    private const val KEY_DEVICE_TOKEN = "device_token"
    private const val KEY_SCANNING = "scanning"
    private const val KEY_REMOTE_STATUS = "remote_status"
    private const val KEY_REMOTE_ERROR = "remote_error"
    private const val KEY_LOG = "log"
    private const val KEY_HENAN_IMPORT_READY = "henan_import_ready"
    private const val KEY_MONITOR_MODE = "monitor_mode"
    private const val KEY_PACING_DETAIL_ENTRIES = "pacing_detail_entries"
    private const val KEY_PACING_BATCH_COMPLETED = "pacing_batch_completed"
    private const val KEY_PACING_BATCH_TARGET = "pacing_batch_target"
    private const val KEY_PACING_COOLDOWN_UNTIL = "pacing_cooldown_until"

    fun serverUrl(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val stored = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL).orEmpty()
        val normalized = normalizeServerUrl(stored)
        if (normalized == null || isLoopbackServerUrl(normalized) || isPreviousLanServerUrl(normalized)) {
            // 兼容旧版本保存的本机/旧电脑地址，自动切换到当前电脑局域网地址。
            prefs.edit().putString(KEY_SERVER_URL, DEFAULT_SERVER_URL).apply()
            return DEFAULT_SERVER_URL
        }
        return normalized
    }

    private fun isLoopbackServerUrl(value: String): Boolean {
        val host = runCatching { Uri.parse(value).host }.getOrNull()?.lowercase()
        return host == "127.0.0.1" || host == "localhost" || host == "0.0.0.0"
    }

    private fun isPreviousLanServerUrl(value: String): Boolean {
        val uri = runCatching { Uri.parse(value) }.getOrNull()
        return uri?.host == "192.168.3.27" && (uri.port == -1 || uri.port == 8800)
    }

    fun normalizeServerUrl(value: String): String? {
        val trimmed = value.trim()
        val uri = runCatching { Uri.parse(trimmed) }.getOrNull() ?: return null
        if (uri.scheme !in setOf("http", "https") || uri.host.isNullOrBlank()) return null
        return if (trimmed.endsWith('/')) trimmed else "$trimmed/"
    }

    fun saveServerUrl(context: Context, url: String) {
        val normalized = normalizeServerUrl(url) ?: return
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER_URL, normalized)
            .apply()
    }

    fun deviceCode(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val existing = prefs.getString(KEY_DEVICE_CODE, "")
        if (!existing.isNullOrEmpty()) return existing
        val generated = "EV-" + Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ANDROID_ID,
        ).orEmpty().takeLast(8).uppercase()
        prefs.edit().putString(KEY_DEVICE_CODE, generated).apply()
        return generated
    }

    fun isScanning(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_SCANNING, false)

    fun isHenanImportReady(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_HENAN_IMPORT_READY, false)

    fun isMonitorMode(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_MONITOR_MODE, false)

    fun setMonitorMode(context: Context, enabled: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_MONITOR_MODE, enabled)
            .apply()
    }

    fun setHenanImportReady(context: Context, ready: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_HENAN_IMPORT_READY, ready)
            .apply()
    }

    fun setScanning(context: Context, scanning: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_SCANNING, scanning)
            .apply()
    }

    fun activationCode(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ACTIVATION_CODE, "dev-activate")
            .orEmpty()

    fun saveActivationCode(context: Context, value: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_ACTIVATION_CODE, value.trim())
            .apply()
    }

    fun deviceName(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_DEVICE_NAME, Build.MANUFACTURER + " " + Build.MODEL)
            .orEmpty()

    fun saveDeviceName(context: Context, value: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_DEVICE_NAME, value.trim())
            .apply()
    }

    fun deviceToken(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_DEVICE_TOKEN, "")
            .orEmpty()

    fun saveDeviceToken(context: Context, token: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_DEVICE_TOKEN, token.trim())
            .apply()
    }

    fun pacingDetailEntryTimes(
        context: Context,
        nowMs: Long = System.currentTimeMillis(),
    ): List<Long> {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_PACING_DETAIL_ENTRIES, "")
            .orEmpty()
        return CollectionPacingPolicy.pruneDetailEntries(
            timestamps = raw.split(',').mapNotNull { it.toLongOrNull() },
            nowMs = nowMs,
        )
    }

    fun pacingBatchCompletedCount(context: Context): Int =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getInt(KEY_PACING_BATCH_COMPLETED, 0)
            .coerceAtLeast(0)

    fun pacingBatchTarget(context: Context): Int =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getInt(KEY_PACING_BATCH_TARGET, 0)

    fun pacingCooldownUntil(context: Context): Long =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getLong(KEY_PACING_COOLDOWN_UNTIL, 0L)

    fun savePacingState(
        context: Context,
        detailEntryTimes: Collection<Long>,
        batchCompletedCount: Int,
        batchTarget: Int,
        cooldownUntilMs: Long,
    ) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(
                KEY_PACING_DETAIL_ENTRIES,
                detailEntryTimes.sorted().takeLast(64).joinToString(","),
            )
            .putInt(KEY_PACING_BATCH_COMPLETED, batchCompletedCount.coerceAtLeast(0))
            .putInt(KEY_PACING_BATCH_TARGET, batchTarget)
            .putLong(KEY_PACING_COOLDOWN_UNTIL, cooldownUntilMs.coerceAtLeast(0L))
            .apply()
    }

    fun remoteStatus(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_REMOTE_STATUS, "未启动")
            .orEmpty()

    fun remoteError(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_REMOTE_ERROR, "")
            .orEmpty()

    fun setRemoteStatus(context: Context, status: String, error: String = "") {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_REMOTE_STATUS, status.take(300))
            .putString(KEY_REMOTE_ERROR, error.take(500))
            .apply()
    }

    fun logs(context: Context): List<String> =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_LOG, "")
            .orEmpty()
            .split("\n")
            .filter { it.isNotBlank() }

    fun appendLog(context: Context, message: String) {
        val timestamp = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
            .format(java.util.Date())
        val current = logs(context).takeLast(199).toMutableList()
        current.add("[$timestamp] $message")
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_LOG, current.joinToString("\n"))
            .apply()
    }

    fun clearLogs(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .remove(KEY_LOG)
            .apply()
    }
}
