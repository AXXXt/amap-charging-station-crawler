package com.tigercode.evcollector.network

import com.google.gson.annotations.SerializedName
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Path

data class DeviceRegisterRequest(
    @SerializedName("activationCode") val activationCode: String,
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("name") val name: String,
    @SerializedName("appVersion") val appVersion: String,
    @SerializedName("parserVersion") val parserVersion: String,
    @SerializedName("targetAppVersion") val targetAppVersion: String,
    @SerializedName("capabilities") val capabilities: Map<String, Any>,
)

data class DeviceRegisterResponse(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("status") val status: String,
    @SerializedName("deviceToken") val deviceToken: String = "",
    @SerializedName("heartbeatIntervalSeconds") val heartbeatIntervalSeconds: Int = 15,
    @SerializedName("taskPollIntervalSeconds") val taskPollIntervalSeconds: Int = 5,
)

data class HeartbeatRequest(
    @SerializedName("status") val status: String,
    @SerializedName("lastError") val lastError: String,
    @SerializedName("appVersion") val appVersion: String,
    @SerializedName("parserVersion") val parserVersion: String,
    @SerializedName("targetAppVersion") val targetAppVersion: String,
    @SerializedName("capabilities") val capabilities: Map<String, Any>,
)

data class ClaimTaskRequest(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("mode") val mode: String = "NORMAL",
)

data class RemoteTaskDto(
    @SerializedName("id") val id: String,
    @SerializedName("type") val type: String,
    @SerializedName("priority") val priority: Int,
    @SerializedName("province") val province: String,
    @SerializedName("city") val city: String,
    @SerializedName("district") val district: String,
    @SerializedName("keyword") val keyword: String,
    @SerializedName("searchRegion") val searchRegion: String,
    @SerializedName("leaseToken") val leaseToken: String,
    @SerializedName("attempt") val attempt: Int,
    @SerializedName("maxAttempts") val maxAttempts: Int,
    @SerializedName("recoveryAttempt") val recoveryAttempt: Int = 0,
    @SerializedName("maxRecoveryAttempts") val maxRecoveryAttempts: Int = 2,
    @SerializedName("sourceSiteId") val sourceSiteId: String = "",
    @SerializedName("sourceSiteOrder") val sourceSiteOrder: Long = 0,
    @SerializedName("stationId") val stationId: String = "",
    @SerializedName("stationName") val stationName: String = "",
    @SerializedName("stationAddress") val stationAddress: String = "",
    @SerializedName("stationLatitude") val stationLatitude: Double? = null,
    @SerializedName("stationLongitude") val stationLongitude: Double? = null,
    @SerializedName("stationSequence") val stationSequence: Int = 0,
    @SerializedName("sourcePayload") val sourcePayload: Map<String, Any> = emptyMap(),
)

data class ClaimTaskResponse(
    @SerializedName("task") val task: RemoteTaskDto?,
    @SerializedName("reason") val reason: String = "",
)

data class TaskActionRequest(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("mode") val mode: String = "NORMAL",
    @SerializedName("leaseToken") val leaseToken: String,
    @SerializedName("progress") val progress: Map<String, Any>? = null,
    @SerializedName("resultSummary") val resultSummary: Map<String, Any>? = null,
    @SerializedName("errorCode") val errorCode: String? = null,
    @SerializedName("errorMessage") val errorMessage: String? = null,
    @SerializedName("retryable") val retryable: Boolean? = null,
)

data class ObservationUploadRequest(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("leaseToken") val leaseToken: String = "",
    @SerializedName("observations") val observations: List<Map<String, Any>>,
)

data class ObservationUploadResponse(
    @SerializedName("accepted") val accepted: Int = 0,
    @SerializedName("duplicates") val duplicates: Int = 0,
    @SerializedName("acceptedIds") val acceptedIds: List<String> = emptyList(),
    @SerializedName("duplicateIds") val duplicateIds: List<String> = emptyList(),
    @SerializedName("failedIds") val failedIds: List<String> = emptyList(),
)

