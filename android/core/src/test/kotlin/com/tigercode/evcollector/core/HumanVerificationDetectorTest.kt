package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.HumanVerificationDetector
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HumanVerificationDetectorTest {
    @Test
    fun detectsSliderVerificationPage() {
        val pageText = """
            亲，请按照说明进行验证哦
            拖动滑块出现完整的一个蝴蝶后就松开
            请按照说明拖动滑块
        """.trimIndent()

        assertEquals("请按照说明进行验证", HumanVerificationDetector.find(pageText))
    }

    @Test
    fun detectsPhraseSplitAcrossAccessibilityNodes() {
        assertEquals(
            "请按照说明拖动滑块",
            HumanVerificationDetector.find("请按照说明\n拖动滑块"),
        )
    }

    @Test
    fun ignoresOrdinaryStationDetail() {
        assertNull(
            HumanVerificationDetector.find(
                "测试重卡充电站\n营业时间\n24小时\n￥0.62/度",
            ),
        )
    }

    @Test
    fun detectsAccountControlPage() {
        assertEquals("封控", HumanVerificationDetector.find("当前账号已被封控"))
    }
}
