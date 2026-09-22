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
import com.tigercode.evcollector.databinding.ActivityMainBinding
import com.tigercode.evcollector.network.RetrofitClient
import com.tigercode.evcollector.network.HenanPoiImportRequest
import com.tigercode.evcollector.network.HenanTaskResetRequest
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.engine.CollectionListener
import com.tigercode.evcollector.engine.CollectionState
import com.tigercode.evcollector.network.SyncManager
import com.tigercode.evcollector.network.SyncOutcome
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
        binding.startCollectButton.setOnClickListener { startCollector() }
        binding.stopCollectButton.setOnClickListener { stopCollector() }
        binding.syncNowButton.setOnClickListener { syncNow() }
        binding.importHenanPoiButton.setOnClickListener { importHenanPois() }
        binding.startHenanCollectButton.setOnClickListener { startHenanCollect() }
        binding.startMonitorCollectButton.setOnClickListener { startMonitorCollect() }
        binding.resetHenanTasksButton.setOnClickListener { confirmResetHenanTasks() }
        binding.localScanButton.setOnClickListener { startLocalScan() }
        binding.stopRunButton.setOnClickListener { stopRunning() }
        binding.clearLogButton.setOnClickListener { clearLogs() }

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

    private fun startCollector() {
        if (!saveConfig()) return
        requestNotificationPermissionIfNeeded()
        AppPreferences.setMonitorMode(this, false)
        AppPreferences.setScanning(this, true)
        CollectorKeepAliveService.start(this)
        AppPreferences.setRemoteStatus(this, "设备调度已启动")
        refreshUi()
    }

    private fun stopCollector() {
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
        if (!ChargingAccessibilityService.isConnected()) {
            Toast.makeText(this, "请先开启无障碍服务", Toast.LENGTH_SHORT).show()
            openAccessibilitySettings()
            return
        }
        requestNotificationPermissionIfNeeded()
        AppPreferences.setMonitorMode(this, false)
        AppPreferences.setScanning(this, true)
        CollectorKeepAliveService.start(this)
        AppPreferences.setRemoteStatus(this, "河南站点采集已启动")
        AppPreferences.appendLog(this, "已启动河南省重卡站点采集调度")
        ChargingAccessibilityService.current()?.launchAmap()
        refreshUi()
    }

    private fun startMonitorCollect() {
        if (!saveConfig()) return
        if (!ChargingAccessibilityService.isConnected()) {
            Toast.makeText(this, "请先开启无障碍服务", Toast.LENGTH_SHORT).show()
            openAccessibilitySettings()
            return
        }
        requestNotificationPermissionIfNeeded()
        AppPreferences.setMonitorMode(this, true)
        AppPreferences.setScanning(this, true)
        CollectorKeepAliveService.start(this)
        AppPreferences.setRemoteStatus(this, "重点站点小时监控已启动")
        AppPreferences.appendLog(this, "已启动重点站点小时监控滚动调度")
        ChargingAccessibilityService.current()?.launchAmap()
        refreshUi()
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
