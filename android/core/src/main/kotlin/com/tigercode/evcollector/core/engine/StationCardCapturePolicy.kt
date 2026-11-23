package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.core.parser.ChargingPileDialogParser
import com.tigercode.evcollector.core.parser.DetailParser
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.ResultMerger
import com.tigercode.evcollector.core.parser.StationDetailScope

/** Live fields must come from this station's homepage, never a price sheet or result list. */
object StationCardCapturePolicy {
    fun read(root: NodeSnapshot, stationName: String): StationDetail? {
        val page = PageAssessor.assess(root, stationName)
        if (!page.expectedStationVisible || page.kind != PageKind.DETAIL ||
            ChargingPileDialogParser.isDialog(root)
        ) return null
        return DetailParser.parse(StationDetailScope.withoutRecommendations(root))
    }

    private fun validPrice(value: String): Boolean =
        value.toDoubleOrNull()?.let { it.isFinite() && it >= 0.0 } == true

    private fun validGroup(idle: String, total: String): Boolean {
        val available = idle.toIntOrNull() ?: return false
        val count = total.toIntOrNull() ?: return false
        return count > 0 && available in 0..count
    }

    fun missingFields(detail: StationDetail): List<String> = buildList {
        if (!validPrice(detail.currentPrice)) add("实时电价")
        val groups = listOf(
            detail.fastAvailable to detail.fastTotal,
            detail.superAvailable to detail.superTotal,
            detail.slowAvailable to detail.slowTotal,
        ).filter { (idle, total) -> idle.isNotBlank() || total.isNotBlank() }
        if (groups.isEmpty() || groups.any { (idle, total) -> !validGroup(idle, total) }) {
            add("空闲/总枪数")
        }
    }

    fun isComplete(detail: StationDetail): Boolean = missingFields(detail).isEmpty()

    /** A real tariff row is useful even when this station exposes no live pile status. */
    fun hasUsableTariff(detail: StationDetail): Boolean =
        (detail.fastPrices + detail.slowPrices).any { period ->
            val match = Regex("""^(\d{1,2}):(\d{2})\s*[-–—~～至]\s*(\d{1,2}):(\d{2})$""")
                .matchEntire(period.time.replace('：', ':').trim())
            if (match == null) false else {
                val (h1, m1, h2, m2) = match.destructured.toList().map { it.toInt() }
                h1 in 0..23 && m1 in 0..59 && h2 in 0..24 && m2 in 0..59 &&
                    (h2 < 24 || m2 == 0) && h1 * 60 + m1 != h2 * 60 + m2 &&
                    validPrice(period.totalPrice)
            }
        }

    fun canSave(detail: StationDetail): Boolean = isComplete(detail) || hasUsableTariff(detail)

    /** Keep tariff metadata and static fields, but prefer a newly read valid live value. */
    fun merge(first: StationDetail, latest: StationDetail): StationDetail {
        val fast = validGroup(latest.fastAvailable, latest.fastTotal)
        val supercharge = validGroup(latest.superAvailable, latest.superTotal)
        val slow = validGroup(latest.slowAvailable, latest.slowTotal)
        val merged = ResultMerger.merge(first, latest).copy(
            currentPrice = if (validPrice(latest.currentPrice)) latest.currentPrice else first.currentPrice,
            fastAvailable = if (fast) latest.fastAvailable else first.fastAvailable.ifBlank { latest.fastAvailable },
            fastTotal = if (fast) latest.fastTotal else first.fastTotal.ifBlank { latest.fastTotal },
            fastPower = if (fast) latest.fastPower.ifBlank { first.fastPower } else first.fastPower.ifBlank { latest.fastPower },
            superAvailable = if (supercharge) latest.superAvailable else first.superAvailable.ifBlank { latest.superAvailable },
            superTotal = if (supercharge) latest.superTotal else first.superTotal.ifBlank { latest.superTotal },
            superPower = if (supercharge) latest.superPower.ifBlank { first.superPower } else first.superPower.ifBlank { latest.superPower },
            slowAvailable = if (slow) latest.slowAvailable else first.slowAvailable.ifBlank { latest.slowAvailable },
            slowTotal = if (slow) latest.slowTotal else first.slowTotal.ifBlank { latest.slowTotal },
            slowPower = if (slow) latest.slowPower.ifBlank { first.slowPower } else first.slowPower.ifBlank { latest.slowPower },
            priceScheduleCapturedAt = first.priceScheduleCapturedAt,
            priceScheduleReused = first.priceScheduleReused,
            sourcePage = first.sourcePage.ifBlank { latest.sourcePage },
            collectionMode = first.collectionMode.ifBlank { latest.collectionMode },
        )
        val total = listOf(merged.fastTotal, merged.superTotal, merged.slowTotal)
            .mapNotNull { it.toIntOrNull() }.sum()
        return if (total > 0) merged.copy(chargingPileTotal = total) else merged
    }
}
