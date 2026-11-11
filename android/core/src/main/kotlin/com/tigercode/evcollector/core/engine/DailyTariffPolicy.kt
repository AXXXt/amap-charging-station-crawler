package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationDetail
import java.time.Instant
import java.time.ZoneOffset
import kotlin.math.abs

/** Shared by claims for the same station and map platform. Timestamp is milliseconds. */
data class DailyTariffCache(
    val capturedAt: Long = 0,
    val platform: String = "",
    val fastPrices: List<PricePeriod> = emptyList(),
    val slowPrices: List<PricePeriod> = emptyList(),
)

object DailyTariffPolicy {
    private const val RECHECK_MS = 6 * 60 * 60 * 1000L
    private val zone = ZoneOffset.ofHours(8)
    private val timePattern = Regex("^(\\d{1,2}):(\\d{2})\\s*[-–—~～至]\\s*(\\d{1,2}):(\\d{2})$")
    private data class Range(val start: Int, val end: Int, val period: PricePeriod)
    private fun number(value: String): Double? {
        val text = value.trim().replace(Regex("\\s*元\\s*[/／]\\s*(?:度|kWh)$", RegexOption.IGNORE_CASE), "")
        return if (Regex("^(?:\\d+(?:\\.\\d+)?|\\.\\d+)$").matches(text)) text.toDoubleOrNull()?.takeIf { it.isFinite() } else null
    }
    private fun ranges(periods: List<PricePeriod>): List<Range>? {
        if (periods.isEmpty()) return null
        val ranges = mutableListOf<Range>()
        for (p in periods) {
            if (p.time.trim() == "当前时段") continue
            val match = timePattern.matchEntire(p.time.replace('：', ':').trim()) ?: return null
            val (h1, m1, h2, m2) = match.destructured
            val start = h1.toInt() * 60 + m1.toInt()
            var end = h2.toInt() * 60 + m2.toInt()
            if (m1.toInt() >= 60 || m2.toInt() >= 60 || start >= 1440 || end > 1440 || start == end) return null
            if (end == 1439) end = 1440
            val elec = number(p.elecFee) ?: return null
            val fee = number(p.serviceFee) ?: return null
            val total = number(p.totalPrice) ?: return null
            if (abs(elec + fee - total) > 0.015) return null
            if (start < end) ranges += Range(start, end, p)
            else { ranges += Range(start, 1440, p); if (end > 0) ranges += Range(0, end, p) }
        }
        return ranges.distinctBy { listOf(it.start, it.end, number(it.period.elecFee), number(it.period.serviceFee), number(it.period.totalPrice)) }.sortedBy { it.start }
    }
    fun complete(periods: List<PricePeriod>): Boolean {
        val ranges = ranges(periods) ?: return false
        var end = 0
        for (range in ranges) { if (range.start != end) return false; end = range.end }
        return end == 1440
    }
    fun reuse(detail: StationDetail, cache: DailyTariffCache?, platform: String, now: Long = System.currentTimeMillis()): StationDetail? {
        if (cache == null || cache.platform != platform || cache.capturedAt <= 0) return null
        val capturedAt = if (cache.capturedAt < 100_000_000_000L) cache.capturedAt * 1000 else cache.capturedAt
        if (capturedAt > now || now - capturedAt >= RECHECK_MS) return null
        val day = Instant.ofEpochMilli(now).atOffset(zone).toLocalDate()
        if (Instant.ofEpochMilli(capturedAt).atOffset(zone).toLocalDate() != day) return null
        val fast = cache.fastPrices
        val slow = cache.slowPrices
        if (fast.isEmpty() && slow.isEmpty()) return null
        if (fast.isNotEmpty() && !complete(fast) || slow.isNotEmpty() && !complete(slow)) return null
        if ((detail.fastTotal.toIntOrNull() ?: 0) + (detail.superTotal.toIntOrNull() ?: 0) > 0 && fast.isEmpty()) return null
        if ((detail.slowTotal.toIntOrNull() ?: 0) > 0 && slow.isEmpty()) return null
        val current = number(detail.currentPrice) ?: return null
        val minute = Instant.ofEpochMilli(now).atOffset(zone).let { it.hour * 60 + it.minute }
        val relevant = ranges(fast.ifEmpty { slow }) ?: return null
        val period = relevant.singleOrNull { minute >= it.start && minute < it.end } ?: return null
        val expected = number(period.period.totalPrice) ?: return null
        if (abs(current - expected) >= 0.005) return null
        return detail.copy(fastPrices = fast, slowPrices = slow, priceScheduleCapturedAt = capturedAt, priceScheduleReused = true)
    }
}
