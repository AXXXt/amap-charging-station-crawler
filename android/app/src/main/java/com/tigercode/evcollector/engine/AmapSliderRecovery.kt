package com.tigercode.evcollector.engine

import android.content.Context
import android.os.SystemClock
import android.provider.Settings
import com.google.gson.Gson
import com.google.gson.JsonParser
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CooldownAlarmScheduler
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.engine.AmapSliderRestartPolicy as Policy
import com.tigercode.evcollector.network.RetrofitClient
import com.tigercode.evcollector.network.TaskActionRequest
import com.tigercode.evcollector.privileged.ShizukuMapController
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import retrofit2.HttpException

class AmapSliderRestartException(val phrase: String) :
    IllegalStateException("检测到高德滑块验证，暂停采集并重启地图：$phrase")

/** Private durable state. A manual stop cancels the restart but must not discard a held lease. */
object AmapSliderRecoveryStore {
    data class PendingLease(val taskId: String, val deviceCode: String, val leaseToken: String, val mode: String)
    private val gson = Gson()
    private fun prefs(context: Context) = context.getSharedPreferences("amap_slider_recovery", Context.MODE_PRIVATE)
    fun bootCount(context: Context): Int = Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT, -1)

    @Synchronized fun state(context: Context): Policy.State? {
        val raw = prefs(context).getString("state", null) ?: return null
        val saved = gson.fromJson(raw, Policy.State::class.java)
        val restored = Policy.restore(saved, bootCount(context))
        if (restored != saved) save(context, restored)
        return restored
    }
    @Synchronized fun save(context: Context, state: Policy.State) {
        check(prefs(context).edit().putString("state", gson.toJson(state)).commit()) { "无法保存滑块恢复进度" }
    }
    @Synchronized fun request(context: Context) {
        if (AppPreferences.isScanning(context) && state(context) == null) save(context, Policy.requested(bootCount(context)))
    }
    @Synchronized fun transition(context: Context, next: Policy.State) {
        if (AppPreferences.isScanning(context) && state(context) != null) save(context, next)
    }
    @Synchronized fun cancel(context: Context) {
        prefs(context).edit().remove("state").commit()
        CooldownAlarmScheduler.cancel(context)
    }
    @Synchronized fun pendingLease(context: Context): PendingLease? =
        prefs(context).getString("pending_lease", null)?.let { gson.fromJson(it, PendingLease::class.java) }
    @Synchronized fun rememberLease(context: Context, lease: PendingLease) {
        check(prefs(context).edit().putString("pending_lease", gson.toJson(lease)).commit()) { "无法保存待释放任务" }
    }
    @Synchronized fun clearLease(context: Context) {
        check(prefs(context).edit().remove("pending_lease").commit()) { "无法保存任务释放结果" }
    }
    fun blocksNewTask(context: Context): Boolean = state(context) != null || pendingLease(context) != null
}

