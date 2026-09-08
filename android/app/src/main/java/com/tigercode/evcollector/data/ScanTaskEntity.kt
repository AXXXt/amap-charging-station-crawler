package com.tigercode.evcollector.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "scan_tasks")
data class ScanTaskEntity(
    @PrimaryKey val stationId: String,
    val name: String,
    val address: String,
    val latitude: Double?,
    val longitude: Double?,
    val payloadJson: String,
    val status: String,
    val attempts: Int,
    val maxAttempts: Int,
    val leaseToken: String,
    val leaseExpiresAt: Long,
    val resultJson: String?,
    val lastError: String,
    val createdAt: Long,
    val updatedAt: Long,
)
