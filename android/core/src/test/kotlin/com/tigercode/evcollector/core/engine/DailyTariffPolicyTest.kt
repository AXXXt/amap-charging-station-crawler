package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationDetail
import java.time.OffsetDateTime
import org.junit.Assert.*
import org.junit.Test

class DailyTariffPolicyTest {
    private fun time(s: String) = OffsetDateTime.parse(s + "+08:00").toInstant().toEpochMilli()
    private fun period(t: String, total: String = "0.8", fee: String = "0.2") = PricePeriod(time = t, totalPrice = total, elecFee = "0.6", serviceFee = fee)
    private val now = time("2026-11-11T12:00:00")
    private val plan = listOf(period("00:00-24:00"))
    private val cache = DailyTariffCache(now - 3600000, "BAIDU", plan)
    private val live = StationDetail(currentPrice = "0.8", fastAvailable = "9", fastTotal = "10", address = "fresh address")

    @Test fun reusedPlanKeepsFreshGunStateAndOriginalCaptureTime() {
        val result = DailyTariffPolicy.reuse(live, cache, "BAIDU", now)!!
        assertEquals("9", result.fastAvailable)
        assertEquals("10", result.fastTotal)
        assertEquals("fresh address", result.address)
        assertEquals(cache.capturedAt, result.priceScheduleCapturedAt)
        assertTrue(result.priceScheduleReused)
    }
    @Test fun priceMismatchWrongMapExpiredOrPriorDayForceFullRead() {
        assertNull(DailyTariffPolicy.reuse(live.copy(currentPrice = "0.9"), cache, "BAIDU", now))
        assertNull(DailyTariffPolicy.reuse(live, cache, "AMAP", now))
        assertNull(DailyTariffPolicy.reuse(live, cache.copy(capturedAt = now - 21600000), "BAIDU", now))
        assertNull(DailyTariffPolicy.reuse(live, cache.copy(capturedAt = time("2026-11-10T23:59:00")), "BAIDU", time("2026-11-11T00:01:00")))
        assertNull(DailyTariffPolicy.reuse(live.copy(currentPrice = ""), cache, "BAIDU", now))
    }
    @Test fun missingKnownChargingTypeOrFeesPreventReuse() {
        assertNull(DailyTariffPolicy.reuse(live.copy(slowTotal = "5"), cache, "BAIDU", now))
        assertNull(DailyTariffPolicy.reuse(live, cache.copy(fastPrices = listOf(period("00:00-24:00", fee = ""))), "BAIDU", now))
        assertFalse(DailyTariffPolicy.complete(listOf(period("00:00-24:00", fee = "0.5"))))
    }
    @Test fun fullCoverageAcceptsMidnightAndAuxiliaryRowButRejectsGapsAndConflicts() {
        assertTrue(DailyTariffPolicy.complete(listOf(period("22:00-08:00"), period("08:00-22:00"))))
        assertTrue(DailyTariffPolicy.complete(plan + period("当前时段")))
        assertFalse(DailyTariffPolicy.complete(listOf(period("当前时段"))))
        assertFalse(DailyTariffPolicy.complete(listOf(period("00:00-06:00"), period("10:00-24:00"))))
        assertFalse(DailyTariffPolicy.complete(plan + period("08:00-09:00", "0.9", "0.3")))
        assertTrue(DailyTariffPolicy.complete(listOf(period("00：00—23：59", "0.6", "0"))))
    }
}
