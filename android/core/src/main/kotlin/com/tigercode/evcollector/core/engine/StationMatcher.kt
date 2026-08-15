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
    fun visibleStationCards(root: NodeSnapshot): List<StationCandidate> {
        val cards = mutableListOf<StationCandidate>()

        fun walk(node: NodeSnapshot) {
            val description = node.contentDescription.trim()
            val text = node.text.trim()
            val label = description.ifEmpty { text }
            if (node.clickable &&
                label.isNotEmpty() &&
                "充电" in label &&
                !label.startsWith("搜索框") &&
                node.bounds.isValid
            ) {
                cards.add(
                    StationCandidate(
                        name = label,
                        centerX = node.bounds.centerX,
                        centerY = node.bounds.centerY,
                        bounds = node.bounds,
                    )
                )
            }
            node.children.forEach { walk(it) }
        }

        walk(root)
        return cards.sortedWith(compareBy({ it.centerY ?: 0 }, { it.centerX ?: 0 }))
    }

    fun bestMatch(root: NodeSnapshot, stationName: String): StationMatch? {
        val targetName = StationNameNormalizer.normalize(stationName)
        var best: StationMatch? = null

        fun walk(node: NodeSnapshot) {
            if (node.clickable) {
                val description = node.contentDescription.trim()
                val text = node.text.trim()
                val label = description.ifEmpty { text }
                if (label.isNotEmpty() &&
                    "充电" in label &&
                    !label.startsWith("搜索框")
                ) {
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
}
