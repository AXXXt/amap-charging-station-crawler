package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot

/** Recommendation cards below the POI belong to other stations. */
object StationDetailScope {
    private fun isRecommendationHeader(node: NodeSnapshot): Boolean =
        listOf(node.text, node.contentDescription).any {
            it.trim() == "相似服务推荐" || it.trim().startsWith("相似服务推荐，")
        }

    fun hasRecommendations(root: NodeSnapshot): Boolean =
        isRecommendationHeader(root) || root.children.any(::hasRecommendations)

    fun withoutRecommendations(root: NodeSnapshot): NodeSnapshot {
        var reachedRecommendations = false
        fun trim(node: NodeSnapshot): NodeSnapshot? {
            if (reachedRecommendations) return null
            if (isRecommendationHeader(node)) {
                reachedRecommendations = true
                return null
            }
            return node.copy(children = node.children.mapNotNull(::trim))
        }
        return trim(root) ?: NodeSnapshot()
    }
}
