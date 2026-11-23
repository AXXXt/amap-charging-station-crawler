package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.AmapNavigationPolicy
import com.tigercode.evcollector.core.engine.StationCardCapturePolicy
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.Rect
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.StationDetailScope
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.*
import org.junit.Test

class AmapMissingDetailRegressionTest {
    private val station = "兰考服务区电动汽车充电站"
    private val recommendation = """
        <node text="相似服务推荐" />
        <node content-desc="云快充汽车充电站(兰考县皓诺集中式快速充电站场)" clickable="true" />
        <node text="快充" /><node text="空2/2" />
        <node text="￥" /><node text="1.31" /><node text="/度" />
        <node content-desc="其他汽车充电站" clickable="true" />
    """.trimIndent()

    private val ownFields = """
        <node text="快充" /><node text="空闲" /><node text="0" /><node text="/10" />
        <node text="￥" /><node text="0.85" /><node text="/度" />
    """.trimIndent()

    private fun page(content: String) = checkNotNull(XmlSnapshotParser.parse("""
        <hierarchy><node content-desc="$station" />
          <node text="地图" /><node text="电话" />
          <node text="河南省开封市兰考县兰考服务区兰南高速公路南地面停车场" />
          $content
        </hierarchy>
    """.trimIndent()))

    @Test fun missingOwnDataCannotBorrowRecommendationPriceOrPiles() {
        val detail = checkNotNull(StationCardCapturePolicy.read(page(recommendation), station))
        assertEquals("", detail.currentPrice)
        assertEquals("", detail.fastTotal)
        assertEquals(listOf("实时电价", "空闲/总枪数"), StationCardCapturePolicy.missingFields(detail))
    }

    @Test fun realZeroIdleAndOwnPriceSurviveRecommendationTrimming() {
        val detail = checkNotNull(StationCardCapturePolicy.read(page(ownFields + recommendation), station))
        assertEquals("0.85", detail.currentPrice)
        assertEquals("0", detail.fastAvailable)
        assertEquals("10", detail.fastTotal)
        assertTrue(StationCardCapturePolicy.isComplete(detail))
    }

    @Test fun nestedRecommendationCardsAndFollowingSiblingsAreExcluded() {
        val root = page("""<node>$ownFields<node>$recommendation</node></node><node text="1.99元/度" />""")
        val scoped = StationDetailScope.withoutRecommendations(root)
        assertFalse(StationDetailScope.hasRecommendations(scoped))
        val detail = checkNotNull(StationCardCapturePolicy.read(root, station))
        assertEquals("0.85", detail.currentPrice)
        assertEquals("10", detail.fastTotal)
    }

    @Test fun recommendationOnlyScrollPositionIsDetailAndCannotStartSearch() {
        val root = page(recommendation)
        assertEquals(PageKind.DETAIL, PageAssessor.assess(root).kind)
        assertFalse(AmapNavigationPolicy.isSearchSurface(root))
    }

    @Test fun validDetailWithoutRecommendationsStillRequiresBackNavigation() {
        val root = page("""<node text="电站信息" />$ownFields""")
        assertFalse(AmapNavigationPolicy.isSearchSurface(root))
    }

    @Test fun detailIsNotSearchJustBecauseItContainsAPlaceholderInput() {
        val root = page("""<node text="电站信息" /><node class="android.widget.EditText" bounds="[10,100][900,180]" />""")
        assertFalse(AmapNavigationPolicy.isSearchSurface(root))
    }

    @Test fun oldPoiHandleDoesNotQualifyAsTopSearchBar() {
        assertFalse(AmapNavigationPolicy.isTopSearchBar(Rect(0, 188, 1080, 210), true, 1080))
        assertTrue(AmapNavigationPolicy.isTopSearchBar(Rect(80, 100, 1020, 210), true, 1080))
        assertFalse(AmapNavigationPolicy.isTopSearchBar(Rect(80, 100, 1020, 210), false, 1080))
    }

    @Test fun searchResultsAndHomeAreUsableRecoveryDestinations() {
        val search = page("""<node text="在此区域搜索" /><node content-desc="目标充电站" clickable="true" />""")
        assertTrue(AmapNavigationPolicy.isSearchSurface(search))
        val home = NodeSnapshot(children = listOf(NodeSnapshot(viewId = "com.autonavi.minimap:id/maphome_searchbar_bg")))
        assertTrue(AmapNavigationPolicy.isSearchSurface(home))
    }

    @Test fun enabledEditorIsUsableButBlankAndDisabledEditorAreNot() {
        val editor = NodeSnapshot(className = "android.widget.EditText", bounds = Rect(80, 100, 1000, 200))
        assertTrue(AmapNavigationPolicy.isSearchSurface(editor))
        assertFalse(AmapNavigationPolicy.isSearchSurface(editor.copy(enabled = false)))
        assertFalse(AmapNavigationPolicy.isSearchSurface(NodeSnapshot()))
    }

    @Test fun priceSheetIsNotARecoveryDestination() {
        val sheet = page("""<node text="参考价" /><node text="服务费" /><node text="00:00-06:00" /><node text="06:00-24:00" />""")
        assertFalse(AmapNavigationPolicy.isSearchSurface(sheet))
    }
}
