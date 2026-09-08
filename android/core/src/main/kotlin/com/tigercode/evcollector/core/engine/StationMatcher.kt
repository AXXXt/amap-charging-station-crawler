package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.StationCandidate
import com.tigercode.evcollector.core.parser.StationNameNormalizer

data class StationMatch(
    val score: Int,
    val centerX: Int,
    val centerY: Int,
    val description: String,
)

object StationMatcher {
    private val excludedKeywords = listOf(
        "搜索框",
        "扫码充电补贴",
        "扫码优惠",
        "充电桩二维码",
        "高德红包",
        "领取",
        "查看地图",
        "展开列表",
    )

    private val stationDetailKeywords = listOf(
        "公里",
        "停车费",
        "/度",
        "桩",
        "快充",
        "超充",
        "有人充电",
    )

    fun listViewport(root: NodeSnapshot): com.tigercode.evcollector.core.model.Rect? {
        var best: NodeSnapshot? = null

        fun walk(node: NodeSnapshot) {
            if (node.scrollable && node.bounds.isValid) {
                val isRecyclerView = node.className.contains("RecyclerView", ignoreCase = true)
                val bestIsRecyclerView = best?.className?.contains("RecyclerView", ignoreCase = true) == true
                val area = (node.bounds.right - node.bounds.left).toLong() *
                    (node.bounds.bottom - node.bounds.top).toLong()
                val bestArea = best?.let {
                    (it.bounds.right - it.bounds.left).toLong() *
                        (it.bounds.bottom - it.bounds.top).toLong()
                } ?: -1L
                if (best == null || (isRecyclerView && !bestIsRecyclerView) ||
                    (isRecyclerView == bestIsRecyclerView && area > bestArea)
                ) {
                    best = node
                }
            }
            node.children.forEach(::walk)
        }

        walk(root)
        return best?.bounds
    }

    fun visibleStationCards(root: NodeSnapshot): List<StationCandidate> {
        val cards = mutableListOf<StationCandidate>()
        val viewport = listViewport(root)

        fun walk(node: NodeSnapshot) {
            val description = node.contentDescription.trim()
            val text = node.text.trim()
            val label = description.ifEmpty { text }
            val subtreeText = collectText(node)
            if (node.clickable && isStationCard(node, label, subtreeText, viewport, root.bounds)) {
                val clickVisible = node.bounds.height >= 180
                cards.add(
                    StationCandidate(
                        name = label,
                        centerX = node.bounds.centerX,
                        centerY = node.bounds.centerY,
                        bounds = node.bounds,
                        clickVisible = clickVisible,
                    )
                )
            }
            node.children.forEach { walk(it) }
        }

        walk(root)
        return cards
            .distinctBy { "${StationNameNormalizer.normalize(it.name)}:${it.bounds}" }
            .sortedWith(compareBy({ it.centerY ?: 0 }, { it.centerX ?: 0 }))
    }

    /**
     * Return a conservative swipe distance for a paged search-result list.
     *
     * A full-screen swipe is not a stable unit across devices: on a 2400 px
     * screen it can move several AMap cards at once. Use the typical visible
     * card height instead so that previous cards remain on screen as an
     * overlap anchor after the swipe.
     */
    fun recommendedScrollDistance(root: NodeSnapshot): Int? {
        val viewport = listViewport(root) ?: return null
        val cardHeights = visibleStationCards(root)
            .mapNotNull { it.bounds?.height }
            .filter { it in 160..760 }

        if (cardHeights.isEmpty()) {
            // A missing card height is normally a transient accessibility
            // snapshot. Do not fling through a full page in that case.
            return (viewport.height * 0.28f).toInt().coerceIn(260, 620)
        }

        val sorted = cardHeights.sorted()
        val median = sorted[sorted.size / 2]
        val overlapMargin = maxOf(18, median / 12)
        val viewportCap = maxOf(260, (viewport.height * 0.42f).toInt())
        return (median + overlapMargin).coerceIn(260, viewportCap)
    }

    fun bestMatch(root: NodeSnapshot, stationName: String): StationMatch? {
        val targetName = StationNameNormalizer.normalize(stationName)
        var best: StationMatch? = null

        fun walk(node: NodeSnapshot) {
            if (node.clickable) {
                val description = node.contentDescription.trim()
                val text = node.text.trim()
                val label = description.ifEmpty { text }
                if (isStationCard(node, label, collectText(node), null, root.bounds)) {
                    val candidateName = StationNameNormalizer.normalize(label)
                    val score = when {
                        candidateName.isEmpty() -> 0
                        candidateName == targetName -> 1000
                        targetName in candidateName -> 800 + targetName.length
                        candidateName in targetName -> 700 + candidateName.length
                        else -> 0
                    }
                    if (score > 0 && node.bounds.isValid) {
                        val candidate = StationMatch(
                            score = score,
                            centerX = node.bounds.centerX,
                            centerY = node.bounds.centerY,
                            description = label,
                        )
                        if (best == null || candidate.score > best!!.score) {
                            best = candidate
                        }
                    }
                }
            }
            node.children.forEach { walk(it) }
        }

        walk(root)
        return best
    }

    private fun isStationCard(
        node: NodeSnapshot,
        label: String,
        subtreeText: String,
        viewport: com.tigercode.evcollector.core.model.Rect?,
        rootBounds: com.tigercode.evcollector.core.model.Rect,
    ): Boolean {
        if (!node.clickable || label.isBlank() || !node.bounds.isValid) return false
        if (excludedKeywords.any { it in label || it in subtreeText }) return false
        if (isTopSearchInput(node, rootBounds)) return false
        val hasStationName =
            (label.contains("充") && label.contains("站")) || label.contains("电站")
        if (!hasStationName) return false
        if (viewport != null && visibleHeight(node.bounds, viewport) <= 0) return false
        if (node.bounds.height < 48) return false
        return stationDetailKeywords.any { it in subtreeText } || node.children.isEmpty()
    }

    private fun isTopSearchInput(
        node: NodeSnapshot,
        rootBounds: com.tigercode.evcollector.core.model.Rect,
    ): Boolean {
        val screenWidth = rootBounds.width.takeIf { it > 0 } ?: 1080
        val screenHeight = rootBounds.height.takeIf { it > 0 } ?: 2280
        val topLimit = rootBounds.top + maxOf(220, screenHeight / 7)
        return node.bounds.top < topLimit &&
            node.bounds.height <= maxOf(180, screenHeight / 10) &&
            node.bounds.width >= (screenWidth * 0.55f).toInt()
    }

    private fun visibleHeight(
        bounds: com.tigercode.evcollector.core.model.Rect,
        viewport: com.tigercode.evcollector.core.model.Rect,
    ): Int =
        (minOf(bounds.bottom, viewport.bottom) - maxOf(bounds.top, viewport.top)).coerceAtLeast(0)

    private fun collectText(node: NodeSnapshot): String {
        val values = mutableListOf<String>()
        fun walk(current: NodeSnapshot) {
            if (current.text.isNotBlank()) values.add(current.text)
            if (current.contentDescription.isNotBlank()) values.add(current.contentDescription)
            current.children.forEach(::walk)
        }
        walk(node)
        return values.joinToString("\n")
    }
}
