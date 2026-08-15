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
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var loopJob: Job? = null
    private var engine: CollectionEngine? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startAsForeground("采集服务正在启动")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP || !AppPreferences.isScanning(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        startAsForeground("采集服务正在运行")
        startSyncLoop()
        return START_STICKY
    }

    override fun onDestroy() {
        loopJob?.cancel()
        engine = null
        AppPreferences.setScanning(this, false)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startSyncLoop() {
        if (loopJob?.isActive == true) return
        val repository = CollectorRepository(this)
        val engine = CollectionEngine(this, repository)
        this.engine = engine
        loopJob = scope.launch {
            while (isActive && AppPreferences.isScanning(this@CollectorKeepAliveService)) {
                if (!ChargingAccessibilityService.isConnected()) {
                    AppPreferences.setRemoteStatus(
                        this@CollectorKeepAliveService,
                        "等待无障碍服务",
                        "请在系统设置中开启采集助手",
                    )
                    updateNotification("等待无障碍服务")
                    delay(5000)
                    continue
                }
                updateNotification("正在同步服务端")
                when (
                    val outcome = SyncManager(
                        this@CollectorKeepAliveService,
                        repository,
                        engine,
                    ).runOnce()
                ) {
                    is SyncOutcome.Completed -> {
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "任务完成，采集 ${outcome.stationCount} 个站点",
                        )
                        updateNotification("任务完成，采集 ${outcome.stationCount} 个站点")
                    }
                    is SyncOutcome.Failed -> {
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "同步失败",
                            outcome.message,
                        )
                        updateNotification("同步失败，等待重试")
                    }
                    is SyncOutcome.NoTask -> {
                        AppPreferences.setRemoteStatus(
                            this@CollectorKeepAliveService,
                            "等待任务",
                        )
                        updateNotification("等待任务")
                    }
                }
                delay(15_000)
            }
            stopSelf()
        }
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
    }
}
