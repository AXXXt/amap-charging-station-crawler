package com.tigercode.evcollector.ui

import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.lifecycleScope
import com.tigercode.evcollector.privileged.ShizukuMapController
import com.tigercode.evcollector.privileged.ShizukuMapController.Availability
import kotlinx.coroutines.launch
import rikka.shizuku.Shizuku

class ShizukuControls(
    private val activity: AppCompatActivity,
    private val status: TextView,
    private val authorize: Button,
    private val stop: Button,
    private val collectionIsActive: () -> Boolean,
) : DefaultLifecycleObserver {
    private val binderReceived = Shizuku.OnBinderReceivedListener { refresh() }
    private val binderDead = Shizuku.OnBinderDeadListener { activity.runOnUiThread { refresh() } }
    private val permissionResult = Shizuku.OnRequestPermissionResultListener { code, result ->
        if (code == REQUEST_CODE) {
            Toast.makeText(activity,
                if (result == PackageManager.PERMISSION_GRANTED) "Shizuku 已授权" else "未获得 Shizuku 授权，可在 Shizuku 中允许",
                Toast.LENGTH_LONG).show()
            refresh()
        }
    }

    init {
        authorize.setOnClickListener { authorizeOrOpenManager() }
        stop.text = "关闭${ShizukuMapController.target.displayName}后台"
        stop.setOnClickListener {
            activity.lifecycleScope.launch {
                try {
                    val result = ShizukuMapController.forceStopMap(activity, collectionIsActive)
                    Toast.makeText(activity, result.message, Toast.LENGTH_LONG).show()
                } finally {
                    refresh()
                }
            }
        }
        activity.lifecycle.addObserver(this)
    }

    override fun onCreate(owner: LifecycleOwner) {
        Shizuku.addBinderReceivedListenerSticky(binderReceived)
        Shizuku.addBinderDeadListener(binderDead)
        Shizuku.addRequestPermissionResultListener(permissionResult)
        refresh()
    }

    override fun onResume(owner: LifecycleOwner) = refresh()

    override fun onDestroy(owner: LifecycleOwner) {
        Shizuku.removeBinderReceivedListener(binderReceived)
        Shizuku.removeBinderDeadListener(binderDead)
        Shizuku.removeRequestPermissionResultListener(permissionResult)
    }

    fun refresh() {
        val state = ShizukuMapController.availability(activity)
        val busy = ShizukuMapController.isBusy
        val collecting = collectionIsActive()
        val label = when (state) {
            Availability.MISSING -> "未安装，点击下方打开下载页"
            Availability.STOPPED -> "未运行，请在 Shizuku 中启动服务"
            Availability.OUTDATED -> "版本过低，请更新到 13 或更高版本"
            Availability.NEEDS_PERMISSION -> "已连接，尚未授权"
            Availability.READY -> "已连接、已授权"
        }
        status.text = buildString {
            append("Shizuku：$label")
            when {
                busy -> append("\n正在关闭地图并核验后台进程……")
                collecting -> append("\n遇滑块自动关闭高德，休息2分钟，打开后等待1分钟续采。")
                else -> append("\n可强行停止${ShizukuMapController.target.displayName}及其后台进程。")
            }
            append("\n非 root 手机重启后，需重新启动 Shizuku。")
            if (ShizukuMapController.lastMessage.isNotBlank()) {
                append("\n上次操作：${ShizukuMapController.lastMessage}")
            }
        }
        authorize.text = if (state == Availability.NEEDS_PERMISSION) "授权 Shizuku" else "打开 Shizuku"
        authorize.isEnabled = !busy
        stop.isEnabled = state == Availability.READY && !busy && !collecting
    }

    private fun authorizeOrOpenManager() {
        try {
            when (ShizukuMapController.availability(activity)) {
                Availability.NEEDS_PERMISSION -> Shizuku.requestPermission(REQUEST_CODE)
                Availability.MISSING -> activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://shizuku.rikka.app/download/")))
                else -> {
                    val intent = activity.packageManager.getLaunchIntentForPackage(ShizukuMapController.MANAGER_PACKAGE)
                    checkNotNull(intent) { "无法打开 Shizuku，请手动打开" }
                    activity.startActivity(intent)
                }
            }
        } catch (error: Exception) {
            Toast.makeText(activity, error.message ?: "无法申请 Shizuku 授权", Toast.LENGTH_LONG).show()
        }
    }

    companion object {
        private const val REQUEST_CODE = 4108
    }
}
