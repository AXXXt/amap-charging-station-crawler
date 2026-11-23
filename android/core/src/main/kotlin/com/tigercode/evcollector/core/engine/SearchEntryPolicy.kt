package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.Rect
import com.tigercode.evcollector.core.parser.StationNameNormalizer

object SearchEntryPolicy {
    const val CLICK_JITTER_PX = 7

    fun isTapObscuredByKeyboard(x: Int, y: Int, keyboardBounds: List<Rect>): Boolean =
        keyboardBounds.any { bounds ->
            bounds.isValid &&
                x >= bounds.left - CLICK_JITTER_PX && x < bounds.right + CLICK_JITTER_PX &&
                y >= bounds.top - CLICK_JITTER_PX && y < bounds.bottom + CLICK_JITTER_PX
        }

    // Hiding the IME may clear focus. Require the query in an actual enabled
    // editor near the top, rather than mistaking a detail-page title for a row.
    fun hasQueryEditor(root: NodeSnapshot, query: String): Boolean {
        val expected = StationNameNormalizer.normalize(query)
        if (expected.isEmpty()) return false
        val topLimit = root.bounds.top + maxOf(220, root.bounds.height / 4)
        fun walk(node: NodeSnapshot): Boolean {
            if (node.className == "android.widget.EditText" && node.enabled &&
                node.bounds.isValid && node.bounds.top < topLimit &&
                StationNameNormalizer.normalize(node.text) == expected
            ) return true
            return node.children.any(::walk)
        }
        return walk(root)
    }
}
