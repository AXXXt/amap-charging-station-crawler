package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.NetworkErrorDetector
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NetworkErrorDetectorTest {
    @Test
    fun detectsNetworkErrorPage() {
        assertEquals(
            "没网了",
            NetworkErrorDetector.find("没网了\n刷新重试"),
        )
    }

    @Test
    fun detectsPhraseSplitAcrossAccessibilityNodes() {
        assertEquals(
            "网络好像开小差了",
            NetworkErrorDetector.find("咿...网络\n好像开小差了"),
        )
    }

    @Test
    fun ignoresLoadedDetail() {
        assertNull(
            NetworkErrorDetector.find(
                "测试重卡充电站\n营业时间\n24小时营业\n￥0.62/度",
            ),
        )
    }
}
