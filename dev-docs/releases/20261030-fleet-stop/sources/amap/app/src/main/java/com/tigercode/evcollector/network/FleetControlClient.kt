package com.tigercode.evcollector.network

import android.content.Context
import android.os.Build
import com.tigercode.evcollector.AppPreferences
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import com.tigercode.evcollector.CollectorVersions
import com.tigercode.evcollector.CooldownAlarmScheduler
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import retrofit2.HttpException

class FleetStoppedException : CancellationException("收到全部停止指令")

object FleetControlClient {
    fun api(context: Context): ServerApi =
        RetrofitClient.serverApi(AppPreferences.serverUrl(context), context, controlPlane = true)

    private suspend fun register(context: Context, api: ServerApi) {
        val response = api.register(DeviceRegisterRequest(
            activationCode = AppPreferences.activationCode(context),
            deviceCode = AppPreferences.deviceCode(context),
            name = Build.MODEL,
            appVersion = CollectorVersions.APP_VERSION,
            parserVersion = CollectorVersions.PARSER_VERSION,
            targetAppVersion = "",
            capabilities = mapOf(
                "accessibilityConnected" to ChargingAccessibilityService.isConnected(),
                "targetPackage" to ChargingAccessibilityService.AMAP_PACKAGE,
                "model" to Build.MODEL,
                "fleetStopSupported" to true,
            ),
        ))
        check(response.deviceToken.isNotBlank()) { "设备注册未返回令牌" }
        AppPreferences.saveDeviceToken(context, response.deviceToken)
    }

    suspend fun control(context: Context, api: ServerApi, allowRegistration: Boolean = false): FleetControlDto {
        if (AppPreferences.deviceToken(context).isBlank()) {
            check(allowRegistration) { "等待设备注册" }
            register(context, api)
        }
        val response = try {
            api.deviceConfig()
        } catch (error: HttpException) {
            if (!allowRegistration || (error.code() != 401 && error.code() != 403)) throw error
            register(context, api)
            api.deviceConfig()
        }
        val control = response.fleetControl
        check(control?.supported == true) { "服务器尚未部署全部停止功能，请先更新服务器" }
        return control
    }

    // Called only from a user click, never from automatic recovery or polling.
    suspend fun resumeManually(context: Context) {
        val api = api(context)
        val control = control(context, api, allowRegistration = true)
        check(control.releaseComplete) { "全停任务仍在释放，请稍后再开始" }
        api.resumeDevice(FleetGenerationRequest(control.stopGeneration))
    }

    fun markStopped(context: Context) {
        AppPreferences.setScanning(context, false)
        CooldownAlarmScheduler.cancel(context)
        val message = "收到全部停止指令，本机已停止；任务由服务器释放，需手动开始"
        AppPreferences.setRemoteStatus(context, message)
        AppPreferences.appendLog(context, message)
        Handler(Looper.getMainLooper()).post { Toast.makeText(context, message, Toast.LENGTH_LONG).show() }
    }

    suspend fun stopAndAcknowledge(context: Context, api: ServerApi, generation: Long) {
        markStopped(context)
        withContext(NonCancellable) {
            withTimeoutOrNull(2_500L) {
                try {
                    api.acknowledgeStop(FleetGenerationRequest(generation))
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Exception) {
                    // Claim permission remains revoked even if the acknowledgement cannot arrive.
                }
            }
        }
    }

    suspend fun requireAllowed(context: Context, api: ServerApi) {
        val control = control(context, api)
        if (control.stopRequired || !control.releaseComplete) {
            stopAndAcknowledge(context, api, control.stopGeneration)
            throw FleetStoppedException()
        }
    }
}
