package com.tigercode.evcollector.network

import android.content.Context
import android.os.Build
import com.google.gson.JsonParser
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.CollectorVersions
import com.tigercode.evcollector.CollectionStatusNotifier
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.engine.CollectionEngine
import com.tigercode.evcollector.engine.DetailPageRiskControlException
import com.tigercode.evcollector.engine.HumanVerificationException
import com.tigercode.evcollector.engine.SuggestionSearchNotStartedException
import com.tigercode.evcollector.engine.StationNotFoundException
import com.tigercode.evcollector.engine.SkippedStationException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.job
import kotlinx.coroutines.cancel
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
private const val HENAN_POI_LIST_TASK = "HENAN_POI_LIST"

class SyncManager(
    private val context: Context,
    private val repository: CollectorRepository,
    private val engine: CollectionEngine,
) {
    suspend fun runOnce(): SyncOutcome {
        return withContext(Dispatchers.IO) {
            val deviceCode = AppPreferences.deviceCode(context)
            val activationCode = AppPreferences.activationCode(context)
            // The server keeps this fixed 499-station loop separate from Amap.
            val collectionMode = "MONITOR_BAIDU_MIN240_499"
            var claimedTask: RemoteTaskDto? = null
            var api: ServerApi? = null
            var leaseRenewJob: Job? = null
            var fleetWatchJob: Job? = null
            val leaseLost = AtomicBoolean(false)
            try {
                reportStatus("正在连接百度采集服务器")
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
                            "fleetStopSupported" to true,
                            "sdkInt" to Build.VERSION.SDK_INT,
                            "manufacturer" to Build.MANUFACTURER,
                            "model" to Build.MODEL,
                        ),
                    )
                )
                if (registerResponse.deviceToken.isNotBlank()) {
                    AppPreferences.saveDeviceToken(context, registerResponse.deviceToken)
                }
                if (registerResponse.priorityStationScopes?.get("BAIDU_MIN240_499") != 499) {
                    throw IllegalStateException("服务器尚未部署百度499站循环调度，请先更新服务器")
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
                            "fleetStopSupported" to true,
                        ),
                    )
                )

                reportStatus("正在领取百度499站循环采集任务")
                val controlApi = FleetControlClient.api(context)
                FleetControlClient.requireAllowed(context, controlApi)
                val taskJob = currentCoroutineContext().job
                fleetWatchJob = kotlinx.coroutines.CoroutineScope(currentCoroutineContext()).launch {
                    while (isActive) {
                        delay(3_000L)
                        try {
                            val control = FleetControlClient.control(context, controlApi)
                            if (control.stopRequired || !control.releaseComplete) {
                                engine.stop()
                                FleetControlClient.stopAndAcknowledge(context, controlApi, control.stopGeneration)
                                taskJob.cancel(FleetStoppedException())
                                return@launch
                            }
                        } catch (error: CancellationException) {
                            throw error
                        } catch (_: Exception) {
                            // A transient control request failure is retried in three seconds.
                        }
                    }
                }
                val claim = activeApi.claimTask(ClaimTaskRequest(deviceCode, collectionMode))
                if (claim.reason == "FLEET_STOPPED") {
                    engine.stop()
                    FleetControlClient.markStopped(context)
                    throw FleetStoppedException()
                }
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

                reportStatus("已领取站点：${task.stationName.ifBlank { task.keyword }}")
                activeApi.acknowledgeTask(
                    task.id,
                    TaskActionRequest(deviceCode = deviceCode, mode = collectionMode, leaseToken = task.leaseToken),
                )
                leaseRenewJob = startLeaseRenewal(activeApi, deviceCode, collectionMode, task, leaseLost)
                try {
                    // A failed status upload must not reopen a station already found to have no data.
                    if (AppPreferences.hasNoDataTask(context, task.id)) {
                        AppPreferences.appendLog(context, "已记录无电价/桩信息，仅同步跳过状态: ${task.stationName.ifBlank { task.keyword }}")
                        throw SkippedStationException(task.stationName.ifBlank { task.keyword })
                    }
                    val taskStartedAt = System.currentTimeMillis()
                    val listSnapshotMode =
                        AppPreferences.isMonitorMode(context) && AppPreferences.isMonitorListMode(context)
                    val details = if (
                        task.type == HENAN_POI_LIST_TASK ||
                        (listSnapshotMode && task.type == HENAN_POI_DETAIL_TASK)
                    ) {
                        val requestedName = task.stationName.ifBlank { task.keyword }
                        val detail = engine.runStationListSnapshotTask(
                            stationId = task.stationId,
                            stationName = requestedName,
                            address = task.stationAddress,
                            latitude = task.stationLatitude,
                            longitude = task.stationLongitude,
                            remoteTaskId = task.id,
                        ) ?: throw IllegalStateException("未采集到目标站点: $requestedName")
                        listOf(detail)
                    } else if (
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
                        task.type == HENAN_POI_DETAIL_TASK ||
                        task.type == HENAN_POI_LIST_TASK
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
                    reportStatus("采集结果已上传，正在同步任务完成状态")
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
            } catch (error: DetailPageRiskControlException) {
                claimedTask?.let { task ->
                    try {
                        api?.failTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                mode = collectionMode,
                                leaseToken = task.leaseToken,
                                errorCode = "DETAIL_RISK_CONTROL",
                                errorMessage = error.message ?: "详情页触发风控",
                                retryable = true,
                            ),
                        )
                    } catch (_: Exception) {
                        // Leave the lease to expire if the release call also fails.
                    }
                }
                SyncOutcome.DetailRiskReleased(
                    error.message ?: "详情页触发风控，任务已释放",
                )
            } catch (error: HumanVerificationException) {
                claimedTask?.let { task ->
                    try {
                        api?.failTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                mode = collectionMode,
                                leaseToken = task.leaseToken,
                                errorCode = "HUMAN_VERIFICATION",
                                errorMessage = error.message
                                    ?: error.javaClass.simpleName,
                                retryable = true,
                            ),
                        )
                    } catch (_: Exception) {
                        // Leave the lease to expire if the release call also fails.
                    }
                }
                SyncOutcome.HumanVerification(
                    error.message ?: "检测到人机验证，已停止本设备采集",
                )
            } catch (error: SuggestionSearchNotStartedException) {
                claimedTask?.let { task ->
                    try {
                        api?.failTask(
                            task.id,
                            TaskActionRequest(
                                deviceCode = deviceCode,
                                mode = collectionMode,
                                leaseToken = task.leaseToken,
                                errorCode = "SUGGESTION_NOT_STARTED",
                                errorMessage = error.message ?: "联想搜索未真正开始",
                                retryable = true,
                            ),
                        )
                    } catch (_: Exception) {
                        // Leave the lease to expire if the release call also fails.
                    }
                }
                SyncOutcome.DetailRiskReleased(
                    error.message ?: "联想搜索未真正开始，任务已释放重试",
                )
            } catch (error: SkippedStationException) {
                reportSkippedTask(
                    api = api,
                    task = claimedTask,
                    deviceCode = deviceCode,
                    collectionMode = collectionMode,
                    stationName = error.stationName,
                    reason = "NO_PRICE_OR_PILE",
                    message = "详情页无电价/桩信息，不再采集",
                )
            } catch (error: StationNotFoundException) {
                reportSkippedTask(
                    api = api,
                    task = claimedTask,
                    deviceCode = deviceCode,
                    collectionMode = collectionMode,
                    stationName = error.stationName,
                    reason = "NO_MATCH",
                    message = "搜索结果中没有对应站点",
                )
            } catch (error: CancellationException) {
                engine.stop()
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
            } finally {
                fleetWatchJob?.cancel()
            }
        }
    }

    private suspend fun reportSkippedTask(
        api: ServerApi?,
        task: RemoteTaskDto?,
        deviceCode: String,
        collectionMode: String,
        stationName: String,
        reason: String,
        message: String,
    ): SyncOutcome {
        if (task == null || api == null) return SyncOutcome.Failed(message)
        if (reason == "NO_PRICE_OR_PILE") {
            // Persist before the request so a network failure or app restart only retries reporting.
            AppPreferences.rememberNoDataTask(context, task.id)
        }
        return try {
            reportStatus("正在同步跳过状态：$stationName（$message）")
            // Completing a skipped task excludes it from both normal claims and failed-task recovery.
            api.completeTask(
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
                        "stationName" to stationName,
                        "stationSequence" to task.stationSequence,
                        "skipped" to true,
                        "skipReason" to reason,
                        "skipMessage" to message,
                    ),
                ),
            )
            if (reason == "NO_PRICE_OR_PILE") {
                AppPreferences.clearNoDataTask(context, task.id)
            }
            AppPreferences.appendLog(context, "已跳过并同步服务器: $stationName - $message")
            SyncOutcome.Skipped(stationName = stationName, reason = message)
        } catch (error: CancellationException) {
            throw error
        } catch (completionError: Exception) {
            try {
                api.failTask(
                    task.id,
                    TaskActionRequest(
                        deviceCode = deviceCode,
                        mode = collectionMode,
                        leaseToken = task.leaseToken,
                        errorCode = "SKIP_REPORT_FAILED",
                        errorMessage = completionError.message ?: completionError.javaClass.simpleName,
                        retryable = true,
                    ),
                )
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                // Keep the no-data marker while the lease expires; the next claim only reports the skip.
            }
            SyncOutcome.Failed(
                "跳过任务状态提交失败: ${friendlyNetworkError(completionError)}"
            )
        }
    }

    private fun reportStatus(message: String) {
        AppPreferences.setRemoteStatus(context, message)
        AppPreferences.appendLog(context, message)
        CollectionStatusNotifier.show(context, message)
    }

    private fun friendlyNetworkError(error: Exception): String {
        if (error is HttpException && error.code() == 409) {
            val code = runCatching {
                JsonParser.parseString(error.response()?.errorBody()?.string().orEmpty())
                    .asJsonObject.get("detail")?.asString
            }.getOrNull()?.takeIf { it.matches(Regex("[A-Z_]{1,64}")) }
            val reason = when (code) {
                "TASK_LEASE_STALE" -> "任务租约已变更或归属不一致"
                "TASK_NOT_ACTIVE" -> "任务已不在执行状态"
                "TASK_LEASE_EXPIRED" -> "任务租约已过期"
                "TASK_LEASE_INVALID" -> "任务租约无效"
                else -> "服务端任务状态冲突"
            }
            return "HTTP 409：$reason" + (code?.let { "（$it）" } ?: "")
        }
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
                            "fleetStopSupported" to true,
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
        reportStatus("正在上传 ${pending.size} 条采集结果，等待服务器确认")
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
                        AppPreferences.appendLog(context, "任务租约已失效: ${task.id}，${friendlyNetworkError(error)}")
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
    data class HumanVerification(val message: String) : SyncOutcome()
    data class DetailRiskReleased(val message: String) : SyncOutcome()
    data class Skipped(
        val stationName: String,
        val reason: String,
    ) : SyncOutcome()
    data class Failed(val message: String) : SyncOutcome()
    object NoTask : SyncOutcome()
}
