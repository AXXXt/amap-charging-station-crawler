package com.tigercode.evcollector.core.model

data class ChargingPileDialogSnapshot(
    val expectedTotal: Int = 0,
    val piles: List<ChargingPileDetail> = emptyList(),
)
