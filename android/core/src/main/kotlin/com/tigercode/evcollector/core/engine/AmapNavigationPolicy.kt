package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.Rect
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.StationDetailScope

object AmapNavigationPolicy {
    fun isSearchSurface(root: NodeSnapshot): Boolean {
        if (StationDetailScope.hasRecommendations(root)) return false
        return when (PageAssessor.assess(root).kind) {
            PageKind.HOME, PageKind.SEARCH_RESULTS -> true
            PageKind.DETAIL, PageKind.PRICE_DETAIL, PageKind.POPUP -> false
            else -> hasEnabledSearchInput(root)
        }
    }

    private fun hasEnabledSearchInput(node: NodeSnapshot): Boolean =
        (node.className == "android.widget.EditText" && node.enabled && node.bounds.isValid) ||
            node.children.any(::hasEnabledSearchInput)

    fun isTopSearchBar(bounds: Rect, clickable: Boolean, screenWidth: Int): Boolean =
        clickable && screenWidth > 0 && bounds.isValid &&
            bounds.top in 60..280 && bounds.bottom <= 320 &&
            bounds.left <= 220 && bounds.right >= screenWidth - 220 &&
            bounds.width >= screenWidth * 0.55 &&
            // The collapsed POI handle is full-width but only 22 px high on MI 8 Lite.
            bounds.height >= screenWidth * 0.035
}
