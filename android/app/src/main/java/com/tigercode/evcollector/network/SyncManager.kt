package com.tigercode.evcollector.network

import android.content.Context
import android.os.Build
import com.google.gson.JsonParser
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorVersions
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.engine.StationNotFoundException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import retrofit2.HttpException
import java.util.concurrent.atomic.AtomicBoolean
import java.io.EOFException
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException

private const val SITE_STATION_DETAIL_TASK = "SITE_STATION_DETAIL"
private const val HENAN_POI_DETAIL_TASK = "HENAN_POI_DETAIL"

class SyncManager(
    private val context: Context,
    private val repository: CollectorRepository,
    private val engine: CollectionEngine,
) {
    suspend fun runOnce(): SyncOutcome {
        return withContext(Dispatchers.IO) {
            val deviceCode = AppPreferences.deviceCode(context)
            val activationCode = AppPreferences.activationCode(context)
            val collectionMode = if (AppPreferences.isMonitorMode(context)) "MONITOR" else "NORMAL"
            var claimedTask: RemoteTaskDto? = null
            var api: ServerApi? = null
            var leaseRenewJob: Job? = null
            val leaseLost = AtomicBoolean(false)
            try {
                api = RetrofitClient.serverApi(AppPreferences.serverUrl(context), context)
                val activeApi = api
                val registerResponse = activeApi.register(
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
                activeApi.heartbeat(
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

                val claim = activeApi.claimTask(ClaimTaskRequest(deviceCode, collectionMode))
                val task = claim.task
                claimedTask = task
                if (task == null) {
                    uploadPendingResults(
                        api = activeApi,
                        deviceCode = deviceCode,
                        taskId = "",
                        leaseToken = "",
                        remoteTasksOnly = true,
                    )
                    return@withContext SyncOutcome.NoTask
                }

                if (claim.reason == "CLAIMED_FAILED_RETRY") {
                    val retryNumber = task.recoveryAttempt.coerceAtLeast(1)
                    val retryLimit = task.maxRecoveryAttempts.coerceAtLeast(retryNumber)
                    val message = "常规任务已完成，正在补采失败站点（第${retryNumber}/${retryLimit}次）"
                    AppPreferences.setRemoteStatus(context, message)
                    AppPreferences.appendLog(context, "$message: ${task.stationName.ifBlank { task.keyword }}")
                }

                activeApi.acknowledgeTask(
                    task.id,
                    TaskActionRequest(deviceCode = deviceCode, mode = collectionMode, leaseToken = task.leaseToken),
                )
                leaseRenewJob = startLeaseRenewal(activeApi, deviceCode, collectionMode, task, leaseLost)
                try {
                    val taskStartedAt = System.currentTimeMillis()
                    val details = if (
                        task.type == SITE_STATION_DETAIL_TASK ||
                        task.type == HENAN_POI_DETAIL_TASK
                    ) {
                        val requestedName = task.stationName.ifBlank { task.keyword }
                        val detail = engine.runStationTask(
                            stationId = task.stationId,
                            stationName = requestedName,
                            address = task.stationAddress,
                            latitude = task.stationLatitude,
                            longitude = task.stationLongitude,
                            remoteTaskId = task.id,
                        ) ?: throw IllegalStateException("未采集到目标站点: $requestedName")
                        listOf(detail)
                    } else {
                        engine.runRegion(
                            city = task.city,
                            district = task.district,
                            keyword = task.keyword,
                        )
                    }
                    if (leaseLost.get()) {
                        throw IllegalStateException("任务租约已失效，停止提交旧任务结果: ${task.id}")
                    }
                    val summary = linkedMapOf<String, Any>(
                        "stations" to details.size,
                        "type" to task.type,
                        "city" to task.city,
                        "district" to task.district,
                    )
                    if (
                        task.type == SITE_STATION_DETAIL_TASK ||
                        task.type == HENAN_POI_DETAIL_TASK
                    ) {
                        summary["sourceSiteId"] = task.sourceSiteId
                        summary["sourceSiteOrder"] = task.sourceSiteOrder
                        summary["stationId"] = task.stationId
                        summary["stationName"] = task.stationName.ifBlank { task.keyword }
                        summary["stationSequence"] = task.stationSequence
                    }
                    val uploadedCount = uploadPendingResults(
                        api = activeApi,
                        deviceCode = deviceCode,
                        taskId = task.id,
                        taskStartedAt = taskStartedAt,
                        leaseToken = task.leaseToken,
                    )
                    if (details.isNotEmpty() && uploadedCount == 0) {
                        throw IllegalStateException(
                            "当前任务采集到 ${details.size} 条结果，但没有结果成功上传，禁止标记任务完成"
                        )
                    }
                    AppPreferences.appendLog(
                        context,
                        "当前任务结果已由服务端确认: $uploadedCount 条",
                    )
                    activeApi.completeTask(
                        task.id,
                        TaskActionRequest(
                            deviceCode = deviceCode,
                            mode = collectionMode,
                            leaseToken = task.leaseToken,
                            resultSummary = summary,
                        ),
                    )
                    SyncOutcome.Completed(details.size)
                } finally {
                    leaseRenewJob?.cancelAndJoin()
                    leaseRenewJob = null
                }
            } catch (error: StationNotFoundException) {
                val task = claimedTask
                val activeApi = api
                if (task == null || activeApi == null) {
                    SyncOutcome.Failed(error.message ?: "目标站点不存在")
                } else {
                    try {
                        activeApi.completeTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                mode = collectionMode,
                                leaseToken = task.leaseToken,
                                resultSummary = linkedMapOf(
                                    "stations" to 0,
                                    "type" to task.type,
                                    "city" to task.city,
                                    "district" to task.district,
                                    "sourceSiteId" to task.sourceSiteId,
                                    "sourceSiteOrder" to task.sourceSiteOrder,
                                    "stationId" to task.stationId,
                                    "stationName" to error.stationName,
                                    "stationSequence" to task.stationSequence,
                                    "skipped" to true,
                                    "skipReason" to "NO_MATCH",
                                    "skipMessage" to (error.message ?: "搜索结果中没有对应站点"),
                                ),
                            ),
                        )
                        SyncOutcome.Skipped(
                            stationName = error.stationName,
                            reason = "搜索结果中没有对应站点",
                        )
                    } catch (completionError: Exception) {
                        try {
                            activeApi.failTask(
                                task.id,
                                TaskActionRequest(
                                    deviceCode = deviceCode,
                                    mode = collectionMode,
                                    leaseToken = task.leaseToken,
                                    errorCode = "SKIP_REPORT_FAILED",
                                    errorMessage = completionError.message
                                        ?: completionError.javaClass.simpleName,
                                    retryable = true,
                                ),
                            )
                        } catch (_: Exception) {
                            // The lease will expire if neither status call reaches the server.
                        }
                        SyncOutcome.Failed(
                            "跳过任务状态提交失败: ${completionError.message ?: completionError.javaClass.simpleName}"
                        )
                    }
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                claimedTask?.let { task ->
                    try {
                        api?.failTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                mode = collectionMode,
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
                SyncOutcome.Failed(friendlyNetworkError(error))
            }
        }
    }

    private fun friendlyNetworkError(error: Exception): String {
        val root = generateSequence(error as Throwable?) { it.cause }.lastOrNull() ?: error
        val isNetworkFailure = root is ConnectException ||
            root is SocketTimeoutException ||
            root is UnknownHostException ||
            root is EOFException ||
            root is IOException
        if (!isNetworkFailure) {
            return error.message ?: error.javaClass.simpleName
        }
        return "无法连接电脑端服务 ${AppPreferences.serverUrl(context)}；请确认电脑已启动 API 服务，系统会自动重试"
    }

    suspend fun uploadLocalResultsOnly(): Int {
        val deviceCode = AppPreferences.deviceCode(context)
        val api = RetrofitClient.serverApi(AppPreferences.serverUrl(context), context)
        val registerResponse = api.register(
            DeviceRegisterRequest(
                activationCode = AppPreferences.activationCode(context),
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
        return uploadPendingResults(api, deviceCode, "", leaseToken = "")
    }

    private suspend fun uploadPendingResults(
        api: ServerApi,
        deviceCode: String,
        taskId: String,
        taskStartedAt: Long? = null,
        leaseToken: String = "",
        remoteTasksOnly: Boolean = false,
    ): Int {
        // Filter in SQLite before applying LIMIT. Previously we loaded the
        // oldest 50 rows first and only then filtered by taskId, so a current
        // task could be hidden behind an old local outbox backlog.
        val candidates = when {
            taskId.isNotBlank() -> buildList {
                addAll(repository.pendingResultsForTask(taskId))
                if (taskStartedAt != null) {
                    addAll(repository.pendingResultsSince(taskStartedAt))
                }
            }
            remoteTasksOnly -> repository.pendingRemoteTaskResults()
            else -> repository.pendingResults()
        }
        val pending = candidates
            .distinctBy { it.idempotencyKey }
            .filter { item ->
                if (taskId.isBlank()) {
                    !remoteTasksOnly || embeddedTaskId(item.resultJson).isNotBlank()
                } else {
                    val resultTaskId = embeddedTaskId(item.resultJson)
                    resultTaskId == taskId ||
                        (resultTaskId.isBlank() &&
                            taskStartedAt != null && item.createdAt >= taskStartedAt)
                }
            }
            .take(50)
        if (pending.isEmpty()) return 0
        val observations = pending.map { item ->
            val observationTaskId = embeddedTaskId(item.resultJson).ifBlank { taskId }
            mapOf(
                "observationId" to item.idempotencyKey,
                "stationId" to item.stationId,
                "taskId" to observationTaskId,
                "deviceId" to deviceCode,
                "capturedAt" to item.createdAt,
                "payload" to item.resultJson,
            )
        }
        val response = api.uploadObservations(
            ObservationUploadRequest(
                deviceCode = deviceCode,
                leaseToken = leaseToken,
                observations = observations,
            )
        )
        val acknowledgedIds = (response.acceptedIds + response.duplicateIds).toSet()
        if (acknowledgedIds.isNotEmpty() || response.failedIds.isNotEmpty()) {
            pending.filter { it.idempotencyKey in acknowledgedIds }
                .forEach { repository.markUploaded(it.idempotencyKey) }
            val uploadedCount = pending.count { it.idempotencyKey in acknowledgedIds }
            val unresolved = pending.filter { it.idempotencyKey !in acknowledgedIds }
            if (unresolved.isNotEmpty()) {
                throw IllegalStateException(
                    "服务端未确认 ${unresolved.size}/${pending.size} 条采集结果，" +
                        "失败ID=${response.failedIds.joinToString(",")}".take(500),
                )
            }
            return uploadedCount
        } else {
            // Backward compatibility for an older server that only returns
            // aggregate counts. New servers must return per-ID lists.
            val acknowledged = response.accepted + response.duplicates
            if (acknowledged < pending.size) {
                throw IllegalStateException(
                    "服务端仅确认 $acknowledged/${pending.size} 条采集结果"
                )
            }
            pending.forEach { repository.markUploaded(it.idempotencyKey) }
            return acknowledged
        }
        return pending.size
    }

    private suspend fun startLeaseRenewal(
        api: ServerApi,
        deviceCode: String,
        collectionMode: String,
        task: RemoteTaskDto,
        leaseLost: AtomicBoolean,
    ): Job {
        val parentContext = currentCoroutineContext()
        return kotlinx.coroutines.CoroutineScope(parentContext).launch(Dispatchers.IO) {
            while (isActive) {
                delay(30_000L)
                try {
                    api.reportProgress(
                        task.id,
                        TaskActionRequest(
                            deviceCode = deviceCode,
                            mode = collectionMode,
                            leaseToken = task.leaseToken,
                            progress = mapOf(
                                "leaseHeartbeatAt" to System.currentTimeMillis(),
                            ),
                        ),
                    )
                    AppPreferences.appendLog(context, "任务租约续期成功: ${task.id}")
                } catch (error: CancellationException) {
                    throw error
                } catch (error: HttpException) {
                    if (error.code() == 401 || error.code() == 409) {
                        leaseLost.set(true)
                        AppPreferences.appendLog(context, "任务租约已失效: ${task.id}")
                        return@launch
                    }
                    AppPreferences.appendLog(
                        context,
                        "任务租约续期失败，将继续重试: ${error.message()}",
                    )
                } catch (error: Exception) {
                    AppPreferences.appendLog(
                        context,
                        "任务租约续期网络异常，将继续重试: ${error.message ?: error.javaClass.simpleName}",
                    )
                }
            }
        }
    }

    private fun embeddedTaskId(resultJson: String): String = runCatching {
        val payload = JsonParser.parseString(resultJson)
        if (!payload.isJsonObject) return@runCatching ""
        payload.asJsonObject.get("remoteTaskId")?.asString.orEmpty()
    }.getOrDefault("")
}

sealed class SyncOutcome {
    data class Completed(val stationCount: Int) : SyncOutcome()
    data class Skipped(
        val stationName: String,
        val reason: String,
    ) : SyncOutcome()
    data class Failed(val message: String) : SyncOutcome()
    object NoTask : SyncOutcome()
}
