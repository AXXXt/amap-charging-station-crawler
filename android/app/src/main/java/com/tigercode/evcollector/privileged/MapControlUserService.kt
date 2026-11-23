package com.tigercode.evcollector.privileged

import android.os.Binder
import android.os.Bundle
import android.os.Process
import android.util.Log
import androidx.annotation.Keep
import com.tigercode.evcollector.BuildConfig
import com.tigercode.evcollector.core.control.MapProcessPolicy
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Loaded by Shizuku as shell/root, not as a regular Android Service. */
@Keep
class MapControlUserService : IMapControlService.Stub() {
    override fun destroy() = exitProcess(0)

    @Synchronized
    override fun forceStopMap(): Bundle {
        val target = MapProcessPolicy.targetFor(BuildConfig.APPLICATION_ID)
        val executingUid = Process.myUid()
        val callerUid = Binder.getCallingUid()
        val userId = callerUid / 100_000
        val result = Bundle().apply {
            putString("package", target.packageName)
            putInt("executing_uid", executingUid)
            putInt("user_id", userId)
        }
        try {
            check(executingUid == 2000 || executingUid == 0) { "Shizuku 未提供 shell/root 身份" }
            Log.i(TAG, "STOP_REQUEST package=${target.packageName} user=$userId uid=$executingUid caller=$callerUid")
            command("强行停止", 6_000, "/system/bin/am", "force-stop", "--user", userId.toString(), target.packageName)
            val dump = command("读取停止状态", 6_000, "/system/bin/dumpsys", "package", target.packageName)
            val stopped = MapProcessPolicy.stoppedForUser(dump, userId)
            Log.i(TAG, "STOP_STATE package=${target.packageName} user=$userId stopped=$stopped")
            val processes = command("核实后台进程", 4_000, "/system/bin/ps", "-A", "-o", "UID,NAME")
            val remaining = MapProcessPolicy.runningMapProcesses(processes, target, userId)
            val success = stopped == true && remaining.isEmpty()
            val message = when {
                stopped == null -> "未能核实${target.displayName}的停止状态，请重试"
                !success -> "${target.displayName}关闭未通过核验：stopped=$stopped，残留进程=${remaining.size}"
                else -> "${target.displayName}已强行停止，后台残留进程 0 个"
            }
            result.putBoolean("success", success)
            result.putString("message", message)
            Log.i(TAG, "success=$success package=${target.packageName} user=$userId uid=$executingUid stopped=$stopped remaining=${remaining.size}")
        } catch (error: Exception) {
            result.putBoolean("success", false)
            result.putString("message", "关闭失败：${error.message ?: error.javaClass.simpleName}")
            Log.w(TAG, "Stop failed for ${target.packageName}", error)
        }
        return result
    }

    /** Argument arrays only; bounded execution and output, including error paths. */
    private fun command(label: String, timeoutMs: Long, vararg arguments: String): String {
        val process = ProcessBuilder(*arguments).redirectErrorStream(true).start()
        val reader = Executors.newSingleThreadExecutor { task ->
            Thread(task, "map-control-output").apply { isDaemon = true }
        }
        val output = reader.submit<String> {
            process.inputStream.bufferedReader().use { stream ->
                val text = StringBuilder()
                val buffer = CharArray(4096)
                while (true) {
                    val count = stream.read(buffer)
                    if (count < 0) break
                    if (text.length + count > 1_048_576) throw IOException("$label 输出过长，无法核验")
                    text.append(buffer, 0, count)
                }
                text.toString()
            }
        }
        try {
            if (!process.waitFor(timeoutMs, TimeUnit.MILLISECONDS)) {
                throw IOException("$label 超时")
            }
            val text = output.get(1_000, TimeUnit.MILLISECONDS)
            check(process.exitValue() == 0) { "$label 失败（退出码 ${process.exitValue()}）：${text.take(240)}" }
            return text
        } finally {
            if (process.isAlive) process.destroyForcibly()
            runCatching { process.inputStream.close() }
            runCatching { process.outputStream.close() }
            runCatching { process.errorStream.close() }
            output.cancel(true)
            reader.shutdownNow()
        }
    }

    companion object {
        private const val TAG = "ShizukuMapControl"
    }
}
