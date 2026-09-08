package com.tigercode.evcollector.core.model

data class ChargingPileDetail(
    val deviceId: String = "",
    val chargingType: String = "",
    val status: String = "",
    val ratedPower: String = "",
    val ratedCurrent: String = "",
    val ratedVoltage: String = "",
    val equipmentType: String = "",
    val standard: String = "",
) {
    val filledFieldCount: Int
        get() = listOf(
            deviceId,
            chargingType,
            status,
            ratedPower,
            ratedCurrent,
            ratedVoltage,
            equipmentType,
            standard,
        ).count { it.isNotBlank() }
}
