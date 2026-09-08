package com.tigercode.evcollector.core.model

data class NodeSnapshot(
    val text: String = "",
    val contentDescription: String = "",
    val viewId: String = "",
    val className: String = "",
    val bounds: Rect = Rect(),
    val clickable: Boolean = false,
    val scrollable: Boolean = false,
    val enabled: Boolean = true,
    val selected: Boolean = false,
    val focused: Boolean = false,
    val children: List<NodeSnapshot> = emptyList(),
)