data class HenanPoiImportRequest(
    @SerializedName("keyword") val keyword: String = "重卡充电站",
    @SerializedName("province") val province: String = "河南省",
    @SerializedName("adcode") val adcode: String = "410000",
    @SerializedName("priority") val priority: Int = 80,
    @SerializedName("maxAttempts") val maxAttempts: Int = 3,
    @SerializedName("skipExistingResults") val skipExistingResults: Boolean = true,
)

data class HenanPoiImportResponse(
    @SerializedName("jobId") val jobId: String = "",
    @SerializedName("status") val status: String = "",
    @SerializedName("ready") val ready: Boolean = false,
    @SerializedName("started") val started: Boolean = false,
    @SerializedName("currentCity") val currentCity: String = "",
    @SerializedName("citiesCompleted") val citiesCompleted: Int = 0,
    @SerializedName("citiesTotal") val citiesTotal: Int = 18,
    @SerializedName("reportedTotal") val total: Int = 0,
    @SerializedName("fetched") val fetched: Int = 0,
    @SerializedName("created") val created: Int = 0,
    @SerializedName("skippedTask") val skippedTask: Int = 0,
    @SerializedName("skippedResult") val skippedResult: Int = 0,
    @SerializedName("skippedDuplicatePoi") val skippedDuplicatePoi: Int = 0,
    @SerializedName("message") val message: String = "",
    @SerializedName("failedCities") val failedCities: List<String> = emptyList(),
)

data class HenanTaskResetRequest(
    @SerializedName("confirmation") val confirmation: String = "RESET_ALL_HENAN_TASKS",
)

data class HenanTaskResetResponse(
    @SerializedName("total") val total: Int = 0,
    @SerializedName("reset") val reset: Int = 0,
    @SerializedName("statusCounts") val statusCounts: Map<String, Int> = emptyMap(),
)

interface ServerApi {
    @POST("api/v1/devices/register")
    suspend fun register(@Body body: DeviceRegisterRequest): DeviceRegisterResponse

    @POST("api/v1/devices/heartbeat")
    suspend fun heartbeat(@Body body: HeartbeatRequest): Map<String, Any>

    @POST("api/v1/device-tasks/claim")
    suspend fun claimTask(@Body body: ClaimTaskRequest): ClaimTaskResponse

    @POST("api/v1/device-tasks/{taskId}/ack")
    suspend fun acknowledgeTask(@Path("taskId") taskId: String, @Body body: TaskActionRequest): Map<String, Any>

    @POST("api/v1/device-tasks/{taskId}/progress")
    suspend fun reportProgress(@Path("taskId") taskId: String, @Body body: TaskActionRequest): Map<String, Any>

    @POST("api/v1/device-tasks/{taskId}/complete")
    suspend fun completeTask(@Path("taskId") taskId: String, @Body body: TaskActionRequest): Map<String, Any>

    @POST("api/v1/device-tasks/{taskId}/fail")
    suspend fun failTask(@Path("taskId") taskId: String, @Body body: TaskActionRequest): Map<String, Any>

    @POST("api/v1/observations/batches")
    suspend fun uploadObservations(@Body body: ObservationUploadRequest): ObservationUploadResponse

    @POST("api/v1/admin/henan-poi/import")
    suspend fun importHenanPois(
        @Header("x-admin-key") adminKey: String,
        @Body body: HenanPoiImportRequest,
    ): HenanPoiImportResponse

    @GET("api/v1/admin/henan-poi/import-status")
    suspend fun henanPoiImportStatus(
        @Header("x-admin-key") adminKey: String,
    ): HenanPoiImportResponse

    @POST("api/v1/admin/henan-tasks/reset")
    suspend fun resetHenanTasks(
        @Header("x-admin-key") adminKey: String,
        @Body body: HenanTaskResetRequest,
    ): HenanTaskResetResponse

    @GET("health")
    suspend fun health(): Map<String, String>
}
