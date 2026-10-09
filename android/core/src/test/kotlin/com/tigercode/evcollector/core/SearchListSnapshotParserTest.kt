package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.parser.SearchListSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Test

class SearchListSnapshotParserTest {
    @Test
    fun parsesExplicitTypeRatio() {
        val name = "星星充电汽车充电站(安阳绿圆重卡充电站)"
        val detail = SearchListSnapshotParser.parse(
            requestedName = name,
            matchedName = name,
            values = listOf("超充\n空32/32\n重卡车位\n停车费：免费停车\n0.78"),
        )

        assertEquals("32", detail.superTotal)
        assertEquals("32", detail.superAvailable)
        assertEquals(32, detail.chargingPileTotal)
    }

    @Test
    fun ignoresPhoneNumberInStationName() {
        val name = "云快充超级充电站(新区南高速口北超充☎:156 6090 2959)"
        val detail = SearchListSnapshotParser.parse(
            requestedName = name,
            matchedName = name,
            values = listOf("重卡车位\n停车费：免费停车"),
        )

        assertEquals("", detail.superTotal)
        assertEquals("", detail.fastTotal)
        assertEquals("", detail.slowTotal)
        assertEquals(0, detail.chargingPileTotal)
    }

    @Test
    fun ignoresRoadNumberInStationName() {
        val name = "云快充汽车充电站(苗桥镇快充站-317省道)"
        val detail = SearchListSnapshotParser.parse(
            requestedName = name,
            matchedName = name,
            values = listOf("停车费：免费停车"),
        )

        assertEquals("", detail.fastTotal)
        assertEquals("", detail.superTotal)
        assertEquals(0, detail.chargingPileTotal)
    }

    @Test
    fun ignoresPhoneNumberInDetailText() {
        val detail = SearchListSnapshotParser.parse(
            requestedName = "测试站点",
            matchedName = "测试站点",
            values = listOf("超充☎:156 6090 2959\n停车费：免费停车"),
        )

        assertEquals("", detail.superTotal)
        assertEquals(0, detail.chargingPileTotal)
    }

    @Test
    fun parsesFastRatio() {
        val detail = SearchListSnapshotParser.parse(
            requestedName = "测试快充站",
            matchedName = "测试快充站",
            values = listOf("快充 空3/4\n停车费：免费停车"),
        )

        assertEquals("4", detail.fastTotal)
        assertEquals("3", detail.fastAvailable)
        assertEquals(4, detail.chargingPileTotal)
    }
}
