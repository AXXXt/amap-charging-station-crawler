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
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.network.SyncManager
import com.tigercode.evcollector.network.SyncOutcome
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class CollectorKeepAliveService : Service() {
    // MIUI/HyperOS 可能在息屏/切后台时压制主线程调度。调度循环使用工作线程，
    // 并由 PARTIAL_WAKE_LOCK 保证设备在长期插电采集时不会进入 CPU 深度休眠。
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val mainHandler = Handler(Looper.getMainLooper())
    private var loopJob: Job? = null
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
        engine.addListener { _, message, _, _ ->
            showStepToast(message)
        }
        loopJob = scope.launch {
            // 避免无任务时每 15 秒重复弹 Toast；重新发现并完成任务后会再次提示。
            var waitingToastShown = false
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
                if (cooldownEndAt > System.currentTimeMillis()) {
                    CooldownAlarmScheduler.schedule(this@CollectorKeepAliveService, cooldownEndAt)
                } else {
                    CooldownAlarmScheduler.cancel(this@CollectorKeepAliveService)
                }
                engine.awaitReadyForNextTask()
                CooldownAlarmScheduler.cancel(this@CollectorKeepAliveService)
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
                        showStepToast(message)
                        waitingToastShown = false
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
                        showStepToast(message)
                        waitingToastShown = false
                        3_000L
                    }
                    is SyncOutcome.Failed -> {
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "同步失败",
                            outcome.message,
                        )
                        updateNotification("同步失败，等待重试")
                        showStepToast("同步失败：${outcome.message}")
                        waitingToastShown = false
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
                        if (!waitingToastShown) {
                            showStepToast(message)
                            waitingToastShown = true
                        }
                        15_000L
                    }
                }
                delay(nextSyncDelayMs)
            }
            stopSelf()
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
            .setContentTitle("高德充电站采集终端")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    // CollectionEngine 的每个状态事件都会在手机前台弹出短提示，同时仍保留日志和常驻通知。
    private fun showStepToast(message: String) {
        if (message.isBlank()) return
        // 服务调度已移到工作线程；Toast 统一回主线程，避免后台/前台线程差异导致崩溃。
        mainHandler.post {
            Toast.makeText(applicationContext, message, Toast.LENGTH_SHORT).show()
        }
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
