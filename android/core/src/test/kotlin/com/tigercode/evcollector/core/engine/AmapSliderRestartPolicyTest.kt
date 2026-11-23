package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.engine.AmapSliderRestartPolicy as Policy
import org.junit.Assert.*
import org.junit.Test

class AmapSliderRestartPolicyTest {
    @Test fun sliderAndRetryPageRequestRestartWithoutGesture() {
        listOf("请滑动下方滑块完成验证", "向右 拖动验证", "拖动滑块填充拼图", "请点我重试", "点击框体重试").forEach {
            assertTrue(it, Policy.isSliderChallenge(it))
        }
    }
    @Test fun ordinaryDetailAndGenericVerificationDoNotTriggerRestart() {
        listOf("充电站 电价0.85元 空闲3", "安全验证", "请选择符合描述的图片", "网络加载失败").forEach {
            assertFalse(it, Policy.isSliderChallenge(it))
        }
    }
    @Test fun bannedAccountStillRequiresManualAttentionEvenWithSliderText() {
        assertFalse(Policy.isSliderChallenge("账户异常 请拖动滑块"))
        assertFalse(Policy.isSliderChallenge("账号已被封禁 滑块验证"))
    }
    @Test fun failedStopDoesNotStartRestOrAutomaticallyAdvance() {
        val waitingForStop = Policy.requested(7)
        assertEquals(Policy.Phase.STOPPING, Policy.advance(waitingForStop, 9_999_999L)?.phase)
        assertEquals(0L, waitingForStop.deadlineElapsedMs)
    }
    @Test fun restStartsAtConfirmedStopAndLastsFullTwoMinutes() {
        val stopped = Policy.afterStop(50_000L, 7)
        assertEquals(120_000L, Policy.remainingMs(stopped, 50_000L))
        assertEquals(Policy.Phase.RESTING, Policy.advance(stopped, 169_999L)?.phase)
        assertEquals(Policy.Phase.REOPENING, Policy.advance(stopped, 170_000L)?.phase)
    }
    @Test fun failedLaunchDoesNotAdvanceToCollection() {
        val opening = Policy.advance(Policy.afterStop(50_000L, 7), 170_000L)!!
        assertEquals(Policy.Phase.REOPENING, Policy.advance(opening, 9_999_999L)?.phase)
    }
    @Test fun warmupStartsAtConfirmedLaunchNotAtRestDeadline() {
        val launched = Policy.afterOpen(210_000L, 7)
        assertEquals(60_000L, Policy.remainingMs(launched, 210_000L))
        assertNotNull(Policy.advance(launched, 269_999L))
        assertNull(Policy.advance(launched, 270_000L))
    }
    @Test fun processRecreationPreservesRemainingWait() {
        val saved = Policy.afterStop(50_000L, 7)
        assertEquals(saved, Policy.restore(saved, 7))
        assertEquals(45_000L, Policy.remainingMs(Policy.restore(saved, 7), 125_000L))
    }
    @Test fun rebootCannotReuseAnElapsedClockDeadlineOrSkipRest() {
        listOf(Policy.afterStop(50_000L, 7), Policy.afterOpen(210_000L, 7)).forEach { saved ->
            val restored = Policy.restore(saved, 8)
            assertEquals(Policy.Phase.STOPPING, restored.phase)
            assertEquals(0L, restored.deadlineElapsedMs)
            assertEquals(8, restored.bootCount)
        }
    }
}
