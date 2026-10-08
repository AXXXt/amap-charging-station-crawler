package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.StationDetail

/**
 * Parses the lightweight snapshot that AMap already shows inside one
 * search-result card: the current price plus aggregated idle/total pile
 * counts. It never opens the station detail page, so per-pile states and
 * time-of-use price ladders stay whatever the last full detail round captured.
 */
object SearchListSnapshotParser {
    private val priceRegex = Regex(
        pattern = "(?:￥|¥)?\\s*([0-9]+(?:\\.[0-9]+)?)\\s*(?:元\\s*/\\s*(?:kWh|度(?:电)?)|元/度电|元/度|元/kWh|元)",
        options = setOf(RegexOption.IGNORE_CASE),
    )
    private val idleRegex = Regex("(?:空闲|可充|空余|剩余)\\s*([0-9]+)\\s*(?:根|个)?")
    private val busyRegex = Regex("(?:忙碌|充电中|使用中|占用|已满)\\s*([0-9]+)\\s*(?:根|个)?")

    private data class TypeCounts(val total: String, val idle: String)

    private fun parseTypeCounts(joined: String, keywords: List<String>): TypeCounts {
        var total = ""
        var idle = ""
        for (keyword in keywords) {
            if (total.isEmpty()) {
                val withUnit = Regex("$keyword[^0-9]{0,4}([0-9]+)\\s*(?:根|个)").find(joined)
                val beforeStatus = Regex(
                    "$keyword[^0-9]{0,4}([0-9]+)(?=[^0-9]{0,4}(?:空闲|可充|空余|剩余|忙碌|充电中|使用中|占用|已满))"
                ).find(joined)
                val match = withUnit ?: beforeStatus
                if (match != null) total = match.groupValues[1]
            }
            if (idle.isEmpty()) {
                val match = Regex("$keyword[^\\n]{0,12}?(?:空闲|可充|空余|剩余)\\s*([0-9]+)")
                    .find(joined)
                if (match != null) idle = match.groupValues[1]
            }
        }
        return TypeCounts(total, idle)
    }

    private fun clampIdle(idle: String, total: String): String {
        val idleValue = idle.toIntOrNull() ?: return ""
        val totalValue = total.toIntOrNull() ?: return idle
        return minOf(idleValue, totalValue).toString()
    }

    fun parse(
        requestedName: String,
        matchedName: String,
        values: List<String>,
        address: String = "",
        operator: String = "",
        stationId: String = "",
    ): StationDetail {
        val joined = (listOf(matchedName, requestedName) + values)
            .filter { it.isNotBlank() }
            .joinToString("\n")
        val price = priceRegex.findAll(joined)
            .mapNotNull { it.groupValues[1].toBigDecimalOrNull() }
            .map { it.toPlainString() }
            .firstOrNull()
            .orEmpty()

        val fast = parseTypeCounts(joined, listOf("快充", "快"))
        val superType = parseTypeCounts(joined, listOf("超充", "超"))
        val slow = parseTypeCounts(joined, listOf("慢充", "慢"))
        val overallIdle = idleRegex.find(joined)?.groupValues?.get(1).orEmpty()
        val overallBusy = busyRegex.find(joined)?.groupValues?.get(1).orEmpty()

        val totals = linkedMapOf("fast" to fast.total, "super" to superType.total, "slow" to slow.total)
        val idles = linkedMapOf("fast" to fast.idle, "super" to superType.idle, "slow" to slow.idle)
        val present = totals.filterValues { (it.toIntOrNull() ?: 0) > 0 }.keys.toList()

        if (present.size == 1) {
            val only = present.first()
            if (idles[only].isNullOrEmpty()) {
                val derived = when {
                    overallIdle.isNotBlank() -> clampIdle(overallIdle, totals[only].orEmpty())
                    overallBusy.isNotBlank() -> {
                        val total = totals[only]!!.toIntOrNull() ?: 0
                        val busy = overallBusy.toIntOrNull() ?: 0
                        maxOf(0, total - busy).toString()
                    }
                    else -> ""
                }
                idles[only] = derived
            } else {
                idles[only] = clampIdle(idles[only]!!, totals[only].orEmpty())
            }
        } else if (present.size > 1 && idles.values.all { it.isNullOrEmpty() } && overallIdle.isNotBlank()) {
            // The list card can expose only one aggregate idle number while
            // still naming several pile types. Attribute it to the largest
            // group instead of counting it once per type.
            val biggest = present.maxByOrNull { totals[it]?.toIntOrNull() ?: 0 }
            if (biggest != null) idles[biggest] = clampIdle(overallIdle, totals[biggest].orEmpty())
        }

        val totalSum = totals.values.mapNotNull { it.toIntOrNull() }.sum()
        val chargingPileTotal = if (totalSum > 0) {
            totalSum
        } else {
            (overallIdle.toIntOrNull() ?: 0) + (overallBusy.toIntOrNull() ?: 0)
        }

        return StationDetail(
            stationName = matchedName.ifBlank { requestedName },
            address = address,
            operator = operator,
            currentPrice = price,
            fastAvailable = idles["fast"].orEmpty(),
            fastTotal = totals["fast"].orEmpty(),
            superAvailable = idles["super"].orEmpty(),
            superTotal = totals["super"].orEmpty(),
            slowAvailable = idles["slow"].orEmpty(),
            slowTotal = totals["slow"].orEmpty(),
            chargingPileTotal = chargingPileTotal,
            chargingPiles = emptyList(),
            sourcePage = "SEARCH_LIST",
            collectionMode = "LIST_SNAPSHOT",
        )
    }
}
