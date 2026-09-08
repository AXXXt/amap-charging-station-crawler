package com.tigercode.evcollector.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface StationResultDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(result: StationResultEntity)

    @Query(
        """SELECT * FROM station_results
           WHERE uploaded = 0 AND syncEligible = 1
           ORDER BY createdAt ASC LIMIT 50"""
    )
    suspend fun pendingUploads(): List<StationResultEntity>

    @Query(
        """SELECT * FROM station_results
           WHERE uploaded = 0 AND syncEligible = 1
             AND resultJson LIKE '%' || :taskId || '%'
           ORDER BY createdAt ASC LIMIT 50"""
    )
    suspend fun pendingUploadsForTask(taskId: String): List<StationResultEntity>

    @Query(
        """SELECT * FROM station_results
           WHERE uploaded = 0 AND syncEligible = 1 AND createdAt >= :startedAt
           ORDER BY createdAt ASC LIMIT 50"""
    )
    suspend fun pendingUploadsSince(startedAt: Long): List<StationResultEntity>

    @Query(
        """SELECT * FROM station_results
           WHERE uploaded = 0 AND syncEligible = 1
             AND resultJson LIKE '%"remoteTaskId"%'
           ORDER BY createdAt ASC LIMIT 50"""
    )
    suspend fun pendingRemoteTaskUploads(): List<StationResultEntity>

    @Query("UPDATE station_results SET uploaded = 1 WHERE idempotencyKey = :key")
    suspend fun markUploaded(key: String)
}
