package com.tigercode.evcollector.core.engine

/**
 * Conservative pacing policy for AMap detail-page access.
 *
 * The sliding windows are deliberately lower than the observed platform
 * thresholds so multiple retries cannot create a burst at a boundary.
 */
object CollectionPacingPolicy {
    const val HISTORY_RETENTION_MS = 15 * 60 * 1000L
    const val SAFETY_MARGIN_MS = 1_500L

    private data class Window(
        val durationMs: Long,
        val maxEntries: Int,
    )

    private val detailWindows = listOf(
        Window(durationMs = 60 * 1000L, maxEntries = 6),
        Window(durationMs = 5 * 60 * 1000L, maxEntries = 16),
        Window(durationMs = 15 * 60 * 1000L, maxEntries = 36),
    )

    fun pruneDetailEntries(
        timestamps: Collection<Long>,
        nowMs: Long,
    ): List<Long> = timestamps
        .filter { it in (nowMs - HISTORY_RETENTION_MS)..nowMs }
        .sorted()

    /** Returns the wait required before one more detail-page entry is allowed. */
    fun requiredDetailDelayMs(
        timestamps: Collection<Long>,
        nowMs: Long,
    ): Long {
        val retained = pruneDetailEntries(timestamps, nowMs)
        return detailWindows.maxOf { window ->
            val inWindow = retained.filter { nowMs - it < window.durationMs }
            if (inWindow.size < window.maxEntries) {
                0L
            } else {
                val releaseIndex = inWindow.size - window.maxEntries
                (
                    inWindow[releaseIndex] + window.durationMs +
                        SAFETY_MARGIN_MS - nowMs
                    ).coerceAtLeast(0L)
            }
        }
    }
}
