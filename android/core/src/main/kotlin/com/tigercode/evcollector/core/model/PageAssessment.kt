package com.tigercode.evcollector.core.model

data class PageAssessment(
    val kind: PageKind,
    val confidence: Double,
    val reasons: List<String>,
    val expectedStationVisible: Boolean = false,
)
