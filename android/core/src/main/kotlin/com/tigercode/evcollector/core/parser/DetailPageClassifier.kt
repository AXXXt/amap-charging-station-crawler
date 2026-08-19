package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot

enum class DetailPageType(val label: String, val scrollsNeeded: Int) {
    BASIC("basic", 0),
    STANDARD("standard", 1),
    CLICK_TO_EXPAND("click_to_expand", 1),
    FULL_TREND("full_trend", 2),
}

data class DetailPageInfo(
    val type: DetailPageType,
    val features: Map<String, Boolean>,
    val scrollsNeeded: Int,
    val description: String,
)

object DetailPageClassifier {
    private val markers = linkedMapOf(
        "has_equipment" to listOf("空闲", "空"),
        "has_price_trend" to listOf("24小时价格趋势图"),
        "has_price_click" to listOf("涨至", "降至"),
        "has_parking" to listOf("停车费", "停车免费"),
        "has_occupancy" to listOf("占位费"),
        "has_business_hours" to listOf("营业时间"),
        "has_facilities" to listOf("卫生间", "休息室", "便利店", "重卡车位"),
        "has_price_section" to listOf("/度", "￥"),
    )

    private val uniformPriceRegex = Regex(
        """(全时段|全天|各时段|所有时段|任意时段|不分时段)?(价格|电价).{0,6}(一致|统一)|统一(价|电价|价格)"""
    )

    fun hasUniformPrice(root: NodeSnapshot): Boolean {
        val joined = collectText(root)
        if (uniformPriceRegex.containsMatchIn(joined)) return true
        // 高德有时会把固定文案拆成多个相邻节点，去掉空白后仍要能识别。
        return uniformPriceRegex.containsMatchIn(joined.replace(Regex("\\s+"), ""))
    }

    fun classify(root: NodeSnapshot): DetailPageInfo {
        val joined = collectText(root)
        val features = markers.mapValues { (_, keywords) -> keywords.any { it in joined } }

        val (type, scrolls, description) = when {
            features.getValue("has_price_trend") ->
                Triple(DetailPageType.FULL_TREND, 2, "完整：含24h价格趋势图")
            features.getValue("has_price_click") ->
                Triple(DetailPageType.CLICK_TO_EXPAND, 1, "需点击电价查看分时详情")
            features.getValue("has_equipment") ->
                Triple(DetailPageType.STANDARD, 1, "标准：含设备信息")
            features.getValue("has_parking") || features.getValue("has_occupancy") ->
                Triple(DetailPageType.STANDARD, 1, "标准：含停车信息")
            else ->
                Triple(DetailPageType.BASIC, 0, "基础：仅名称和地址")
        }

        return DetailPageInfo(type, features, scrolls, description)
    }

    private fun collectText(root: NodeSnapshot): String {
        val values = mutableListOf<String>()
        fun walk(node: NodeSnapshot) {
            if (node.text.isNotBlank()) values.add(node.text)
            if (node.contentDescription.isNotBlank()) values.add(node.contentDescription)
            if (node.viewId.isNotBlank()) values.add(node.viewId)
            node.children.forEach { walk(it) }
        }
        walk(root)
        return values.joinToString("\n")
    }
}