/** Called only between tasks, after SyncManager has unwound collection and stopped lease renewal. */
class AmapSliderRecoveryController(
    private val context: Context,
    private val notify: (String) -> Unit,
) {
    private fun running() = AppPreferences.isScanning(context)
    private var lastLoggedStatus = ""
    private var scheduledDeadlineElapsed = 0L

    private fun status(message: String, log: Boolean = false) {
        if (message != AppPreferences.remoteStatus(context)) {
            AppPreferences.setRemoteStatus(context, message)
            notify(message)
        }
        if (log && lastLoggedStatus != message) {
            AppPreferences.appendLog(context, "滑块恢复：$message")
            lastLoggedStatus = message
        }
    }

    suspend fun releasePendingLease(): Boolean = withContext(Dispatchers.IO) {
        val lease = AmapSliderRecoveryStore.pendingLease(context) ?: return@withContext true
        try {
            val api = RetrofitClient.serverApi(AppPreferences.serverUrl(context), context)
            val body = TaskActionRequest(deviceCode = lease.deviceCode, mode = lease.mode, leaseToken = lease.leaseToken)
            if (lease.mode.startsWith("MONITOR")) {
                check(api.releaseTask(lease.taskId, body)["released"] == true) { "服务端未确认任务释放" }
            } else {
                // The legacy NORMAL queue has no release endpoint; use its existing retry protocol.
                api.failTask(lease.taskId, body.copy(errorCode = "SLIDER_RESTART", errorMessage = "滑块验证，重启高德后重试", retryable = true))
            }
            AmapSliderRecoveryStore.clearLease(context)
            AppPreferences.appendLog(context, if (lease.mode.startsWith("MONITOR")) "滑块恢复：当前任务已释放回队列" else "滑块恢复：普通任务已提交重试")
            true
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            val detail = if (error is HttpException && error.code() == 409) runCatching {
                JsonParser.parseString(error.response()?.errorBody()?.string().orEmpty()).asJsonObject.get("detail")?.asString
            }.getOrNull() else null
            if (detail in setOf("TASK_LEASE_STALE", "TASK_NOT_ACTIVE", "TASK_NOT_FOUND")) {
                AmapSliderRecoveryStore.clearLease(context)
                AppPreferences.appendLog(context, "滑块恢复：原任务租约已不属于本机，无需再次释放")
                true
            } else {
                // Do not log tokens, server bodies, or guess that a failed request released the task.
                status("任务释放尚未确认，已暂停领取；请检查网络与服务端接口", log = true)
                false
            }
        }
    }

    suspend fun step(): Boolean = try {
        stepAllowed()
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        status("滑块恢复暂未完成（${error.javaClass.simpleName}），保持暂停并重试", log = true)
        delay(10_000L)
        true
    }

    private suspend fun stepAllowed(): Boolean {
        if (!AmapSliderRecoveryStore.blocksNewTask(context)) return false
        if (!running()) return true
        if (!releasePendingLease()) {
            delay(10_000L)
            return true
        }
        if (!running()) return true
        val state = AmapSliderRecoveryStore.state(context) ?: return true
        val service = ChargingAccessibilityService.current()
        when (state.phase) {
            Policy.Phase.STOPPING -> {
                status("已暂停采集，正在通过 Shizuku 关闭高德后台", log = true)
                val result = ShizukuMapController.forceStopMap(context) {
                    !running() || AmapSliderRecoveryStore.state(context)?.phase != Policy.Phase.STOPPING
                }
                if (!running()) return true
                if (!result.success) {
                    status("${result.message}；保持暂停，10秒后重试", log = true)
                    delay(10_000L)
                    return true
                }
                if (service?.launchCollectorApp() != true) {
                    status("高德已关闭，返回采集 App 未成功，保持暂停并重试", log = true)
                    delay(10_000L)
                    return true
                }
                if (!running()) return true
                AmapSliderRecoveryStore.transition(context, Policy.afterStop(SystemClock.elapsedRealtime(), AmapSliderRecoveryStore.bootCount(context)))
                status("高德后台已关闭，已返回采集 App，休息2分钟", log = true)
            }
            Policy.Phase.REOPENING -> {
                status("休息结束，正在打开高德地图", log = true)
                if (service?.launchAmap() != true) {
                    status("打开高德地图未成功，保持暂停，10秒后重试", log = true)
                    delay(10_000L)
                    return true
                }
                if (!running()) return true
                AmapSliderRecoveryStore.transition(context, Policy.afterOpen(SystemClock.elapsedRealtime(), AmapSliderRecoveryStore.bootCount(context)))
                status("高德已打开，等待1分钟后恢复采集", log = true)
            }
            Policy.Phase.RESTING, Policy.Phase.WARMING -> {
                val remaining = Policy.remainingMs(state, SystemClock.elapsedRealtime())
                if (remaining == 0L) {
                    val next = Policy.advance(state, SystemClock.elapsedRealtime())
                    if (next == null) {
                        AmapSliderRecoveryStore.cancel(context)
                        status("打开后等待已结束，继续领取任务采集", log = true)
                    } else {
                        AmapSliderRecoveryStore.transition(context, next)
                    }
                    return true
                }
                if (scheduledDeadlineElapsed != state.deadlineElapsedMs) {
                    CooldownAlarmScheduler.schedule(context, System.currentTimeMillis() + remaining)
                    scheduledDeadlineElapsed = state.deadlineElapsedMs
                }
                val seconds = ((remaining + 9_999L) / 10_000L) * 10L
                status(if (state.phase == Policy.Phase.RESTING) "采集 App 休息中，约${seconds}秒后打开高德" else "高德已打开，约${seconds}秒后恢复采集")
                delay(minOf(1_000L, remaining))
                return true
            }
        }
        return true
    }
}
