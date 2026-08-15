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
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorKeepAliveService
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.databinding.ActivityMainBinding
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
        engine = CollectionEngine(this, repository)
        engine.addListener(engineListener)

        binding.serverUrlInput.setText(AppPreferences.serverUrl(this))
        binding.activationCodeInput.setText(AppPreferences.activationCode(this))
        binding.deviceNameInput.setText(AppPreferences.deviceName(this))
        binding.cityInput.setText("郑州")
        binding.districtInput.setText("")
        binding.keywordInput.setText("")

        binding.saveConfigButton.setOnClickListener { saveConfig() }
        binding.openAccessibilityButton.setOnClickListener { openAccessibilitySettings() }
        binding.openAmapButton.setOnClickListener { openAmap() }
        binding.startCollectButton.setOnClickListener { startCollector() }
        binding.stopCollectButton.setOnClickListener { stopCollector() }
        binding.syncNowButton.setOnClickListener { syncNow() }
        binding.localScanButton.setOnClickListener { startLocalScan() }

        refreshHandler.post(refreshRunnable)
        refreshUi()
    }

    override fun onDestroy() {
        refreshHandler.removeCallbacks(refreshRunnable)
        engine.removeListener(engineListener)
        super.onDestroy()
    }

    private fun saveConfig() {
        AppPreferences.saveServerUrl(
            this,
            binding.serverUrlInput.text?.toString()?.trim().orEmpty(),
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
    }

    private fun startCollector() {
        saveConfig()
        requestNotificationPermissionIfNeeded()
        AppPreferences.setScanning(this, true)
        CollectorKeepAliveService.start(this)
        AppPreferences.setRemoteStatus(this, "设备调度已启动")
        refreshUi()
    }

    private fun stopCollector() {
        AppPreferences.setScanning(this, false)
        CollectorKeepAliveService.stop(this)
        AppPreferences.setRemoteStatus(this, "设备调度已停止")
        refreshUi()
    }

    private fun syncNow() {
        saveConfig()
        lifecycleScope.launch {
            AppPreferences.setRemoteStatus(this@MainActivity, "正在同步")
            refreshUi()
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

    private fun startLocalScan() {
        if (!ChargingAccessibilityService.isConnected()) {
            Toast.makeText(this, "请先开启无障碍服务", Toast.LENGTH_SHORT).show()
            openAccessibilitySettings()
            return
        }
        val city = binding.cityInput.text?.toString()?.trim().orEmpty().ifBlank { "郑州" }
        val district = binding.districtInput.text?.toString()?.trim().orEmpty()
        val keyword = binding.keywordInput.text?.toString()?.trim().orEmpty()
        engine.startLocal(city, district, keyword, engineListener)
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
        val accessibility = ChargingAccessibilityService.isConnected()
        val scanning = AppPreferences.isScanning(this)
        binding.serviceStatusText.text = buildString {
            append("无障碍服务: ")
            append(if (accessibility) "已连接" else "未开启")
            append("\n设备调度: ")
            append(if (scanning) "运行中" else "已停止")
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
