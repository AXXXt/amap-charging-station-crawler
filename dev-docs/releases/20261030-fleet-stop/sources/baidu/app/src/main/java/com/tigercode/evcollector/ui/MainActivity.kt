package com.tigercode.evcollector.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorKeepAliveService
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.data.ScanTaskEntity
import java.io.File
import com.tigercode.evcollector.databinding.ActivityMainBinding
import com.tigercode.evcollector.network.RetrofitClient
import com.tigercode.evcollector.network.HenanPoiImportRequest
import com.tigercode.evcollector.network.HenanTaskResetRequest
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.engine.CollectionListener
import com.tigercode.evcollector.engine.CollectionState
import com.tigercode.evcollector.network.FleetControlClient
import com.tigercode.evcollector.network.FleetStopRequest
import com.tigercode.evcollector.network.SyncManager
import com.tigercode.evcollector.network.SyncOutcome
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var repository: CollectorRepository
    private lateinit var engine: CollectionEngine

    private val refreshHandler = Handler(Looper.getMainLooper())
    private val refreshRunnable = object : Runnable {
        override fun run() {
            refreshUi()
            refreshHandler.postDelayed(this, 2000)
        }
    }

    private var localPending = 0
    private var localDone = 0
    private var localFailed = 0
    private var localSkipped = 0

    private var currentState = CollectionState.IDLE
    private var currentMessage = "待机"
    private var currentStation = ""
    private var stationCount = 0

    private val engineListener = CollectionListener { state, message, count, station ->
        currentState = state
        currentMessage = message
        currentStation = station
        stationCount = count
        refreshUi()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        repository = CollectorRepository(this)
        engine = CollectionEngine(this, repository) {
            SyncManager(this, repository, engine).uploadLocalResultsOnly()
        }
        engine.addListener(engineListener)

        binding.serverUrlInput.setText(AppPreferences.serverUrl(this))
        binding.activationCodeInput.setText(AppPreferences.activationCode(this))
        binding.deviceNameInput.setText(AppPreferences.deviceName(this))
        binding.cityInput.setText("")
        binding.districtInput.setText("")
        binding.keywordInput.setText("")

        binding.saveConfigButton.setOnClickListener { saveConfig() }
        binding.openAccessibilityButton.setOnClickListener { openAccessibilitySettings() }
        binding.openAmapButton.setOnClickListener { openAmap() }
        binding.startCollectButton.setOnClickListener { startWithFleetPermission { startCollector() } }
        binding.stopCollectButton.setOnClickListener { stopCollector() }
        binding.stopAllDevicesButton.setOnClickListener { stopAllDevices() }
        binding.syncNowButton.setOnClickListener { startWithFleetPermission { syncNow() } }
        binding.importHenanPoiButton.setOnClickListener { importHenanPois() }
        binding.startHenanCollectButton.setOnClickListener { startWithFleetPermission { startHenanCollect() } }
        binding.startMonitorCollectButton.setOnClickListener { startWithFleetPermission { startMonitorCollect() } }
        binding.startMonitorListButton.setOnClickListener { startWithFleetPermission { startMonitorListCollect() } }
        binding.startMonitorCollectButton.visibility = android.view.View.GONE
        binding.startMonitorListButton.visibility = android.view.View.GONE
        binding.resetHenanTasksButton.setOnClickListener { confirmResetHenanTasks() }
        binding.localScanButton.setOnClickListener { startWithFleetPermission { startLocalScan() } }
        binding.stopRunButton.setOnClickListener { stopRunning() }
        binding.clearLogButton.setOnClickListener { clearLogs() }
        binding.exportResultsButton.setOnClickListener { exportResults() }

        lifecycleScope.launch {
            seedLocalTasksIfNeeded()
            while (true) {
                localPending = repository.pendingCount()
                localDone = repository.succeededCount()
                localFailed = repository.failedCount()
                localSkipped = repository.skippedCount()
                delay(3000L)
            }
        }

        // 升级安装/系统回收后，只要用户仍处于“设备调度”状态，打开 App 就自动恢复服务。
        if (AppPreferences.isScanning(this)) {
            requestNotificationPermissionIfNeeded()
            CollectorKeepAliveService.start(this)
        }

        refreshHandler.post(refreshRunnable)
        refreshUi()
    }

    override fun onDestroy() {
        refreshHandler.removeCallbacks(refreshRunnable)
        engine.removeListener(engineListener)
        engine.stop()
        super.onDestroy()
    }

    private fun saveConfig(): Boolean {
        val serverUrl = binding.serverUrlInput.text?.toString()?.trim().orEmpty()
        val normalizedServerUrl = AppPreferences.normalizeServerUrl(serverUrl)
        if (normalizedServerUrl == null) {
            Toast.makeText(this, "服务端地址必须以 http:// 或 https:// 开头", Toast.LENGTH_LONG).show()
            AppPreferences.appendLog(this, "配置保存失败：服务端地址格式错误")
            binding.serverUrlInput.setText(AppPreferences.serverUrl(this))
            refreshUi()
            return false
        }
        AppPreferences.saveServerUrl(
            this,
            normalizedServerUrl,
        )
        AppPreferences.saveActivationCode(
            this,
            binding.activationCodeInput.text?.toString().orEmpty(),
        )
        AppPreferences.saveDeviceName(
            this,
            binding.deviceNameInput.text?.toString()?.trim().orEmpty(),
        )
        AppPreferences.appendLog(this, "配置已保存")
        refreshUi()
        return true
    }

    private var fleetStartInProgress = false

    private fun startWithFleetPermission(action: () -> Unit) {
        if (fleetStartInProgress) return
        if (AppPreferences.isScanning(this) || engine.isRunning()) {
            Toast.makeText(this, "本机正在采集，请先停止后再开始", Toast.LENGTH_SHORT).show()
            return
        }
        if (!saveConfig()) return
        fleetStartInProgress = true
        lifecycleScope.launch {
            try {
                FleetControlClient.resumeManually(this@MainActivity)
                action()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val message = "无法开始：${error.message ?: error.javaClass.simpleName}"
                AppPreferences.setRemoteStatus(this@MainActivity, message)
                AppPreferences.appendLog(this@MainActivity, message)
                Toast.makeText(this@MainActivity, message, Toast.LENGTH_LONG).show()
            } finally {
                fleetStartInProgress = false
                refreshUi()
            }
        }
    }

    private fun stopAllDevices() {
        if (!saveConfig()) return
        binding.stopAllDevicesButton.isEnabled = false
        lifecycleScope.launch {
            val prefs = getSharedPreferences("fleet_control", MODE_PRIVATE)
            // Keep the same ID after timeout/restart so a retry cannot issue a second stop.
            val requestId = prefs.getString("pending_stop_request", null)
                ?: java.util.UUID.randomUUID().toString().also {
                    prefs.edit().putString("pending_stop_request", it).commit()
                }
            try {
                val api = FleetControlClient.api(this@MainActivity)
                val control = FleetControlClient.control(this@MainActivity, api, allowRegistration = true)
                check(control.canStopAll) { "本机未获全部停止权限，请由K60百度采集端操作" }
                AppPreferences.setRemoteStatus(this@MainActivity, "正在通知全部设备停止并释放任务")
                refreshUi()
                val response = api.stopAllDevices(FleetStopRequest(requestId))
                check(response.status == "STOP_REQUESTED") { "服务器未确认全停指令" }
                prefs.edit().remove("pending_stop_request").commit()
                engine.stop()
                stopCollector()
                val message = "全停指令已发送，释放 ${response.releasedTaskCount} 条任务；其他设备联网后停止，需手动开始"
                AppPreferences.setRemoteStatus(this@MainActivity, message)
                AppPreferences.appendLog(this@MainActivity, message)
                Toast.makeText(this@MainActivity, message, Toast.LENGTH_LONG).show()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val message = "全停尚未确认，请重试：${error.message ?: error.javaClass.simpleName}"
                AppPreferences.setRemoteStatus(this@MainActivity, message)
                AppPreferences.appendLog(this@MainActivity, message)
                Toast.makeText(this@MainActivity, message, Toast.LENGTH_LONG).show()
            } finally {
                binding.stopAllDevicesButton.isEnabled = true
                refreshUi()
            }
        }
    }

    private fun startCollector() {
        startHenanCollect()
    }

    private fun stopCollector() {
        AppPreferences.setLocalQueueMode(this, false)
        AppPreferences.setMonitorMode(this, false)
        AppPreferences.setScanning(this, false)
        CollectorKeepAliveService.stop(this)
        AppPreferences.setRemoteStatus(this, "设备调度已停止")
        refreshUi()
    }

    private fun syncNow() {
        if (!saveConfig()) return
        lifecycleScope.launch {
            AppPreferences.setRemoteStatus(this@MainActivity, "正在同步")
            refreshUi()
            engine.awaitReadyForNextTask()
            when (
                val outcome = SyncManager(this@MainActivity, repository, engine).runOnce()
            ) {
                is SyncOutcome.Completed -> {
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "同步完成，采集 ${outcome.stationCount} 个站点",
                    )
                    AppPreferences.appendLog(
                        this@MainActivity,
                        "同步完成，采集 ${outcome.stationCount} 个站点",
                    )
                }
                is SyncOutcome.Skipped -> {
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "任务已跳过：${outcome.stationName}",
                        outcome.reason,
                    )
                    AppPreferences.appendLog(
                        this@MainActivity,
                        "任务已跳过：${outcome.stationName}（${outcome.reason}）",
                    )
                }
                is SyncOutcome.DetailRiskReleased -> {
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "详情页风控，任务已释放",
                        outcome.message,
                    )
                    AppPreferences.appendLog(
                        this@MainActivity,
                        "详情页风控，任务已释放: ${outcome.message}",
                    )
                }
                is SyncOutcome.HumanVerification -> {
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "人机验证，任务已释放",
                        outcome.message,
                    )
                    AppPreferences.appendLog(
                        this@MainActivity,
                        "人机验证，任务已释放，返回列表继续: ${outcome.message}",
                    )
                }
                is SyncOutcome.Failed -> {
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "同步失败",
                        outcome.message,
                    )
                    AppPreferences.appendLog(this@MainActivity, "同步失败: ${outcome.message}")
                }
                is SyncOutcome.NoTask -> {
                    AppPreferences.setRemoteStatus(this@MainActivity, "暂无待领取任务")
                    AppPreferences.appendLog(this@MainActivity, "同步完成，暂无任务")
                }
            }
            refreshUi()
        }
    }

    private fun importHenanPois() {
        if (!saveConfig()) return
        binding.importHenanPoiButton.isEnabled = false
        AppPreferences.setRemoteStatus(this, "正在按地市导入河南候选站点")
        AppPreferences.appendLog(this, "启动后台导入：郑州优先，随后按地市继续导入")
        refreshUi()
        lifecycleScope.launch {
            try {
                val api = RetrofitClient.serverApi(
                    AppPreferences.serverUrl(this@MainActivity),
                    this@MainActivity,
                )
                var status = api.importHenanPois(
                    adminKey = "dev-admin-key",
                    body = HenanPoiImportRequest(),
                )
                while (status.status == "RUNNING" && !status.ready) {
                    val city = status.currentCity.ifBlank { "首个地市" }
                    AppPreferences.setRemoteStatus(
                        this@MainActivity,
                        "正在导入${city}，完成后即可开始采集",
                    )
                    refreshUi()
                    delay(2_000)
                    status = api.henanPoiImportStatus("dev-admin-key")
                }
                if (status.ready) {
                    val message = "${status.message}；已新增 ${status.created} 条任务"
                    AppPreferences.setRemoteStatus(this@MainActivity, message)
                    AppPreferences.appendLog(
                        this@MainActivity,
                        "$message（进度 ${status.citiesCompleted}/${status.citiesTotal}，后台持续导入）",
                    )
                    AppPreferences.setHenanImportReady(this@MainActivity, true)
                    Toast.makeText(
                        this@MainActivity,
                        "首个地市已导入，可边采集边继续导入",
                        Toast.LENGTH_LONG,
                    ).show()
                } else {
                    val message = status.message.ifBlank { "河南POI导入未生成可采集任务" }
                    throw IllegalStateException(message)
                }
            } catch (error: Exception) {
                val message = error.message ?: error.javaClass.simpleName
                AppPreferences.setRemoteStatus(this@MainActivity, "导入失败", message)
                AppPreferences.appendLog(this@MainActivity, "河南POI导入失败: $message")
            } finally {
                binding.importHenanPoiButton.isEnabled = true
                refreshUi()
            }
        }
    }

    private fun startHenanCollect() {
        if (!saveConfig()) return
        if (!ChargingAccessibilityService.isConnected()) {
            Toast.makeText(this, "请先开启无障碍服务", Toast.LENGTH_SHORT).show()
            openAccessibilitySettings()
            return
        }
        requestNotificationPermissionIfNeeded()
        AppPreferences.setLocalQueueMode(this, false)
        AppPreferences.setMonitorMode(this, false)
        AppPreferences.setScanning(this, true)
        CollectorKeepAliveService.start(this)
        AppPreferences.setMonitorListMode(this, false)
        AppPreferences.setRemoteStatus(this, "百度499站循环采集已启动")
        AppPreferences.appendLog(this, "已启动百度499站固定名单循环调度，每轮结束后开始下一轮")
        ChargingAccessibilityService.current()?.launchAmap()
        refreshUi()
    }

    private suspend fun seedLocalTasksIfNeeded() {
        try {
            if (repository.taskCount() > 0) return
            val text = assets.open("baidu_tasks.json").bufferedReader().use { it.readText() }
            val array = org.json.JSONArray(text)
            val now = System.currentTimeMillis()
            val tasks = (0 until array.length()).map { index ->
                val item = array.getJSONObject(index)
                ScanTaskEntity(
                    stationId = item.getString("stationId"),
                    name = item.getString("name"),
                    address = item.optString("address"),
                    latitude = if (item.isNull("latitude")) null else item.getDouble("latitude"),
                    longitude = if (item.isNull("longitude")) null else item.getDouble("longitude"),
                    payloadJson = "",
                    status = "pending",
                    attempts = 0,
                    maxAttempts = 3,
                    leaseToken = "",
                    leaseExpiresAt = 0L,
                    resultJson = null,
                    lastError = "",
                    createdAt = now + index,
                    updatedAt = now,
                )
            }
            repository.seedTasks(tasks)
            AppPreferences.appendLog(this, "已导入本地任务 ${tasks.size} 条")
        } catch (error: Exception) {
            AppPreferences.appendLog(this, "本地任务导入失败: ${error.message}")
        }
    }

    private fun exportResults() {
        lifecycleScope.launch {
            try {
                val results = repository.allResultJson()
                if (results.isEmpty()) {
                    Toast.makeText(this@MainActivity, "暂无可导出的采集结果", Toast.LENGTH_SHORT).show()
                    return@launch
                }
                val file = File(getExternalFilesDir(null), "baidu_results_${System.currentTimeMillis()}.json")
                file.writeText("[\n" + results.joinToString(",\n") + "\n]")
                val uri = androidx.core.content.FileProvider.getUriForFile(
                    this@MainActivity,
                    "$packageName.fileprovider",
                    file,
                )
                val intent = Intent(Intent.ACTION_SEND).apply {
                    type = "application/json"
                    putExtra(Intent.EXTRA_STREAM, uri)
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
                startActivity(Intent.createChooser(intent, "导出采集结果"))
                AppPreferences.appendLog(this@MainActivity, "已导出 ${results.size} 条结果: ${file.absolutePath}")
            } catch (error: Exception) {
                Toast.makeText(this@MainActivity, "导出失败: ${error.message}", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun startMonitorCollect() {
        startHenanCollect()
    }

    private fun startMonitorListCollect() {
        startHenanCollect()
    }

    private fun confirmResetHenanTasks() {
        AlertDialog.Builder(this)
            .setTitle("确认重置任务表")
            .setMessage(
                "将把全部河南任务重置为待领取，并清空租约、尝试次数和进度。\n\n" +
                    "结果表和快照表不会删除。建议先停止其他正在采集的手机。"
            )
            .setNegativeButton("取消", null)
            .setPositiveButton("确认重置") { _, _ ->
                if (AppPreferences.isScanning(this)) stopCollector()
                resetHenanTasks()
            }
            .show()
    }

    private fun resetHenanTasks() {
        if (!saveConfig()) return
        binding.resetHenanTasksButton.isEnabled = false
        AppPreferences.setRemoteStatus(this, "正在重置任务表")
        refreshUi()
        lifecycleScope.launch {
            try {
                val api = RetrofitClient.serverApi(
                    AppPreferences.serverUrl(this@MainActivity),
                    this@MainActivity,
                )
                val response = api.resetHenanTasks(
                    adminKey = "dev-admin-key",
                    body = HenanTaskResetRequest(),
                )
                val pending = response.statusCounts["PENDING"] ?: response.total
                val message = "任务表已重置：共 ${response.total} 条，待领取 $pending 条"
                AppPreferences.setRemoteStatus(this@MainActivity, message)
                AppPreferences.appendLog(this@MainActivity, message)
                Toast.makeText(this@MainActivity, message, Toast.LENGTH_LONG).show()
            } catch (error: Exception) {
                val message = error.message ?: error.javaClass.simpleName
                AppPreferences.setRemoteStatus(this@MainActivity, "任务表重置失败", message)
                AppPreferences.appendLog(this@MainActivity, "任务表重置失败: $message")
                Toast.makeText(this@MainActivity, "重置失败: $message", Toast.LENGTH_LONG).show()
            } finally {
                binding.resetHenanTasksButton.isEnabled = true
                refreshUi()
            }
        }
    }

    private fun startLocalScan() {
        if (!ChargingAccessibilityService.isConnected()) {
            Toast.makeText(this, "请先开启无障碍服务", Toast.LENGTH_SHORT).show()
            openAccessibilitySettings()
            return
        }
        val city = binding.cityInput.text?.toString()?.trim().orEmpty()
        val district = binding.districtInput.text?.toString()?.trim().orEmpty()
        val keyword = binding.keywordInput.text?.toString()?.trim().orEmpty()
        engine.startLocal(city, district, keyword, engineListener)
        lifecycleScope.launch {
            val api = FleetControlClient.api(this@MainActivity)
            do {
                delay(3_000L)
                try {
                    FleetControlClient.requireAllowed(this@MainActivity, api)
                } catch (error: com.tigercode.evcollector.network.FleetStoppedException) {
                    engine.stop()
                    refreshUi()
                    return@launch
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Exception) {
                    // Retry while the local collector is active.
                }
            } while (engine.isRunning())
        }
        refreshUi()
    }

    private fun stopRunning() {
        engine.stop()
        AppPreferences.setMonitorMode(this, false)
        AppPreferences.setScanning(this, false)
        CollectorKeepAliveService.stop(this)
        AppPreferences.setRemoteStatus(this, "运行已停止")
        AppPreferences.appendLog(this, "用户手动停止运行")
        refreshUi()
    }

    private fun clearLogs() {
        AppPreferences.clearLogs(this)
        refreshUi()
    }

    private fun openAccessibilitySettings() {
        try {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        } catch (error: Exception) {
            Toast.makeText(this, "无法打开无障碍设置", Toast.LENGTH_SHORT).show()
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 100)
        }
    }

    private fun openAmap() {
        val service = ChargingAccessibilityService.current()
        if (service != null) {
            service.launchAmap()
            return
        }
        val launchIntent = packageManager.getLaunchIntentForPackage(
            ChargingAccessibilityService.AMAP_PACKAGE
        )
        if (launchIntent != null) {
            startActivity(launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } else {
            try {
                startActivity(
                    Intent(
                        Intent.ACTION_VIEW,
                        Uri.parse("market://details?id=${ChargingAccessibilityService.AMAP_PACKAGE}")
                    )
                )
            } catch (_: Exception) {
                Toast.makeText(this, "未安装高德地图", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun refreshUi() {
        if (!::binding.isInitialized) return
        val accessibilityConnected = ChargingAccessibilityService.isConnected()
        val accessibilityEnabled = ChargingAccessibilityService.isEnabled(this)
        val scanning = AppPreferences.isScanning(this)
        binding.startHenanCollectButton.isEnabled = !scanning
        binding.startMonitorCollectButton.isEnabled = !scanning
        binding.startMonitorListButton.isEnabled = !scanning
        binding.serviceStatusText.text = buildString {
            append("无障碍服务: ")
            append(
                when {
                    accessibilityConnected -> "已连接"
                    accessibilityEnabled -> "已开启，等待系统连接"
                    else -> "未开启"
                }
            )
            append("\n设备调度: ")
            append(if (scanning) "运行中" else "已停止")
            append("\n采集模式: ")
            append(if (AppPreferences.isMonitorMode(this@MainActivity)) "重点站点小时监控" else "普通任务调度")
            append("\n本地任务: 待采集 ")
            append(localPending)
            append(" / 已完成 ")
            append(localDone)
            append(" / 失败 ")
            append(localFailed)
            append(" / 跳过 ")
            append(localSkipped)
            append("\n远程状态: ")
            append(AppPreferences.remoteStatus(this@MainActivity).ifBlank { "未同步" })
            if (AppPreferences.remoteError(this@MainActivity).isNotBlank()) {
                append("\n错误: ")
                append(AppPreferences.remoteError(this@MainActivity))
            }
        }
        binding.statusText.text = buildString {
            append("当前状态: ")
            append(currentState.label)
            append("\n")
            append(currentMessage)
            append("\n已采集站点: ")
            append(stationCount)
            if (currentStation.isNotBlank()) {
                append("\n当前站点: ")
                append(currentStation)
            }
        }
        binding.logText.text = AppPreferences.logs(this)
            .takeLast(40)
            .joinToString("\n")
            .ifBlank { "暂无日志" }
    }
}
