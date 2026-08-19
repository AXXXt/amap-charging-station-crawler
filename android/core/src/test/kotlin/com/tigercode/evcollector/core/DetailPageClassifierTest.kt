package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.parser.DetailPageClassifier
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DetailPageClassifierTest {
    private fun root(text: String): NodeSnapshot =
        checkNotNull(XmlSnapshotParser.parse("""<hierarchy><node text="$text" /></hierarchy>"""))

    @Test
    fun uniformPricePhrasesAreDetected() {
        val phrases = listOf(
            "全时段价格一致",
            "全时段统一价",
            "全时段统一电价",
            "全天价格一致",
            "全天统一价",
            "各时段价格一致",
            "所有时段价格一致",
            "价格一致",
            "统一电价",
        )
        for (phrase in phrases) {
            assertTrue("应识别为统一价: $phrase", DetailPageClassifier.hasUniformPrice(root(phrase)))
        }
    }

    @Test
    fun trendTitleAndPriceLabelsAreNotUniformPrice() {
        val phrases = listOf(
            "24小时价格趋势图",
            "16:00起涨至¥1.41/度",
            "07:00起降至¥0.88/度",
            "￥0.7/度",
        )
        for (phrase in phrases) {
            assertFalse("不应识别为统一价: $phrase", DetailPageClassifier.hasUniformPrice(root(phrase)))
        }
    }

    @Test
    fun splitUniformPriceNodesAreDetected() {
        val xml = """
            <hierarchy>
                <node text="全时段" />
                <node text="统一价" />
            </hierarchy>
        """.trimIndent()

        assertTrue(DetailPageClassifier.hasUniformPrice(checkNotNull(XmlSnapshotParser.parse(xml))))
    }
}
