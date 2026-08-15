package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.Deduplicator
import com.tigercode.evcollector.core.engine.StationRecord
import com.tigercode.evcollector.core.engine.StationMatcher
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.core.parser.PriceDetailParser
import com.tigercode.evcollector.core.parser.ResultMerger
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ParserAndEngineTest {
    private fun root(xml: String): NodeSnapshot = checkNotNull(XmlSnapshotParser.parse(xml))

    @Test
    fun priceDetailParserHandlesSplitPricesAndTags() {
        val xml = """
            <hierarchy>
                <node text="00:00-07:00" />
                <node text="最低" />
                <node text="参考价" />
                <node text="0" />
                <node text=".59" />
                <node text="电费:" />
                <node text="0" />
                <node text=".40" />
                <node text="服务费:" />
                <node text=".19" />
                <node text="07:00-16:00" />
                <node text="参考价" />
                <node text="0.89" />
                <node text="电费" />
                <node text="0.60" />
                <node text="服务费" />
                <node text="0.29" />
            </hierarchy>
        """.trimIndent()

        val periods = PriceDetailParser.parse(root(xml))
        assertEquals(2, periods.size)
        assertEquals("00:00-07:00", periods[0].time)
        assertEquals("最低", periods[0].tag)
        assertEquals("0.59", periods[0].totalPrice)
        assertEquals("0.40", periods[0].elecFee)
        assertEquals("0.19", periods[0].serviceFee)
        assertEquals("0.89", periods[1].totalPrice)
    }

    @Test
    fun resultMergerKeepsAddressAndFillsMissingFields() {
        val first = StationDetail(stationName = "甲充电站", address = "甲路1号", operator = "")
        val second = StationDetail(stationName = "乙充电站", address = "乙路2号", operator = "特来电")

        val merged = ResultMerger.merge(first, second)

        assertEquals("甲充电站", merged.stationName)
        assertEquals("甲路1号", merged.address)
        assertEquals("特来电", merged.operator)
    }

    @Test
    fun visibleCardsAndBestMatch() {
        val xml = """
            <hierarchy>
                <node clickable="true"
                      content-desc="测试重卡充电站"
                      bounds="[36,700][1044,1200]" />
                <node clickable="true"
                      content-desc="搜索框，重卡充电站"
                      bounds="[0,100][1080,300]" />
            </hierarchy>
        """.trimIndent()

        val cards = StationMatcher.visibleStationCards(root(xml))
        assertEquals(1, cards.size)
        assertEquals(540, cards[0].centerX)
        assertEquals(950, cards[0].centerY)

        val match = StationMatcher.bestMatch(root(xml), "测试重卡充电站")
        assertEquals(1000, match?.score)
    }

    @Test
    fun visibleCardsFallsBackToTextLabel() {
        val xml = """
            <hierarchy>
                <node clickable="true"
                      text="物流园充电站"
                      bounds="[10,400][1040,760]" />
            </hierarchy>
        """.trimIndent()

        val cards = StationMatcher.visibleStationCards(root(xml))

        assertEquals(1, cards.size)
        assertEquals("物流园充电站", cards[0].name)
    }

    @Test
    fun deduplicatorMergesSameNameCloseStations() {
        val records = listOf(
            StationRecord("甲充电站", 113.1, 34.1, 5),
            StationRecord("甲充电站", 113.1002, 34.1002, 9),
            StationRecord("乙充电站", 113.5, 34.5, 4),
        )

        val (kept, removed) = Deduplicator.deduplicate(records)

        assertEquals(2, kept.size)
        assertEquals(1, removed)
        assertEquals(9, kept[0].filledCount)
    }
}
