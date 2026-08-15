package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.parser.DetailParser
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DetailParserTest {
    private fun root(xml: String): NodeSnapshot = checkNotNull(XmlSnapshotParser.parse(xml))

    @Test
    fun basicDetailAddressAndMissingHoursAreNormalized() {
        val xml = """
            <hierarchy>
                <node content-desc="汽车充电站(测试站)" />
                <node text="地上 | 人民路160号" />
                <node text="暂无营业时间" />
            </hierarchy>
        """.trimIndent()

        val result = DetailParser.parse(root(xml))
        assertEquals("人民路160号", result.address)
        assertEquals("暂无营业时间", result.businessHours)
    }

    @Test
    fun compactDetailExtractsBusinessStatusAsHours() {
        val xml = """
            <hierarchy>
                <node content-desc="陇海路临湖路重卡汽车充电站" />
                <node text="营业中" />
                <node text="24小时营业" />
            </hierarchy>
        """.trimIndent()

        assertEquals("24小时营业", DetailParser.parse(root(xml)).businessHours)
    }

    @Test
    fun superchargeStationNameIsExtracted() {
        val xml = """
            <hierarchy>
                <node content-desc="铁门锦阳重卡超充站(星轲能源JM)" />
                <node text="铁门镇" />
            </hierarchy>
        """.trimIndent()

        assertEquals("铁门锦阳重卡超充站(星轲能源JM)", DetailParser.parse(root(xml)).stationName)
    }

    @Test
    fun heavyTruckAndSwapStationNamesAreExtracted() {
        val names = listOf("淇县窦氏重卡站", "汽车充换电站(启源充换电站安阳安林路站)")
        for (stationName in names) {
            val result = DetailParser.parse(root("<hierarchy><node content-desc=\"$stationName\" /></hierarchy>"))
            assertEquals(stationName, result.stationName)
        }
    }

    @Test
    fun poiSummaryCardInfersOperatorAndEmbeddedFacility() {
        val xml = """
            <hierarchy>
                <node content-desc="云快充汽车充电站(畅行重卡2站)" />
                <node text="刚刚浏览" />
                <node text="距云快充汽车充电站(畅行重卡2站)·地上｜昆仑能源西北侧" />
                <node text="暂无更多内容" />
            </hierarchy>
        """.trimIndent()

        val result = DetailParser.parse(root(xml))
        assertEquals("云快充汽车充电站(畅行重卡2站)", result.stationName)
        assertEquals("云快充", result.operator)
        assertTrue("地上" in result.facilities)
    }
}
