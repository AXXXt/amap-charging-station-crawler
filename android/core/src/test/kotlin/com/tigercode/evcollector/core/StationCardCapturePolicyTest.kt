package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.StationCardCapturePolicy as Policy
import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.core.parser.PriceDetailParser
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.*
import org.junit.Test

class StationCardCapturePolicyTest {
    private val station = "中国石化超级充电站(魏岗重卡充电站)"

    private fun page(content: String, stationName: String = station) = checkNotNull(
        XmlSnapshotParser.parse("""
            <hierarchy><node content-desc="$stationName" />
                <node text="营业时间" /><node text="周一至周日 00:00-24:00" />
                <node text="河南省信阳市潢川县魏岗镇" /><node text="电站信息" />
                <node text="地图" /><node text="电话" />$content
            </hierarchy>
        """.trimIndent())
    )

    private fun read(content: String) = checkNotNull(Policy.read(page(content), station))

    // Preserve the actual split text nodes captured from the MI 8 Lite station page.
    private val card = """
        <node text="超充" /><node text="空闲" /><node text="7" /><node text="/8" />
        <node text="250.0kW｜1000V" />
    """.trimIndent()
    private val price = """
        <node text="￥" /><node text="1.5" /><node text="/度" />
        <node text="00:00起降至¥0.56/度" />
    """.trimIndent()

    @Test fun capturedSplitNodesReadCurrentPriceAndSevenOfEight() {
        val detail = read(card + price)
        assertEquals("1.5", detail.currentPrice)
        assertEquals("7", detail.superAvailable)
        assertEquals("8", detail.superTotal)
        assertTrue(Policy.isComplete(detail))
    }

    @Test fun shellAndPriceAloneDoNotMakeACompleteCapture() {
        assertFalse(Policy.isComplete(read("")))
        assertEquals(listOf("空闲/总枪数"), Policy.missingFields(read(price)))
        assertEquals(listOf("实时电价"), Policy.missingFields(read(card)))
        val late = Policy.merge(Policy.merge(read(""), read(price)), read(card))
        assertTrue(Policy.isComplete(late))
    }

    @Test fun freshZeroIdleReplacesEarlierCountAndKeepsTariffMetadata() {
        val tariff = listOf(PricePeriod(time = "00:00-24:00", totalPrice = "1.5", serviceFee = "0.2"))
        val first = read(card + price).copy(
            fastPrices = tariff, priceScheduleCapturedAt = 123456L,
            priceScheduleReused = true, collectionMode = "VISIBLE_DETAIL",
        )
        val latest = read(card.replace("text=\"7\"", "text=\"0\"") + price)
        val merged = Policy.merge(first, latest)
        assertEquals("0", merged.superAvailable)
        assertEquals(8, merged.chargingPileTotal)
        assertEquals(tariff, merged.fastPrices)
        assertEquals(123456L, merged.priceScheduleCapturedAt)
        assertTrue(merged.priceScheduleReused)
        assertEquals("VISIBLE_DETAIL", merged.collectionMode)
        assertTrue(Policy.isComplete(merged))
    }

    @Test fun missingAndInvalidCountsAreNotInterpretedAsZeroIdle() {
        assertFalse(Policy.isComplete(StationDetail(currentPrice = "1.5", superTotal = "8")))
        assertFalse(Policy.isComplete(StationDetail(currentPrice = "1.5", superAvailable = "9", superTotal = "8")))
        assertFalse(Policy.isComplete(StationDetail(currentPrice = "1.5", superAvailable = "0", superTotal = "0")))
        val partial = Policy.merge(read(price), StationDetail(superTotal = "8"))
        assertFalse(Policy.isComplete(partial))
        assertEquals("8", partial.superTotal)
    }

    @Test fun blankReadDoesNotEraseEarlierValidLiveFields() {
        val merged = Policy.merge(read(card + price), read(""))
        assertEquals("1.5", merged.currentPrice)
        assertEquals("7", merged.superAvailable)
        assertTrue(Policy.isComplete(merged))
    }

