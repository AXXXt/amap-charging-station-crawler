package com.tigercode.evcollector.core.engine

/** Timers start only after a confirmed force-stop/return, and a confirmed launch. */
object AmapSliderRestartPolicy {
    const val REST_MS = 120_000L
    const val WARMUP_MS = 60_000L
    enum class Phase { STOPPING, RESTING, REOPENING, WARMING }
    data class State(val phase: Phase, val deadlineElapsedMs: Long, val bootCount: Int)

    fun isSliderChallenge(text: String): Boolean {
        val compact = text.replace(Regex("\\s+"), "")
        if (listOf("账号异常", "账户异常", "账号封禁", "账户封禁", "已被封禁", "封控").any { it in compact }) return false
        return listOf("滑块", "拼图", "向右滑动验证", "向右拖动验证", "请点我重试", "点击框体重试")
            .any { it in compact }
    }

    fun requested(bootCount: Int) = State(Phase.STOPPING, 0L, bootCount)
    fun afterStop(nowElapsedMs: Long, bootCount: Int) =
        State(Phase.RESTING, nowElapsedMs + REST_MS, bootCount)
    fun afterOpen(nowElapsedMs: Long, bootCount: Int) =
        State(Phase.WARMING, nowElapsedMs + WARMUP_MS, bootCount)

    fun restore(state: State, currentBootCount: Int): State =
        if (state.bootCount == currentBootCount) state else requested(currentBootCount)

    fun remainingMs(state: State, nowElapsedMs: Long): Long =
        (state.deadlineElapsedMs - nowElapsedMs).coerceAtLeast(0L)

    fun advance(state: State, nowElapsedMs: Long): State? = when {
        remainingMs(state, nowElapsedMs) > 0L -> state
        state.phase == Phase.RESTING -> state.copy(phase = Phase.REOPENING, deadlineElapsedMs = 0L)
        state.phase == Phase.WARMING -> null
        else -> state
    }
}
