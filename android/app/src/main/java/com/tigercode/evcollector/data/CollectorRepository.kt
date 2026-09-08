package com.tigercode.evcollector.data

import android.content.Context
import com.tigercode.evcollector.CollectorApp
import androidx.room.withTransaction
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class CollectorRepository(context: Context) {
    private val database = (context.applicationContext as CollectorApp).database
    private val scanTaskDao = database.scanTaskDao()
    private val resultDao = database.stationResultDao()

    suspend fun enqueueLocalTask(task: ScanTaskEntity) {
        withContext(Dispatchers.IO) {
            scanTaskDao.upsert(task)
        }
    }

    suspend fun nextPendingTask(): ScanTaskEntity? = withContext(Dispatchers.IO) {
        scanTaskDao.nextPending()
    }

    suspend fun claimTask(task: ScanTaskEntity): ScanTaskEntity {
        return withContext(Dispatchers.IO) {
            val token = java.util.UUID.randomUUID().toString()
            val now = System.currentTimeMillis()
            val updated = scanTaskDao.markClaimed(
                stationId = task.stationId,
                status = "running",
                leaseToken = token,
                leaseExpiresAt = now + LEASE_MS,
                updatedAt = now,
            )
            check(updated == 1) { "本地任务已被其他流程领取: ${task.stationId}" }
            task.copy(
                status = "running",
                attempts = task.attempts + 1,
                leaseToken = token,
                leaseExpiresAt = now + LEASE_MS,
            )
        }
    }

    suspend fun saveResult(
        task: ScanTaskEntity,
        resultJson: String,
        markLocalTaskSucceeded: Boolean = false,
    ) {
        withContext(Dispatchers.IO) {
            database.withTransaction {
                val now = System.currentTimeMillis()
                // Keep one pending local result per station so a retry or a
                // second collection run updates the same outbox row instead
                // of creating an unbounded list of lease-token variants.
                resultDao.upsert(
                    StationResultEntity(
                        idempotencyKey = "station:${task.stationId}",
                        stationId = task.stationId,
                        resultJson = resultJson,
                        uploaded = false,
                        createdAt = now,
                    )
                )
                if (markLocalTaskSucceeded) {
                    check(
                        scanTaskDao.markSucceeded(
                            task.stationId,
                            task.leaseToken,
                            resultJson,
                            now,
                        ) == 1,
                    ) { "本地任务状态已变化，未能标记成功: ${task.stationId}" }
                }
            }
        }
    }

    suspend fun markFailed(task: ScanTaskEntity, error: String) {
        withContext(Dispatchers.IO) {
            scanTaskDao.markFailed(task.stationId, task.leaseToken, error, System.currentTimeMillis())
        }
    }

    suspend fun pendingResults(): List<StationResultEntity> = withContext(Dispatchers.IO) {
        resultDao.pendingUploads()
    }

    suspend fun pendingResultsForTask(taskId: String): List<StationResultEntity> =
        withContext(Dispatchers.IO) {
            resultDao.pendingUploadsForTask(taskId)
        }

    suspend fun pendingResultsSince(startedAt: Long): List<StationResultEntity> =
        withContext(Dispatchers.IO) {
            resultDao.pendingUploadsSince(startedAt)
        }

    suspend fun pendingRemoteTaskResults(): List<StationResultEntity> =
        withContext(Dispatchers.IO) {
            resultDao.pendingRemoteTaskUploads()
        }

    suspend fun markUploaded(key: String) {
        withContext(Dispatchers.IO) {
            resultDao.markUploaded(key)
        }
    }

    companion object {
        private const val LEASE_MS = 180_000L
    }
}
