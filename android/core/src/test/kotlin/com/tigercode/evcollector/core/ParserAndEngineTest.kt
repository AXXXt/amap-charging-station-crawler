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
    fun stationNameInsideTopSearchInputIsNotMatchedAsAResultCard() {
        val stationName = "新电途汽车充电站(林州市秒能快充集中式快速充电站有限公司)"
        val xml = """
            <hierarchy bounds="[0,0][1080,2280]">
                <node clickable="true"
                      text="$stationName"
                      bounds="[144,111][1021,199]" />
                <node clickable="true"
                      content-desc="$stationName(货车专用)"
                      bounds="[65,390][1015,565]">
                    <node text="刚刚浏览" />
                    <node text="充电站" />
                </node>
            </hierarchy>
        """.trimIndent()

        val snapshot = root(xml)
        val cards = StationMatcher.visibleStationCards(snapshot)
        val match = StationMatcher.bestMatch(snapshot, stationName)

        assertEquals(1, cards.size)
        assertTrue(checkNotNull(match).centerY > 300)
    }

    @Test
    fun wideFirstResultCardNearTopIsStillMatched() {
        val stationName = "测试重卡充电站"
        val xml = """
            <hierarchy bounds="[0,0][1080,2280]">
                <node clickable="true"
                      content-desc="$stationName"
                      bounds="[33,250][1047,520]">
                    <node text="河南省安阳市林州市" />
                    <node text="停车费：免费" />
                </node>
            </hierarchy>
        """.trimIndent()

        val snapshot = root(xml)

        assertEquals(1, StationMatcher.visibleStationCards(snapshot).size)
        assertEquals(385, StationMatcher.bestMatch(snapshot, stationName)?.centerY)
    }

    @Test
    fun mapBottomSheetUsesRecyclerViewAndKeepsVisibleStations() {
        val xml = """
            <hierarchy>
                <node content-desc="展开列表" bounds="[0,619][1080,708]" />
                <node class="androidx.recyclerview.widget.RecyclerView"
                      scrollable="true"
                      bounds="[0,703][1080,2068]">
                    <node clickable="true"
                          content-desc="郑州公用集团中原超级充电站"
                          bounds="[33,729][1047,1209]">
                        <node text="16.6公里" />
                        <node text="￥0.95/度" />
                    </node>
                    <node clickable="true"
                          content-desc="云快充超级充电站(汇能重卡超充站)"
                          bounds="[33,1209][1047,1689]">
                        <node text="30.9公里" />
                        <node text="停车费：免费" />
                    </node>
                </node>
            </hierarchy>
        """.trimIndent()

        val snapshot = root(xml)
        val viewport = StationMatcher.listViewport(snapshot)
        val cards = StationMatcher.visibleStationCards(snapshot)

        assertEquals(703, viewport?.top)
        assertEquals(2068, viewport?.bottom)
        assertEquals(2, cards.size)
        assertEquals("郑州公用集团中原超级充电站", cards[0].name)
    }

    @Test
    fun promotionHeaderIsNotAStationCard() {
        val xml = """
            <hierarchy>
                <node class="androidx.recyclerview.widget.RecyclerView"
                      scrollable="true"
                      bounds="[0,230][1080,2068]">
                    <node clickable="true"
                          content-desc="高德扫码充电补贴 扫描充电桩二维码可用"
                          bounds="[33,375][1047,562]">
                        <node text="领取" />
                        <node text="满20减10" />
                    </node>
                    <node clickable="true"
                          content-desc="特来电郑州新密白寨万禾重卡超充站"
                          bounds="[33,584][1047,1054]">
                        <node text="33.6公里" />
                        <node text="停车费：停车免费" />
                        <node text="￥0.78/度" />
                    </node>
                </node>
            </hierarchy>
        """.trimIndent()

        val cards = StationMatcher.visibleStationCards(root(xml))

        assertEquals(1, cards.size)
        assertEquals("特来电郑州新密白寨万禾重卡超充站", cards[0].name)
    }

    @Test
    fun tinyCardFragmentAtBottomIsKeptForLaterScrollButNotClicked() {
        val xml = """
            <hierarchy>
                <node class="androidx.recyclerview.widget.RecyclerView"
                      scrollable="true"
                      bounds="[0,703][1080,2068]">
                    <node clickable="true"
                          content-desc="完整可见重卡充电站"
                          bounds="[33,729][1047,1209]" />
                    <node clickable="true"
                          content-desc="底部只露出一点的充电站"
                          bounds="[33,2002][1047,2068]" />
                </node>
            </hierarchy>
        """.trimIndent()

        val cards = StationMatcher.visibleStationCards(root(xml))

        assertEquals(2, cards.size)
        assertEquals("完整可见重卡充电站", cards[0].name)
        assertTrue(cards[0].clickVisible)
        assertEquals("底部只露出一点的充电站", cards[1].name)
        assertTrue(!cards[1].clickVisible)
    }

    @Test
    fun recommendedListScrollUsesCardStepInsteadOfFullViewportFling() {
        val xml = """
            <hierarchy>
                <node class="androidx.recyclerview.widget.RecyclerView"
                      scrollable="true"
                      bounds="[0,240][1080,2240]">
                    <node clickable="true"
                          content-desc="甲重卡充电站"
                          bounds="[32,240][1048,700]">
                        <node text="停车费：免费" />
                    </node>
                    <node clickable="true"
                          content-desc="乙重卡充电站"
                          bounds="[32,700][1048,1160]">
                        <node text="停车费：免费" />
                    </node>
                    <node clickable="true"
                          content-desc="丙重卡充电站"
                          bounds="[32,1160][1048,1620]">
                        <node text="停车费：免费" />
                    </node>
                </node>
            </hierarchy>
        """.trimIndent()

        val distance = StationMatcher.recommendedScrollDistance(root(xml))

        assertTrue(distance != null)
        assertTrue(distance!! >= 460)
        assertTrue(distance < 900)
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
