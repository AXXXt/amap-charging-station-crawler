package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.DetailPageClassifier
import com.tigercode.evcollector.core.parser.DetailPageType
import com.tigercode.evcollector.core.parser.StationNameNormalizer
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PageAssessorTest {
    private fun root(xml: String): NodeSnapshot = checkNotNull(XmlSnapshotParser.parse(xml))

    @Test
    fun searchCardsAreNotDetailPage() {
        val xml = """
            <hierarchy>
                <node text="在此区域搜索" />
                <node text="停车费 免费" />
                <node text="空闲" />
                <node text="￥0.51/度" />
                <node content-desc="测试重卡充电站" clickable="true" />
            </hierarchy>
        """.trimIndent()

        assertEquals(PageKind.SEARCH_RESULTS, PageAssessor.assess(root(xml), "测试重卡充电站").kind)
    }

    @Test
    fun emptySearchResultsIsStillSearchPage() {
        val xml = """
            <hierarchy>
                <node text="未找到信息，换个搜索词试试?" />
                <node text="您想查找的「某某重卡充电站」是个新地点吗？" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml))

        assertEquals(PageKind.SEARCH_RESULTS, assessment.kind)
        assertTrue(assessment.reasons.contains("empty_search_results"))
    }

    @Test
    fun scrolledSearchResultsWithoutAreaAction() {
        val xml = """
            <hierarchy>
                <node text="扫码" />
                <node content-desc="甲充电站" clickable="true" />
                <node content-desc="乙汽车充电站" clickable="true" />
                <node content-desc="丙重卡充电站" clickable="true" />
            </hierarchy>
        """.trimIndent()

        assertEquals(PageKind.SEARCH_RESULTS, PageAssessor.assess(root(xml)).kind)
    }

    @Test
    fun detailRequiresStrongDetailMarkers() {
        val xml = """
            <hierarchy>
                <node content-desc="测试重卡充电站" />
                <node text="营业时间" />
                <node text="电站信息" />
                <node text="地图" />
                <node text="电话" />
                <node text="导航" />
                <node text="路线" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml), "测试重卡充电站")
        assertEquals(PageKind.DETAIL, assessment.kind)
        assertTrue(assessment.expectedStationVisible)
    }

    @Test
    fun basicDetailWithoutPhoneOrStationInfo() {
        val xml = """
            <hierarchy>
                <node content-desc="汽车充电站(测试重卡充电站)" />
                <node text="暂无营业时间" />
                <node text="驾车88.3公里" />
                <node text="地图" />
                <node text="导航" />
                <node text="路线" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml), "汽车充电站(测试重卡充电站)")
        assertEquals(PageKind.DETAIL, assessment.kind)
        assertTrue(assessment.expectedStationVisible)
    }

    @Test
    fun compactDetailActionsAreDetailPage() {
        val xml = """
            <hierarchy>
                <node content-desc="陇海路临湖路重卡汽车充电站" clickable="true" />
                <node text="营业中" />
                <node text="24小时营业" />
                <node text="详情" />
                <node text="地图" />
                <node text="导航" />
                <node text="路线" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml), "陇海路临湖路重卡汽车充电站")
        assertEquals(PageKind.DETAIL, assessment.kind)
        assertTrue(assessment.reasons.contains("compact_detail_actions"))
    }

    @Test
    fun poiSummaryCardIsDetailPage() {
        val xml = """
            <hierarchy>
                <node content-desc="展开列表" />
                <node content-desc="云快充汽车充电站(畅行重卡2站)" />
                <node text="刚刚浏览" />
                <node text="距云快充汽车充电站(畅行重卡2站)·地上｜昆仑能源西北侧" />
                <node text="暂无更多内容" />
                <node content-desc="搜索框，云快充汽车充电站(畅行重卡2站)" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml), "云快充汽车充电站(畅行重卡2站)")
        assertEquals(PageKind.DETAIL, assessment.kind)
        assertTrue(assessment.reasons.contains("poi_summary_card"))
        assertTrue(assessment.expectedStationVisible)
    }

    @Test
    fun scrolledPoiDetailOverlayIsDetailPage() {
        val xml = """
            <hierarchy>
                <node content-desc="收藏按钮未收藏" clickable="true" />
                <node content-desc="打车" clickable="true" />
                <node content-desc="优惠，已选中" />
                <node text="全文" />
                <node text="占位特别严重，车上有人不充电占着位置在车里睡觉" />
            </hierarchy>
        """.trimIndent()

        val assessment = PageAssessor.assess(root(xml))

        assertEquals(PageKind.DETAIL, assessment.kind)
        assertTrue(assessment.reasons.contains("poi_detail_overlay"))
    }

    @Test
    fun priceDetailPage() {
        val xml = """
            <hierarchy>
                <node text="00:00-07:00" />
                <node text="07:00-16:00" />
                <node text="参考价" />
                <node text="电费" />
                <node text="服务费" />
            </hierarchy>
        """.trimIndent()

        assertEquals(PageKind.PRICE_DETAIL, PageAssessor.assess(root(xml)).kind)
    }

    @Test
    fun priceEntryAcceptsLabelsWithoutQiPrefix() {
        val xml = """
            <hierarchy>
                <node text="当前0.62元，14:00涨至0.88元" />
            </hierarchy>
        """.trimIndent()

        val info = DetailPageClassifier.classify(root(xml))

        assertEquals(DetailPageType.CLICK_TO_EXPAND, info.type)
        assertTrue(info.features["has_price_click"] == true)
    }

    @Test
    fun stationNameNormalization() {
        assertEquals(
            StationNameNormalizer.normalize("特来电汽车充电站（测试站）"),
            StationNameNormalizer.normalize("特来电充电站(测试站)"),
        )
    }
}
