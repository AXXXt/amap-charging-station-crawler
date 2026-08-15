package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageAssessment
import com.tigercode.evcollector.core.model.PageKind

object PageAssessor {
    private data class PageValues(
        val texts: List<String>,
        val descriptions: List<String>,
        val clickableDescriptions: List<String>,
        val viewIds: List<String>,
    )

    private val popupKeywords = listOf(
        "仅在使用中允许",
        "使用应用时允许",
        "始终允许",
        "暂不更新",
        "以后再说",
        "我知道了",
    )

    private val timePeriodRegex = Regex("""\d{2}:\d{2}[-~]\d{2}:\d{2}""")

    fun assess(root: NodeSnapshot, expectedStation: String? = null): PageAssessment {
        val values = collectValues(root)
        val combinedValues = values.texts + values.descriptions
        val combinedText = combinedValues.joinToString("\n")
        val reasons = mutableListOf<String>()

        val expectedVisible = if (expectedStation != null) {
            val expectedName = StationNameNormalizer.normalize(expectedStation)
            combinedValues.any { value ->
                val normalized = StationNameNormalizer.normalize(value)
                expectedName.isNotEmpty() && normalized.length >= 4 &&
                    (expectedName in normalized || normalized in expectedName)
            }
        } else {
            false
        }

        if (popupKeywords.any { it in combinedText }) {
            reasons.add("popup_keyword")
            return PageAssessment(PageKind.POPUP, 0.9, reasons, expectedVisible)
        }

        val timePeriodCount = timePeriodRegex.findAll(combinedText).count()
        if (timePeriodCount >= 2 && "参考价" in combinedText && "服务费" in combinedText) {
            reasons.add("multiple_time_periods")
            reasons.add("fee_breakdown")
            return PageAssessment(PageKind.PRICE_DETAIL, 0.98, reasons, expectedVisible)
        }

        var detailScore = 0
        if ("电站信息" in combinedText) {
            detailScore += 5
            reasons.add("station_info_section")
        }
        if ("营业时间" in combinedText) {
            detailScore += 2
            reasons.add("business_hours")
        }
        if ("24小时营业" in combinedText || "营业中" in combinedText) {
            detailScore += 2
            reasons.add("business_status")
        }
        if ("24小时价格趋势图" in combinedText) {
            detailScore += 2
            reasons.add("price_trend")
        }
        if ("扫码" in combinedText) {
            detailScore += 1
            reasons.add("scan_action")
        }
        if ("地图" in values.texts && "电话" in values.texts) {
            detailScore += 2
            reasons.add("map_phone_actions")
        } else if ("地图" in values.texts) {
            detailScore += 1
            reasons.add("map_action")
        }
        if ("导航" in values.texts && "路线" in values.texts) {
            detailScore += 1
            reasons.add("navigation_actions")
        }

        val compactDetailActions = expectedVisible &&
            "详情" in values.texts &&
            "地图" in values.texts &&
            "导航" in values.texts &&
            "路线" in values.texts
        if (compactDetailActions) {
            reasons.add("compact_detail_actions")
            return PageAssessment(PageKind.DETAIL, 0.93, reasons, expectedVisible)
        }

        val poiDetailOverlay = "打车" in combinedText &&
            combinedValues.any { "收藏按钮" in it }
        if (poiDetailOverlay) {
            reasons.add("poi_detail_overlay")
            return PageAssessment(PageKind.DETAIL, 0.94, reasons, expectedVisible)
        }

        if (combinedValues.any { "驾车" in it && "公里" in it }) {
            detailScore += 2
            reasons.add("driving_summary")
        }
        if (expectedVisible) {
            detailScore += 1
            reasons.add("expected_station_visible")
        }

        val searchCardCount = values.clickableDescriptions.count { description ->
            "充电" in description && !description.startsWith("搜索框")
        }
        var searchScore = 0
        if ("在此区域搜索" in combinedText) {
            searchScore += 5
            reasons.add("search_area_action")
        }
        if (searchCardCount >= 2) {
            searchScore += 5
            reasons.add("multiple_search_cards")
        } else if (searchCardCount > 0) {
            searchScore += 1
            reasons.add("search_cards:$searchCardCount")
        }

        val poiSummaryCard = "展开列表" in combinedText &&
            "暂无更多内容" in combinedText &&
            "在此区域搜索" !in combinedText &&
            searchCardCount <= 1 &&
            (
                "刚刚浏览" in combinedText ||
                    Regex("""\d+人浏览""").containsMatchIn(combinedText) ||
                    "停车费" in combinedText ||
                    combinedValues.any { "充电" in it && !it.startsWith("搜索框") }
                )
        if (poiSummaryCard) {
            reasons.add("poi_summary_card")
            return PageAssessment(PageKind.DETAIL, 0.92, reasons, expectedVisible)
        }

        if (detailScore >= 5 && "在此区域搜索" !in combinedText) {
            val confidence = minOf(0.99, 0.72 + detailScore * 0.035)
            return PageAssessment(PageKind.DETAIL, confidence, reasons, expectedVisible)
        }

        if (searchScore >= 5) {
            val confidence = minOf(0.99, 0.75 + searchScore * 0.025)
            return PageAssessment(PageKind.SEARCH_RESULTS, confidence, reasons, expectedVisible)
        }

        if (("未找到信息" in combinedText && "换个搜索词" in combinedText) ||
            ("没有找到" in combinedText && "搜" in combinedText && "地点" in combinedText)
        ) {
            reasons.add("empty_search_results")
            return PageAssessment(PageKind.SEARCH_RESULTS, 0.85, reasons, expectedVisible)
        }

        if (values.viewIds.any { "maphome_searchbar_bg" in it } ||
            ("首页" in values.texts && "打车" in values.texts && "我的" in values.texts)
        ) {
            reasons.add("home_navigation")
            return PageAssessment(PageKind.HOME, 0.9, reasons, expectedVisible)
        }

        return PageAssessment(PageKind.UNKNOWN, 0.2, reasons, expectedVisible)
    }

    fun isDetailPage(root: NodeSnapshot, expectedStation: String? = null): Boolean =
        assess(root, expectedStation).kind == PageKind.DETAIL

    fun isSearchResultsPage(root: NodeSnapshot): Boolean =
        assess(root).kind == PageKind.SEARCH_RESULTS

    private fun collectValues(root: NodeSnapshot): PageValues {
        val texts = mutableListOf<String>()
        val descriptions = mutableListOf<String>()
        val clickableDescriptions = mutableListOf<String>()
        val viewIds = mutableListOf<String>()

        fun walk(node: NodeSnapshot) {
            val text = node.text.trim()
            val description = node.contentDescription.trim()
            if (text.isNotEmpty()) texts.add(text)
            if (description.isNotEmpty()) {
                descriptions.add(description)
                if (node.clickable) clickableDescriptions.add(description)
            }
            if (node.viewId.isNotEmpty()) viewIds.add(node.viewId)
            node.children.forEach { walk(it) }
        }

        walk(root)
        return PageValues(texts, descriptions, clickableDescriptions, viewIds)
    }
}
