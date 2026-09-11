package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.CollectionPacingPolicy
import org.junit.Assert.assertEquals
import org.junit.Test

class CollectionPacingPolicyTest {
    @Test
    fun permitsEntryWhenAllWindowsAreBelowLimit() {
        val now = 1_000_000L
        val timestamps = listOf(now - 50_000L, now - 30_000L, now - 10_000L)

        assertEquals(0L, CollectionPacingPolicy.requiredDetailDelayMs(timestamps, now))
    }

    @Test
    fun enforcesConservativeOneMinuteWindow() {
        val now = 1_000_000L
        val timestamps = listOf(
            now - 50_000L,
            now - 40_000L,
            now - 30_000L,
            now - 20_000L,
            now - 10_000L,
            now,
        )

        assertEquals(11_500L, CollectionPacingPolicy.requiredDetailDelayMs(timestamps, now))
    }

    @Test
    fun enforcesConservativeFiveMinuteWindow() {
        val now = 1_000_000L
        val timestamps = (0 until 16).map { index -> now - index * 15_000L }

        assertEquals(76_500L, CollectionPacingPolicy.requiredDetailDelayMs(timestamps, now))
    }

    @Test
    fun enforcesConservativeFifteenMinuteWindow() {
        val now = 2_000_000L
        val timestamps = (0 until 36).map { index -> now - index * 24_000L }

        assertEquals(61_500L, CollectionPacingPolicy.requiredDetailDelayMs(timestamps, now))
    }

    @Test
    fun usesLowerDensityAtNight() {
        assertEquals(5_000L, CollectionPacingPolicy.delayRangeForHour(9).minMs)
        assertEquals(8_000L, CollectionPacingPolicy.delayRangeForHour(14).minMs)
        assertEquals(12_000L, CollectionPacingPolicy.delayRangeForHour(20).minMs)
        assertEquals(25_000L, CollectionPacingPolicy.delayRangeForHour(2).minMs)
    }
}
