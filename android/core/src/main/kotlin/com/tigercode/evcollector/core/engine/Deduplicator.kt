package com.tigercode.evcollector.core.engine

import kotlin.math.asin
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

data class StationRecord(
    val name: String,
    val longitude: Double?,
    val latitude: Double?,
    val filledCount: Int,
)

object Deduplicator {
    fun deduplicate(
        records: List<StationRecord>,
        distanceThresholdKm: Double = 0.5,
    ): Pair<List<StationRecord>, Int> {
        val kept = mutableListOf<StationRecord>()
        var removed = 0

        for (record in records) {
            var duplicate = false
            for (index in kept.indices) {
                val existing = kept[index]
                val lon1 = record.longitude
                val lat1 = record.latitude
                val lon2 = existing.longitude
                val lat2 = existing.latitude
                if (lon1 == null || lat1 == null || lon2 == null || lat2 == null) continue

                val distance = haversine(lon1, lat1, lon2, lat2)
                val sameName = record.name == existing.name

                if (sameName && distance < distanceThresholdKm) {
                    duplicate = true
                    if (record.filledCount > existing.filledCount) {
                        kept[index] = record
                    }
                    break
                }

                if (distance < 0.1) {
                    duplicate = true
                    if (record.filledCount > existing.filledCount) {
                        kept[index] = record
                    }
                    break
                }
            }

            if (duplicate) {
                removed += 1
            } else {
                kept.add(record)
            }
        }

        return kept to removed
    }

    private fun haversine(lon1: Double, lat1: Double, lon2: Double, lat2: Double): Double {
        val earthRadius = 6371.0
        val dLon = Math.toRadians(lon2 - lon1)
        val dLat = Math.toRadians(lat2 - lat1)
        val a = sin(dLat / 2) * sin(dLat / 2) +
            cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) *
            sin(dLon / 2) * sin(dLon / 2)
        return earthRadius * 2 * asin(sqrt(a))
    }
}
