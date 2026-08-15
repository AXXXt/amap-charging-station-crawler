package com.tigercode.evcollector.accessibility

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.tigercode.evcollector.core.model.NodeSnapshot

object NodeSnapshotReader {
    fun toSnapshot(node: AccessibilityNodeInfo): NodeSnapshot {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val children = mutableListOf<NodeSnapshot>()
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                children.add(toSnapshot(child))
            } finally {
                child.recycle()
            }
        }
        return NodeSnapshot(
            text = node.text?.toString()?.trim().orEmpty(),
            contentDescription = node.contentDescription?.toString()?.trim().orEmpty(),
            viewId = node.viewIdResourceName.orEmpty(),
            className = node.className?.toString().orEmpty(),
            bounds = com.tigercode.evcollector.core.model.Rect(
                left = bounds.left,
                top = bounds.top,
                right = bounds.right,
                bottom = bounds.bottom,
            ),
            clickable = node.isClickable,
            scrollable = node.isScrollable,
            enabled = node.isEnabled,
            selected = node.isSelected,
            children = children,
        )
    }
}