    @Test fun priceSheetCannotSupplyHomePageCurrentPriceOrCounts() {
        val sheet = page("""
            <node text="参考价" /><node text="服务费" />
            <node text="00:00-06:00" /><node text="06:00-24:00" />
            $card$price
        """.trimIndent())
        assertNull(Policy.read(sheet, station))
    }

    @Test fun otherStationAndSearchListAreRejected() {
        assertNull(Policy.read(page(card + price, "其他重卡充电站"), station))
        val list = checkNotNull(XmlSnapshotParser.parse("""
            <hierarchy><node text="在此区域搜索" />
                <node content-desc="$station" clickable="true" />
                <node content-desc="其他重卡充电站" clickable="true" />
                <node text="暂无更多内容" />$card$price
            </hierarchy>
        """.trimIndent()))
        assertNull(Policy.read(list, station))
    }

    @Test fun tariffIsKeptWhenOwnPileStatusIsAbsentAndRecommendationsShowCounts() {
        val own = read("""
            <node text="￥" /><node text="0.77" /><node text="/度" />
            <node text="16:00起涨至¥1.33/度" />
            <node text="相似服务推荐" />
            <node text="其他充电站" /><node text="快充 空12/14" />
        """.trimIndent())
        assertEquals("0.77", own.currentPrice)
        assertEquals("", own.fastTotal)
        assertEquals("", own.fastAvailable)
        assertFalse(Policy.canSave(own))
        val sheet = checkNotNull(XmlSnapshotParser.parse("""
            <hierarchy>
                <node text="00:00-16:00" /><node text="参考价" /><node text="0.77" />
                <node text="电费:" /><node text="0.57" /><node text="服务费:" /><node text="0.20" />
                <node text="16:00-24:00" /><node text="参考价" /><node text="1.33" />
                <node text="电费:" /><node text="1.13" /><node text="服务费:" /><node text="0.20" />
            </hierarchy>
        """.trimIndent()))
        val withTariff = own.copy(fastPrices = PriceDetailParser.parse(sheet))
        assertEquals(2, withTariff.fastPrices.size)
        assertTrue(Policy.canSave(withTariff))
        assertFalse(Policy.isComplete(withTariff))
        assertEquals(listOf("空闲/总枪数"), Policy.missingFields(withTariff))
        val reread = Policy.merge(withTariff, StationDetail())
        assertTrue(Policy.canSave(reread))
        assertEquals("", reread.fastTotal)
        assertEquals(0, reread.chargingPileTotal)
        assertEquals(withTariff.fastPrices, reread.fastPrices)
    }

    @Test fun tariffWithoutHeadlinePriceCanBeSavedButAnEmptyOrInvalidSheetCannot() {
        val tariff = PricePeriod(time = "00:00-24:00", totalPrice = "0.77")
        val onlyTariff = StationDetail(fastPrices = listOf(tariff))
        assertTrue(Policy.canSave(onlyTariff))
        assertEquals(listOf("实时电价", "空闲/总枪数"), Policy.missingFields(onlyTariff))
        for (invalid in listOf(
            tariff.copy(totalPrice = ""), tariff.copy(totalPrice = "NaN"),
            tariff.copy(totalPrice = "-1"), tariff.copy(time = "当前时段"),
            tariff.copy(time = "00:00-24:01"), tariff.copy(time = "00:00-00:00"),
        )) {
            assertFalse(Policy.canSave(StationDetail(fastPrices = listOf(invalid))))
        }
        assertFalse(Policy.canSave(StationDetail()))
        assertTrue(Policy.canSave(StationDetail(slowPrices = listOf(tariff.copy(totalPrice = "0")))))
        // Existing complete live-only records remain collectable.
        assertTrue(Policy.canSave(read(card + price)))
    }

    @Test fun allVisibleChargeGroupsMustHaveValidCounts() {
        val detail = read(card + price).copy(fastAvailable = "3", fastTotal = "5")
        assertTrue(Policy.isComplete(detail))
        assertEquals(13, Policy.merge(detail, StationDetail()).chargingPileTotal)
        assertFalse(Policy.isComplete(detail.copy(fastAvailable = "")))
    }
}
