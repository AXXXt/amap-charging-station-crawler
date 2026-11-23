package com.tigercode.evcollector.privileged

import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.IBinder
import android.util.Log
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.BuildConfig
import com.tigercode.evcollector.core.control.MapProcessPolicy
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import rikka.shizuku.Shizuku

object ShizukuMapController {
    const val MANAGER_PACKAGE = "moe.shizuku.privileged.api"
    val target = MapProcessPolicy.targetFor(BuildConfig.APPLICATION_ID)
    private val busy = AtomicBoolean(false)
    val isBusy: Boolean get() = busy.get()
    var lastMessage: String = ""
        private set

    enum class Availability { MISSING, STOPPED, OUTDATED, NEEDS_PERMISSION, READY }
    data class StopResult(val success: Boolean, val message: String)

    fun availability(context: Context): Availability = runCatching {
        if (Shizuku.pingBinder()) {
            when {
                Shizuku.getVersion() < 13 -> Availability.OUTDATED
                Shizuku.checkSelfPermission() != PackageManager.PERMISSION_GRANTED -> Availability.NEEDS_PERMISSION
                else -> Availability.READY
            }
        } else {
            @Suppress("DEPRECATION")
            val installed = runCatching { context.packageManager.getPackageInfo(MANAGER_PACKAGE, 0) }.isSuccess
            if (installed) Availability.STOPPED else Availability.MISSING
        }
    }.getOrDefault(Availability.STOPPED)

    /** Must be entered on main; only IPC/commands move to IO. No collector/task state is changed. */
    suspend fun forceStopMap(context: Context, collectionIsActive: () -> Boolean): StopResult =
        withContext(Dispatchers.Main.immediate) {
            if (!busy.compareAndSet(false, true)) return@withContext StopResult(false, "正在关闭地图，请稍候")
            val args = Shizuku.UserServiceArgs(ComponentName(context, MapControlUserService::class.java))
                .daemon(false)
                .processNameSuffix("map_control")
                .debuggable(BuildConfig.DEBUG)
                .version(BuildConfig.VERSION_CODE)
            var connection: ServiceConnection? = null
            try {
                check(!collectionIsActive()) { "本机正在采集或准备采集，请先停止本机采集" }
                check(availability(context) == Availability.READY) { "请先启动 Shizuku 并授权采集 App" }
                val service = withTimeout(12_000L) {
                    suspendCancellableCoroutine<IMapControlService> { continuation ->
                        val callback = object : ServiceConnection {
                            override fun onServiceConnected(name: ComponentName, binder: IBinder) {
                                if (continuation.isActive) continuation.resume(IMapControlService.Stub.asInterface(binder))
                            }
                            override fun onServiceDisconnected(name: ComponentName) {
                                if (continuation.isActive) continuation.resumeWithException(IllegalStateException("Shizuku 服务连接中断"))
                            }
                            override fun onNullBinding(name: ComponentName) {
                                if (continuation.isActive) continuation.resumeWithException(IllegalStateException("Shizuku 未返回控制服务"))
                            }
                            override fun onBindingDied(name: ComponentName) = onServiceDisconnected(name)
                        }
                        connection = callback
                        try {
                            Shizuku.bindUserService(args, callback)
                        } catch (error: Exception) {
                            if (continuation.isActive) continuation.resumeWithException(error)
                        }
                    }
                }
                // Collection may have changed while waiting for the privileged service.
                check(!collectionIsActive()) { "本机已开始采集，本次关闭已取消" }
                check(availability(context) == Availability.READY) { "Shizuku 授权或连接已失效" }
                val reply = withContext(Dispatchers.IO) { service.forceStopMap() }
                check(reply.getString("package") == target.packageName) { "关闭目标校验失败" }
                check(reply.getInt("executing_uid", -1) in listOf(0, 2000)) { "关闭服务身份校验失败" }
                StopResult(reply.getBoolean("success", false), reply.getString("message") ?: "未返回核验结果")
            } catch (error: TimeoutCancellationException) {
                StopResult(false, "Shizuku 控制服务启动超时，请检查 Shizuku 状态")
            } catch (error: CancellationException) {
                lastMessage = "关闭操作已中断，请重新核实地图状态"
                throw error
            } catch (error: Exception) {
                StopResult(false, "关闭失败：${error.message ?: error.javaClass.simpleName}")
            } finally {
                if (connection != null) {
                    runCatching { Shizuku.unbindUserService(args, connection, true) }
                    runCatching { Shizuku.unbindUserService(args, connection, false) }
                }
                busy.set(false)
            }.also { result ->
                lastMessage = result.message
                AppPreferences.appendLog(context.applicationContext, "Shizuku：${result.message}")
                Log.i("ShizukuMapControl", "APP_RESULT success=${result.success} ${result.message}")
            }
        }
}
