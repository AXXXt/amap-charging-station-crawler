package com.tigercode.evcollector.network

import kotlinx.coroutines.CancellationException

data class DeviceCommandReceipt(val key: String, val status: String, val error: String = "")

/** Commands are receipts, not a persistent instruction to auto-restart after a later stop. */
internal class DeviceCommandProcessor(
    private val mode: String,
    private val identity: () -> String,
    private val load: () -> DeviceCommandReceipt?,
    private val save: (DeviceCommandReceipt) -> Unit,
    private val readControl: suspend () -> FleetControlDto,
    private val stop: suspend () -> Unit,
    private val start: suspend () -> Unit,
    private val acknowledge: suspend (DeviceCommandDto, String, String) -> Unit,
) {
    private fun current(command: DeviceCommandDto, control: FleetControlDto): Boolean =
        control.supported && command.stopGeneration == control.stopGeneration &&
            control.deviceCommand?.commandId == command.commandId && control.deviceCommand?.status == "QUEUED"

    suspend fun apply(control: FleetControlDto) {
        val command = control.deviceCommand ?: return
        if (!current(command, control)) return
        if (command.action == "STOP") {
            stop()
            if (control.releaseComplete && command.releaseComplete) acknowledge(command, "APPLIED", "")
            return
        }
        if (command.action != "START" || control.stopRequired || !control.releaseComplete) return
        val key = "${identity()}|${command.stopGeneration}|${command.commandId}"
        val saved = load()?.takeIf { it.key == key }
        if (saved != null) {
            if (saved.status == "EXECUTING") {
                stop()
                val failed = DeviceCommandReceipt(key, "FAILED", "上次启动执行中断，请重新发送开始指令")
                save(failed)
                acknowledge(command, failed.status, failed.error)
            } else acknowledge(command, saved.status, saved.error)
            return
        }
        // Quiesce the previous engine and join its release work before changing collection mode.
        stop()
        val fresh = readControl()
        if (!current(command, fresh) || fresh.stopRequired || !fresh.releaseComplete) return
        save(DeviceCommandReceipt(key, "EXECUTING"))
        val receipt = try {
            check(command.mode == mode) { "采集模式不匹配，请更新 App 和采集服务器" }
            start()
            DeviceCommandReceipt(key, "APPLIED")
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            stop()
            DeviceCommandReceipt(key, "FAILED", (error.message ?: "启动失败").take(500))
        }
        save(receipt)
        val after = readControl()
        if (!current(command, after)) {
            if (after.stopRequired || !after.releaseComplete) stop()
            return
        }
        acknowledge(command, receipt.status, receipt.error)
    }
}
