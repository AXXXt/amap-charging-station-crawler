package com.tigercode.evcollector.network

import android.content.Context
import android.os.SystemClock
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorKeepAliveService
import com.tigercode.evcollector.CollectorVersions
import com.tigercode.evcollector.CooldownAlarmScheduler
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout

/** Lives with accessibility, so stopping collection does not stop command reception or heartbeat. */
object DeviceCommandClient {
    private val activityStops = linkedMapOf<Any, suspend () -> Unit>()

    fun attachActivity(owner: Any, stop: suspend () -> Unit) { activityStops[owner] = stop }
    fun detachActivity(owner: Any) { activityStops.remove(owner) }

    private suspend fun stopLocal(context: Context) {
        AppPreferences.setScanning(context, false)
        CooldownAlarmScheduler.cancel(context)
        for (stop in activityStops.values.toList()) stop()
        CollectorKeepAliveService.stopForRemote(context)
    }

    fun watch(context: Context, scope: CoroutineScope): Job = scope.launch {
        val app = context.applicationContext
        val prefs = app.getSharedPreferences("remote_device_commands", Context.MODE_PRIVATE)
        var apiServer = AppPreferences.serverUrl(app)
        var api = FleetControlClient.api(app)
        val processor = DeviceCommandProcessor(
            mode = "PRIORITY_DETAIL",
            identity = { AppPreferences.serverUrl(app) + "|" + AppPreferences.deviceCode(app) },
            load = {
                prefs.getString("receipt_key", null)?.let {
                    DeviceCommandReceipt(it, prefs.getString("receipt_status", "EXECUTING")!!, prefs.getString("receipt_error", "").orEmpty())
                }
            },
            save = { receipt ->
                check(prefs.edit().putString("receipt_key", receipt.key).putString("receipt_status", receipt.status)
                    .putString("receipt_error", receipt.error).commit()) { "无法保存设备执行回执" }
            },
            readControl = { FleetControlClient.control(app, api, allowRegistration = true) },
            stop = { stopLocal(app) },
            start = {
                check(ChargingAccessibilityService.isConnected()) { "无障碍未连接，请在手机上开启采集助手" }
                AppPreferences.setMonitorMode(app, true)
                AppPreferences.setPriorityStationMode(app, true)
                AppPreferences.setScanning(app, true)
                CollectorKeepAliveService.start(app)
                withTimeout(5_000L) {
                    while (!CollectorKeepAliveService.isCollectingServiceRunning()) delay(50L)
                }
                check(AppPreferences.isScanning(app)) { "采集服务未能启动，请查看手机端日志" }
                ChargingAccessibilityService.current()?.launchAmap()
                AppPreferences.setRemoteStatus(app, "监控台已启动重点站点详情采集")
                AppPreferences.appendLog(app, "收到监控台开始指令，已启动重点站点详情采集")
            },
            acknowledge = { command, status, error ->
                api.acknowledgeDeviceCommand(DeviceCommandAckRequest(command.commandId, status, error))
                if (command.action == "STOP") AppPreferences.setRemoteStatus(app, "监控台已停止本设备采集")
                if (status == "FAILED") {
                    AppPreferences.setRemoteStatus(app, "远程启动失败", error)
                    AppPreferences.appendLog(app, "远程启动失败：$error")
                }
            },
        )
        var lastGlobalStop = -1L
        var lastHeartbeat = 0L
        var lastErrorAt = 0L
        while (isActive) {
            try {
                val configuredServer = AppPreferences.serverUrl(app)
                if (apiServer != configuredServer) {
                    api = FleetControlClient.api(app)
                    apiServer = configuredServer
                    lastGlobalStop = -1L
                    lastHeartbeat = 0L
                }
                val control = FleetControlClient.control(app, api, allowRegistration = true)
                if (control.stopRequired || !control.releaseComplete) {
                    if (AppPreferences.isScanning(app) || lastGlobalStop != control.stopGeneration) {
                        stopLocal(app)
                        if (control.deviceCommand?.action != "STOP") FleetControlClient.markStopped(app)
                    }
                    if (control.fleetStopRequired && lastGlobalStop != control.stopGeneration) {
                        api.acknowledgeStop(FleetGenerationRequest(control.stopGeneration))
                        lastGlobalStop = control.stopGeneration
                    }
                }
                processor.apply(control)
                val now = SystemClock.elapsedRealtime()
                if (now - lastHeartbeat >= 15_000L) {
                    api.heartbeat(HeartbeatRequest(
                        status = if (AppPreferences.isScanning(app)) "RUNNING" else "STOPPED",
                        lastError = AppPreferences.remoteError(app),
                        appVersion = CollectorVersions.APP_VERSION,
                        parserVersion = CollectorVersions.PARSER_VERSION,
                        targetAppVersion = "",
                        capabilities = collectorCapabilities(app),
                    ))
                    lastHeartbeat = now
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val now = SystemClock.elapsedRealtime()
                if (now - lastErrorAt >= 60_000L) {
                    // Network errors never imply permission to start or resume.
                    AppPreferences.appendLog(app, "监控台指令连接暂不可用：${error.javaClass.simpleName}")
                    lastErrorAt = now
                }
            }
            delay(3_000L)
        }
    }
}
