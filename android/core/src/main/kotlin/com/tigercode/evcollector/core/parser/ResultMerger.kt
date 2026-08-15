package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.StationDetail

object ResultMerger {
    private val neverOverwrite = setOf("stationName", "address")

    fun merge(first: StationDetail, second: StationDetail): StationDetail {
        return StationDetail(
            stationName = first.stationName,
            address = first.address,
            tags = mergeList(first.tags, second.tags),
            category = first.category.ifEmpty { second.category },
            facilities = mergeList(first.facilities, second.facilities),
            businessHours = first.businessHours.ifEmpty { second.businessHours },
            distance = first.distance.ifEmpty { second.distance },
            duration = first.duration.ifEmpty { second.duration },
            operator = first.operator.ifEmpty { second.operator },
            currentPrice = first.currentPrice.ifEmpty { second.currentPrice },
            parkingFee = first.parkingFee.ifEmpty { second.parkingFee },
            occupancyFee = first.occupancyFee.ifEmpty { second.occupancyFee },
            favoriteCount = first.favoriteCount.ifEmpty { second.favoriteCount },
            viewCount = first.viewCount.ifEmpty { second.viewCount },
            fastAvailable = first.fastAvailable.ifEmpty { second.fastAvailable },
            fastTotal = first.fastTotal.ifEmpty { second.fastTotal },
            fastPower = first.fastPower.ifEmpty { second.fastPower },
            superAvailable = first.superAvailable.ifEmpty { second.superAvailable },
            superTotal = first.superTotal.ifEmpty { second.superTotal },
            superPower = first.superPower.ifEmpty { second.superPower },
            slowAvailable = first.slowAvailable.ifEmpty { second.slowAvailable },
            slowTotal = first.slowTotal.ifEmpty { second.slowTotal },
            slowPower = first.slowPower.ifEmpty { second.slowPower },
            priceTrendTitle = first.priceTrendTitle.ifEmpty { second.priceTrendTitle },
            fastPrices = mergePrices(first.fastPrices, second.fastPrices),
            slowPrices = mergePrices(first.slowPrices, second.slowPrices),
        )
    }

    private fun mergeList(first: List<String>, second: List<String>): List<String> {
        val result = linkedSetOf<String>()
        result.addAll(first)
        result.addAll(second)
        return result.toList()
    }

    private fun mergePrices(
        first: List<com.tigercode.evcollector.core.model.PricePeriod>,
        second: List<com.tigercode.evcollector.core.model.PricePeriod>,
    ): List<com.tigercode.evcollector.core.model.PricePeriod> {
        val firstHasTime = first.any { it.time.isNotEmpty() }
        return if (firstHasTime || second.isEmpty()) first else second
    }
}
