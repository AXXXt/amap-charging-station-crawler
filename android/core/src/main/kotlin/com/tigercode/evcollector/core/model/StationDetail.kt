package com.tigercode.evcollector.core.model

data class StationDetail(
    val stationName: String = "",
    val tags: List<String> = emptyList(),
    val category: String = "",
    val facilities: List<String> = emptyList(),
    val businessHours: String = "",
    val distance: String = "",
    val duration: String = "",
    val address: String = "",
    val operator: String = "",
    val currentPrice: String = "",
    val parkingFee: String = "",
    val occupancyFee: String = "",
    val favoriteCount: String = "",
    val viewCount: String = "",
    val fastAvailable: String = "",
    val fastTotal: String = "",
    val fastPower: String = "",
    val superAvailable: String = "",
    val superTotal: String = "",
    val superPower: String = "",
    val slowAvailable: String = "",
    val slowTotal: String = "",
    val slowPower: String = "",
    val priceTrendTitle: String = "",
    val fastPrices: List<PricePeriod> = emptyList(),
    val slowPrices: List<PricePeriod> = emptyList(),
    val chargingPileTotal: Int = 0,
    val chargingPiles: List<ChargingPileDetail> = emptyList(),
) {
    val filledFieldCount: Int
        get() {
            val strings = listOf(
                stationName, category, businessHours, distance, duration, address,
                operator, currentPrice, parkingFee, occupancyFee, favoriteCount,
                viewCount, fastAvailable, fastTotal, fastPower, superAvailable,
                superTotal, superPower, slowAvailable, slowTotal, slowPower,
                priceTrendTitle,
            )
            return strings.count { it.isNotBlank() } +
                tags.size + facilities.size + fastPrices.size + slowPrices.size +
                chargingPiles.size
        }
}
