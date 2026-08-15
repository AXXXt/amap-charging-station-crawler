package com.tigercode.evcollector.core.model

data class StationCandidate(
    val name: String = "",
    val id: String = "",
    val address: String = "",
    val latitude: Double? = null,
    val longitude: Double? = null,
    val centerX: Int? = null,
    val centerY: Int? = null,
    val bounds: Rect? = null,
    val searchQuery: String = "",
    val clickVisible: Boolean = false,
)
