package com.tigercode.evcollector.network

import com.google.gson.annotations.SerializedName
import retrofit2.http.Body
import retrofit2.http.GET
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
)

data class ClaimTaskResponse(
    @SerializedName("task") val task: RemoteTaskDto?,
)

data class TaskActionRequest(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("leaseToken") val leaseToken: String,
    @SerializedName("progress") val progress: Map<String, Any>? = null,
    @SerializedName("resultSummary") val resultSummary: Map<String, Any>? = null,
    @SerializedName("errorCode") val errorCode: String? = null,
    @SerializedName("errorMessage") val errorMessage: String? = null,
    @SerializedName("retryable") val retryable: Boolean? = null,
)

data class ObservationUploadRequest(
    @SerializedName("deviceCode") val deviceCode: String,
    @SerializedName("observations") val observations: List<Map<String, Any>>,
)

data class ObservationUploadResponse(
    @SerializedName("accepted") val accepted: Int,
    @SerializedName("duplicates") val duplicates: Int,
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

    @GET("health")
    suspend fun health(): Map<String, String>
}
