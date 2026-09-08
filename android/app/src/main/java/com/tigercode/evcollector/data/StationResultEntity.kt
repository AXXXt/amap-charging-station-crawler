package com.tigercode.evcollector.data

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "station_results")
data class StationResultEntity(
    @PrimaryKey val idempotencyKey: String,
    val stationId: String,
    val resultJson: String,
    val uploaded: Boolean,
    val createdAt: Long,
    // Existing rows are migrated as ineligible unless they belong to a remote
    // task. Every newly collected row is eligible for automatic upload.
    @ColumnInfo(defaultValue = "0") val syncEligible: Boolean = true,
)
