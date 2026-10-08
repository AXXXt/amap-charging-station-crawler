package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.StationDetail

/**
 * Parses the lightweight snapshot that AMap already shows inside one
 * search-result card: the current price plus aggregated idle/total pile
 * counts. It never opens the station detail page, so per-pile states and
 * time-of-use price ladders stay whatever the last full detail round captured.
 *
 * AMap cards are inconsistent: the price may read "￥0.60/度", "0.60元/度" or
 * "0.60元/kWh", and pile lines may or may not carry a "根" suffix. Everything
 * is therefore parsed defensively with sanity checks so that unrelated numbers
 * (power ratings like 160KW, distances, fees) are never mistaken for a price.
 */
object SearchListSnapshotParser {
    private val yuanUnitPriceRegex = Regex(
        "([0-9]+(?:\\.[0-9]+)?)\\s*元\\s*/?\\s*(?:kWh|KW·h|kw·h|度(?:电)?)",
        RegexOption.IGNORE_CASE,
    )
    private val currencyPriceRegex = Regex("(?:￥|¥)\\s*([0-9]+(?:\\.[0-9]+)?)")
    private val bareYuanPriceRegex = Regex("([0-9]+(?:\\.[0-9]+)?)\\s*元")
    private val idleRegex = Regex("(?:空闲|可充|空余|剩余)\\s*([0-9]{1,3})")
    private val busyRegex = Regex("(?:忙碌|充电中|使用中|占用|已满)\\s*([0-9]{1,3})")
    private val overallRatioRegex = Regex("(?:空闲|空余|剩余|空)\\s*([0-9]{1,3})\\s*/\\s*([0-9]{1,3})")

    private data class TypeCounts(val total: String, val idle: String)

    private fun priceCandidate(text: String): String {
        val value = text.toDoubleOrNull() ?: return ""
        // A charging price outside 0.1 - 5.0 元/kWh is almost certainly a
        // power rating, distance or fee that leaked into the regex.
        if (value < 0.1 || value > 5.0) return ""
        return text
    }

    private fun extractPrice(joined: String): String {
        for (regex in listOf(yuanUnitPriceRegex, currencyPriceRegex, bareYuanPriceRegex)) {
            for (match in regex.findAll(joined)) {
                val candidate = priceCandidate(match.groupValues[1])
                if (candidate.isNotEmpty()) return candidate
            }
        }
        return ""
    }

    private fun stripPriceSpans(joined: String): String {
        var text = joined
        for (regex in listOf(yuanUnitPriceRegex, currencyPriceRegex, bareYuanPriceRegex)) {
            text = regex.replace(text, " ")
        }
        return text
    }

    private fun safeTotal(joined: String, index: Int, length: Int, value: String): Boolean {
        if (value.length > 3) return false
        val tail = joined.substring(index + length, minOf(joined.length, index + length + 4))
        val blocked = listOf("kW", "KW", "kw", "公里", "米", "元", "度", "%", "km")
        return blocked.none { tail.startsWith(it) }
    }

    private fun parseTypeCounts(pileText: String, keywords: List<String>): TypeCounts {
        var total = ""
        var idle = ""
        for (keyword in keywords) {
            if (total.isEmpty()) {
                // 高德列表卡片常用 "超充 空2/6" 这种成对写法（空闲/总数）。
                val emptyRatio = Regex(
                    "$keyword[^0-9]{0,8}(?:空闲|空余|剩余|空)\\s*([0-9]{1,3})\\s*/\\s*([0-9]{1,3})"
                ).find(pileText)
                if (emptyRatio != null) {
                    idle = emptyRatio.groupValues[1]
                    total = emptyRatio.groupValues[2]
                }
            }
            if (total.isEmpty()) {
                val ratio = Regex("$keyword[^0-9\\n]{0,4}([0-9]{1,3})\\s*/\\s*([0-9]{1,3})\\s*(?:根|个)")
                    .find(pileText)
                if (ratio != null) {
                    idle = ratio.groupValues[1]
                    total = ratio.groupValues[2]
                }
            }
            if (total.isEmpty()) {
                val withUnit = Regex("$keyword[^0-9\\n]{0,4}([0-9]{1,3})\\s*(?:根|个)")
                    .find(pileText)
                if (withUnit != null) {
                    total = withUnit.groupValues[1]
                }
            }
            if (total.isEmpty()) {
                val bare = Regex("$keyword[^0-9\\n]{0,3}([0-9]{1,3})").findAll(pileText)
                for (match in bare) {
                    val value = match.groupValues[1]
                    if (safeTotal(pileText, match.range.first, match.value.length, value)) {
                        total = value
                        break
                    }
                }
            }
            if (idle.isEmpty()) {
                val match = Regex("$keyword[^\\n]{0,12}?(?:空闲|可充|空余|剩余)\\s*([0-9]{1,3})")
                    .find(pileText)
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
        val price = extractPrice(joined)
        val pileText = stripPriceSpans(joined)

        val fast = parseTypeCounts(pileText, listOf("快充", "快"))
        val superType = parseTypeCounts(pileText, listOf("超充", "超"))
        val slow = parseTypeCounts(pileText, listOf("慢充", "慢"))
        val overallIdle = idleRegex.find(pileText)?.groupValues?.get(1).orEmpty()
        val overallBusy = busyRegex.find(pileText)?.groupValues?.get(1).orEmpty()

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

        var totalSum = totals.values.mapNotNull { it.toIntOrNull() }.sum()
        if (totalSum == 0) {
            // Fall back to an aggregate "空闲 X/Y 根" style line if present.
            val ratio = overallRatioRegex.find(pileText)
            if (ratio != null) {
                val ratioIdle = ratio.groupValues[1]
                val ratioTotal = ratio.groupValues[2]
                val bucket = present.firstOrNull()
                    ?: totals.keys.firstOrNull { listOf("超充", "快充", "慢充").any { it in pileText } }
                if (bucket != null && ratioTotal.toIntOrNull() != null) {
                    totals[bucket] = ratioTotal
                    idles[bucket] = clampIdle(ratioIdle, ratioTotal)
                    totalSum = ratioTotal.toIntOrNull() ?: 0
                }
            }
        }
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
