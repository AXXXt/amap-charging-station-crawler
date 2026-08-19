package com.tigercode.evcollector.network

import android.content.Context
import android.os.Build
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorVersions
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.engine.CollectionEngine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SyncManager(
    private val context: Context,
    private val repository: CollectorRepository,
    private val engine: CollectionEngine,
) {
    suspend fun runOnce(): SyncOutcome {
        return withContext(Dispatchers.IO) {
            val baseUrl = AppPreferences.serverUrl(context)
            val api = RetrofitClient.serverApi(baseUrl, context)
            val deviceCode = AppPreferences.deviceCode(context)
            val activationCode = AppPreferences.activationCode(context)
            var claimedTask: RemoteTaskDto? = null
            try {
                val registerResponse = api.register(
                    DeviceRegisterRequest(
                        activationCode = activationCode,
                        deviceCode = deviceCode,
                        name = Build.MODEL,
                        appVersion = CollectorVersions.APP_VERSION,
                        parserVersion = CollectorVersions.PARSER_VERSION,
                        targetAppVersion = "",
                        capabilities = mapOf(
                            "accessibilityConnected" to ChargingAccessibilityService.isConnected(),
                            "targetPackage" to ChargingAccessibilityService.AMAP_PACKAGE,
                            "sdkInt" to Build.VERSION.SDK_INT,
                            "manufacturer" to Build.MANUFACTURER,
                            "model" to Build.MODEL,
                        ),
                    )
                )
                if (registerResponse.deviceToken.isNotBlank()) {
                    AppPreferences.saveDeviceToken(context, registerResponse.deviceToken)
                }
                api.heartbeat(
                    HeartbeatRequest(
                        status = if (engine.isRunning()) "RUNNING" else "IDLE",
                        lastError = "",
                        appVersion = CollectorVersions.APP_VERSION,
                        parserVersion = CollectorVersions.PARSER_VERSION,
                        targetAppVersion = "",
                        capabilities = mapOf(
                            "accessibilityConnected" to ChargingAccessibilityService.isConnected(),
                            "targetPackage" to ChargingAccessibilityService.AMAP_PACKAGE,
                        ),
                    )
                )

                val claim = api.claimTask(ClaimTaskRequest(deviceCode))
                val task = claim.task
                claimedTask = task
                if (task == null) {
                    uploadPendingResults(api, deviceCode, "")
                    return@withContext SyncOutcome.NoTask
                }

                api.acknowledgeTask(
                    task.id,
                    TaskActionRequest(deviceCode = deviceCode, leaseToken = task.leaseToken),
                )
                val taskStartedAt = System.currentTimeMillis()
                val details = engine.runRegion(
                    city = task.city,
                    district = task.district,
                    keyword = task.keyword,
                )
                val summary = mapOf(
                    "stations" to details.size,
                    "city" to task.city,
                    "district" to task.district,
                )
                api.completeTask(
                    task.id,
                    TaskActionRequest(
                        deviceCode = deviceCode,
                        leaseToken = task.leaseToken,
                        resultSummary = summary,
                    ),
                )
                uploadPendingResults(
                    api = api,
                    deviceCode = deviceCode,
                    taskId = claimedTask?.id.orEmpty(),
                    taskStartedAt = taskStartedAt,
                )
                SyncOutcome.Completed(details.size)
            } catch (error: Exception) {
                claimedTask?.let { task ->
                    try {
                        api.failTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                leaseToken = task.leaseToken,
                                errorCode = "COLLECTION_FAILED",
                                errorMessage = error.message ?: error.javaClass.simpleName,
                                retryable = true,
                            ),
                        )
                    } catch (_: Exception) {
                        // Local failure is reported through SyncOutcome.
                    }
                }
                SyncOutcome.Failed(error.message ?: error.javaClass.simpleName)
            }
        }
    }

    private suspend fun uploadPendingResults(
        api: ServerApi,
        deviceCode: String,
        taskId: String,
        taskStartedAt: Long? = null,
    ) {
        val pending = repository.pendingResults().filter { item ->
            taskId.isBlank() || taskStartedAt == null || item.createdAt >= taskStartedAt
        }
        if (pending.isEmpty()) return
        val observations = pending.map { item ->
            mapOf(
                "observationId" to item.idempotencyKey,
                "stationId" to item.stationId,
                "taskId" to taskId,
                "deviceId" to deviceCode,
                "payload" to item.resultJson,
            )
        }
        val response = api.uploadObservations(
            ObservationUploadRequest(deviceCode = deviceCode, observations = observations)
        )
        if (response.accepted + response.duplicates >= pending.size) {
            pending.forEach { repository.markUploaded(it.idempotencyKey) }
        }
    }
}

sealed class SyncOutcome {
    data class Completed(val stationCount: Int) : SyncOutcome()
    data class Failed(val message: String) : SyncOutcome()
    object NoTask : SyncOutcome()
}
