package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.StationMatcher
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.parser.StationNameNormalizer
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class StationNameMatchingTest {
    private fun root(xml: String): NodeSnapshot = checkNotNull(XmlSnapshotParser.parse(xml))

    @Test
    fun normalizeRemovesRenderedEllipsis() {
        val full = "新电途汽车充电站(国家电网重卡超充站河南省周口市项城市高寺重卡超充站)"
        val truncated = "新电途汽车充电站(国家电网重卡超充站河南省周口市项城市高寺重卡..."

        assertTrue(StationNameNormalizer.normalize(full).startsWith(StationNameNormalizer.normalize(truncated)))
        assertEquals(StationNameNormalizer.normalize(full), StationNameNormalizer.normalize("$full…"))
    }

    @Test
    fun bestMatchAcceptsTruncatedTaskName() {
        val full = "新电途汽车充电站(国家电网重卡超充站河南省周口市项城市高寺重卡超充站)"
        val truncated = "新电途汽车充电站(国家电网重卡超充站河南省周口市项城市高寺重卡..."
        val xml = """
            <hierarchy bounds="[0,0][1080,2280]">
                <node clickable="true"
                      content-desc="$full"
                      bounds="[33,390][1047,760]" />
                <node clickable="true"
                      content-desc="国家电网汽车充电站(河南省周口市项城市高寺公共充电站)"
                      bounds="[33,780][1047,1150]" />
            </hierarchy>
        """.trimIndent()

        val match = StationMatcher.bestMatch(root(xml), truncated)

        assertEquals(full, match?.description)
        assertTrue(checkNotNull(match).score >= 800)
    }

    @Test
    fun bestMatchAcceptsRenderedCardTitle() {
        val full = "星星充电汽车充电站(星星充电-智凤扬新能源项城市贾岭镇李店村重卡充电站)"
        val truncated = "星星充电汽车充电站(星星充电-智凤扬新能源项城市贾岭镇李店村重..."
        val xml = """
            <hierarchy bounds="[0,0][1080,2280]">
                <node clickable="true"
                      content-desc="$truncated"
                      bounds="[33,390][1047,760]" />
            </hierarchy>
        """.trimIndent()

        val match = StationMatcher.bestMatch(root(xml), full)

        assertEquals(truncated, match?.description)
        assertTrue(checkNotNull(match).score >= 700)
    }

    @Test
    fun preferredDisplayNameKeepsTheFullerName() {
        val full = "国家电网汽车充电站(河南省郑州市巩义市310国道宜航汽车销售公司重卡充电站)"
        val truncated = "国家电网汽车充电站(河南省郑州市巩义市310国道宜航汽车销售公..."

        assertEquals(full, StationNameNormalizer.preferredDisplayName(truncated, full))
        assertEquals(full, StationNameNormalizer.preferredDisplayName(full, truncated))
    }

    @Test
    fun detectsGenericStationNames() {
        assertTrue(StationNameNormalizer.isGenericStationName("汽车充电站"))
        assertTrue(StationNameNormalizer.isGenericStationName("重卡充电站"))
        assertEquals(
            false,
            StationNameNormalizer.isGenericStationName("云快充汽车充电站(红旗岗李建发快速充电站)"),
        )
    }

    @Test
    fun bestSuggestionMatchAcceptsNameAndAddressOnly() {
        val xml = """
            <hierarchy bounds="[0,0][1080,2400]">
                <node clickable="true"
                      content-desc="中国石化汽车充电站(中国石化第二十九..."
                      bounds="[112,362][840,514]">
                    <node text="安阳市 殷都区文明大道西段安林高速上道口西500米" />
                </node>
            </hierarchy>
        """.trimIndent()

        val match = StationMatcher.bestSuggestionMatch(
            root(xml),
            "中国石化汽车充电站(中国石化第二十九充电站)",
        )

        assertEquals("中国石化汽车充电站(中国石化第二十九...", match?.description)
    }

    @Test
    fun bestSuggestionMatchPrefersPoiRowOverSearchAction() {
        val name = "云快充汽车充电站(柳河西充电站)"
        val xml = """
            <hierarchy bounds="[0,0][1080,2400]">
                <node clickable="true" content-desc="$name" bounds="[52,401][1048,461]" />
                <node clickable="true" content-desc="$name" bounds="[52,504][827,608]" />
                <node clickable="true" content-desc="打车" bounds="[837,503][921,609]" />
                <node clickable="true" content-desc="路线" bounds="[921,503][1047,609]" />
            </hierarchy>
        """.trimIndent()

        val match = StationMatcher.bestSuggestionMatch(root(xml), name)

        assertEquals(556, match?.centerY)
    }
}
