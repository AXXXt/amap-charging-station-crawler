package com.tigercode.evcollector

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.network.FleetControlClient
import com.tigercode.evcollector.network.FleetGenerationRequest
import com.tigercode.evcollector.network.FleetStoppedException
import com.tigercode.evcollector.network.SyncManager
import com.tigercode.evcollector.network.SyncOutcome
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.cancel
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class CollectorKeepAliveService : Service() {
    // MIUI/HyperOS 可能在息屏/切后台时压制主线程调度。调度循环使用工作线程，
    // 并由 PARTIAL_WAKE_LOCK 保证设备在长期插电采集时不会进入 CPU 深度休眠。
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var loopJob: Job? = null
    private var fleetWatchJob: Job? = null
    private var engine: CollectionEngine? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        acquireKeepAliveLock()
        startAsForeground("采集服务正在启动")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            AppPreferences.setScanning(this, false)
            engine?.stop()
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_RESUME_AFTER_COOLDOWN) {
            if (!AppPreferences.isScanning(this)) {
                stopSelf()
                return START_NOT_STICKY
            }
            startAsForeground("采集服务继续运行")
            startSyncLoop()
            return START_STICKY
        }
        if (!AppPreferences.isScanning(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        startAsForeground("采集服务正在运行")
        startSyncLoop()
        return START_STICKY
    }

    override fun onDestroy() {
        engine?.stop()
        loopJob?.cancel()
        fleetWatchJob?.cancel()
        scope.cancel()
        engine = null
        releaseKeepAliveLock()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startSyncLoop() {
        if (loopJob?.isActive == true) return
        val repository = CollectorRepository(this)
        var engineRef: CollectionEngine? = null
        val engine = CollectionEngine(this, repository) {
            engineRef?.let { ref ->
                SyncManager(this@CollectorKeepAliveService, repository, ref).uploadLocalResultsOnly()
            } ?: 0
        }
        engineRef = engine
        this.engine = engine
        startFleetWatcher()
        loopJob = scope.launch {
            if (AppPreferences.isLocalQueueMode(this@CollectorKeepAliveService)) {
                updateNotification("本地任务采集启动")
                engine.startLocal("", "", "", com.tigercode.evcollector.engine.CollectionListener { _, _, _, _ -> })
                while (isActive && AppPreferences.isScanning(this@CollectorKeepAliveService)) {
                    updateNotification(if (engine.isRunning()) "本地任务采集中" else "本地任务已暂停")
                    delay(3000)
                }
                return@launch
            }
            while (isActive && AppPreferences.isScanning(this@CollectorKeepAliveService)) {
                if (!ChargingAccessibilityService.isConnected()) {
                    val accessibilityEnabled = ChargingAccessibilityService.isEnabled(
                        this@CollectorKeepAliveService,
                    )
                    AppPreferences.setRemoteStatus(
                        this@CollectorKeepAliveService,
                        "等待无障碍服务",
                        if (accessibilityEnabled) {
                            "采集助手已开启，正在等待系统重新连接；若长时间无响应请关闭后重新开启一次"
                        } else {
                            "请在系统设置中开启采集助手"
                        },
                    )
                    updateNotification("等待无障碍服务")
                    delay(5000)
                    continue
                }
                // 在领取下一条远程任务前完成批次冷却，避免占着租约等待。
                val cooldownEndAt = engine.batchCooldownEndAt()
                val cooldownActive = cooldownEndAt > System.currentTimeMillis()
                if (cooldownActive) {
                    CooldownAlarmScheduler.schedule(this@CollectorKeepAliveService, cooldownEndAt)
                    if (shouldKeepCollectorForegroundDuringCooldown()) {
                        val switched = ChargingAccessibilityService.current()
                            ?.launchCollectorApp() == true
                        AppPreferences.appendLog(
                            this@CollectorKeepAliveService,
                            if (switched) {
                                "批次休息开始，已切换至采集 App 等待"
                            } else {
                                "批次休息开始，切换采集 App 失败"
                            },
                        )
                        updateNotification("批次休息中，采集 App 保持前台")
                    }
                } else {
                    CooldownAlarmScheduler.cancel(this@CollectorKeepAliveService)
                }
                engine.awaitReadyForNextTask()
                CooldownAlarmScheduler.cancel(this@CollectorKeepAliveService)
                if (cooldownActive && shouldKeepCollectorForegroundDuringCooldown()) {
                    val switchedBack = ChargingAccessibilityService.current()
                        ?.launchAmap() == true
                    AppPreferences.appendLog(
                        this@CollectorKeepAliveService,
                        if (switchedBack) {
                            "批次休息结束，已切换回百度地图"
                        } else {
                            "批次休息结束，切换回百度地图失败"
                        },
                    )
                    updateNotification("批次休息结束，正在返回百度采集")
                }
                if (!isActive || !AppPreferences.isScanning(this@CollectorKeepAliveService)) break
                updateNotification("正在同步服务端")
                // 有任务时快速进入下一轮；没有任务时保留较长轮询间隔，降低服务端请求频率。
                val nextSyncDelayMs = when (
                    val outcome = SyncManager(
                        this@CollectorKeepAliveService,
                        repository,
                        engine,
                    ).runOnce()
                ) {
                    is SyncOutcome.Completed -> {
                        val message = "任务已完成，采集 ${outcome.stationCount} 个站点，等待下一条任务"
                        AppPreferences.setRemoteStatus(this@CollectorKeepAliveService, message)
                        updateNotification(message)
                        showStepStatus(message)
                        3_000L
                    }
                    is SyncOutcome.Skipped -> {
                        val message = "任务已跳过：${outcome.stationName}"
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            message,
                            outcome.reason,
                        )
                        updateNotification("任务已跳过，等待下一条任务")
                        showStepStatus(message)
                        3_000L
                    }
                    is SyncOutcome.DetailRiskReleased -> {
                        val message = "详情页风控，当前任务已释放，继续列表采集"
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "详情页风控，继续采集",
                            outcome.message,
                        )
                        updateNotification(message)
                        showStepStatus(message)
                        8_000L
                    }
                    is SyncOutcome.HumanVerification -> {
                        val message = "检测到人机验证，当前任务已释放，返回列表继续采集"
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "人机验证，继续采集",
                            outcome.message,
                        )
                        updateNotification(message)
                        showStepStatus(message)
                        8_000L
                    }
                    is SyncOutcome.Failed -> {
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "同步失败",
                            outcome.message,
                        )
                        updateNotification("同步失败，等待重试")
                        showStepStatus("同步失败：${outcome.message}")
                        8_000L
                    }
                    is SyncOutcome.NoTask -> {
                        val message = "当前暂无待领取任务，正在等待新任务"
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "等待任务",
                            message,
                        )
                        updateNotification("等待任务")
                        showStepStatus(message)
                        15_000L
                    }
                }
                delay(nextSyncDelayMs)
            }
            stopSelf()
        }
        loopJob?.invokeOnCompletion {
            if (!AppPreferences.isScanning(this@CollectorKeepAliveService)) stopSelf()
        }
    }

    private fun startFleetWatcher() {
        if (fleetWatchJob?.isActive == true) return
        fleetWatchJob = scope.launch {
            val api = FleetControlClient.api(this@CollectorKeepAliveService)
            while (isActive && AppPreferences.isScanning(this@CollectorKeepAliveService)) {
                try {
                    val control = FleetControlClient.control(this@CollectorKeepAliveService, api)
                    if (control.stopRequired || !control.releaseComplete) {
                        engine?.stop()
                        FleetControlClient.stopAndAcknowledge(this@CollectorKeepAliveService, api, control.stopGeneration)
                        loopJob?.cancelAndJoin()
                        stopSelf()
                        return@launch
                    }
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Exception) {
                    // Covers task gaps, cooldown and accessibility reconnection waits.
                }
                delay(3_000L)
            }
        }
    }

    private fun acquireKeepAliveLock() {
        if (wakeLock?.isHeld == true) return
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG).apply {
            setReferenceCounted(false)
            acquire(WAKE_LOCK_TIMEOUT_MS)
        }
    }

    private fun shouldKeepCollectorForegroundDuringCooldown(): Boolean =
        Build.MODEL.contains("23013RK75C", ignoreCase = true)

    private fun releaseKeepAliveLock() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
    }

    private fun startAsForeground(text: String) {
        val notification = buildNotification(text)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("百度充电站采集终端")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    private fun showStepStatus(message: String) {
        CollectionStatusNotifier.show(this, message)
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "采集运行状态",
            NotificationManager.IMPORTANCE_LOW,
        )
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(channel)
    }

    companion object {
        private const val CHANNEL_ID = "collector_foreground"
        private const val NOTIFICATION_ID = 1001
        private const val ACTION_STOP = "com.tigercode.evcollector.STOP_COLLECTOR"
        private const val ACTION_RESUME_AFTER_COOLDOWN =
            "com.tigercode.evcollector.RESUME_AFTER_COOLDOWN"
        private const val WAKE_LOCK_TAG = "evcollector:keep_alive"

        // 长期采集设备保持插电；这里给一个足够长的上限，服务重启时会重新获取。
        private const val WAKE_LOCK_TIMEOUT_MS = 24L * 60 * 60 * 1000

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, CollectorKeepAliveService::class.java),
            )
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, CollectorKeepAliveService::class.java)
                    .setAction(ACTION_STOP)
            )
        }

        fun resumeAfterCooldown(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, CollectorKeepAliveService::class.java)
                    .setAction(ACTION_RESUME_AFTER_COOLDOWN),
            )
        }
    }
}
