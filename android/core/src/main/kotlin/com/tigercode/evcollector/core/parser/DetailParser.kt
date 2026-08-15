package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationDetail

object DetailParser {
    private val stationNameMarkers = listOf(
        "充电站", "充换电站", "超充站", "快充站", "重卡站", "充电场", "充电中心",
    )

    private val tagValues = listOf("优选电站", "刚刚浏览", "新站", "热门")

    private val facilitySet = listOf(
        "地上", "地面", "地下", "卫生间", "休息室", "便利店",
        "重卡车位", "桩多", "免费停车", "WIFI", "无障碍",
    )

    private val knownOperators = listOf(
        "特来电", "新电途", "星星充电", "国家电网", "南方电网",
        "依威能源", "云快充", "万马爱充", "小桔充电", "快电", "蔚来",
    )

    private data class NodeValue(val text: String, val description: String)

    fun parse(root: NodeSnapshot): StationDetail {
        val nodes = mutableListOf<NodeValue>()
        fun walk(node: NodeSnapshot) {
            nodes.add(NodeValue(node.text.trim(), node.contentDescription.trim()))
            node.children.forEach { walk(it) }
        }
        walk(root)

        val texts = nodes.map { it.text }.filter { it.isNotEmpty() }

        var stationName = ""
        for (node in nodes) {
            if (node.description.isNotEmpty() && stationNameMarkers.any { it in node.description }) {
                stationName = node.description
                break
            }
        }

        val tags = linkedSetOf<String>()
        texts.filterTo(tags) { it in tagValues }

        var category = ""
        if ("充电站" in texts) category = "充电站"

        val facilities = linkedSetOf<String>()
        for (text in texts) {
            for (facility in facilitySet) {
                if (facility in text) facilities.add(facility)
            }
        }

        var businessHours = ""
        for ((index, text) in texts.withIndex()) {
            if (text == "营业时间" && index + 1 < texts.size) {
                businessHours = texts[index + 1]
                break
            }
            if ("暂无营业时间" in text) {
                businessHours = "暂无营业时间"
                break
            }
            if ("24小时营业" in text) {
                businessHours = text
                break
            }
            if ("营业中" in text) {
                businessHours = if (index + 1 < texts.size && "小时营业" in texts[index + 1]) {
                    texts[index + 1]
                } else {
                    text
                }
                break
            }
        }

        var distance = ""
        var duration = ""
        for (text in texts) {
            if ("驾车" in text && "公里" in text) distance = text
            if ("分钟" in text && text.length <= 5) {
                val minutes = text.removeSuffix("分钟").trim().toIntOrNull()
                if (minutes != null) duration = text
            }
        }

        var address = ""
        val addressMarkers = listOf("省", "市", "县", "区", "镇", "乡", "村", "路", "街", "道", "号", "高速")
        for (text in texts) {
            if (text.length < 6 || !addressMarkers.any { it in text }) continue
            if (text.startsWith("停车费") || text.startsWith("占位费")) continue
            if (listOf("通知", "K/s", "正在充电", "WLAN", "已选中", "未选中", "信号").any { it in text }) continue
            val parts = text.split(Regex("[|｜]")).map { it.trim() }.filter { it.isNotEmpty() }.toMutableList()
            while (parts.isNotEmpty() && parts.first() in facilitySet) {
                parts.removeAt(0)
            }
            address = if (parts.isNotEmpty()) parts.joinToString(" | ") else text
            break
        }

        var operator = ""
        for (text in texts) {
            if (text in knownOperators) {
                operator = text
                break
            }
        }
        if (operator.isEmpty() && stationName.isNotEmpty()) {
            for (candidate in knownOperators) {
                if (candidate in stationName) {
                    operator = candidate
                    break
                }
            }
        }

        var currentPrice = ""
        for ((index, text) in texts.withIndex()) {
            if (text == "/度" && index > 0) {
                val price = texts[index - 1]
                if (price.replace(".", "").replace("￥", "").all { it.isDigit() }) {
                    currentPrice = price
                    break
                }
            }
        }

        var parkingFee = ""
        for (text in texts) {
            if (text.startsWith("停车费 ")) {
                parkingFee = text.removePrefix("停车费 ").trimStart('：', ':')
                break
            }
            if (text.startsWith("停车费")) {
                parkingFee = text.removePrefix("停车费").trimStart('：', ':')
                break
            }
            if ("停车免费" in text) {
                parkingFee = "免费"
                break
            }
            if ("停车费" in text) {
                val index = text.indexOf("停车费")
                parkingFee = text.substring(index + 3).trimStart('：', ':').trim()
                break
            }
        }

        var occupancyFee = ""
        for (text in texts) {
            if ("占位费" in text) {
                occupancyFee = text
                break
            }
        }

        var favoriteCount = ""
        for ((index, text) in texts.withIndex()) {
            if (text == "分享" && index > 0 && texts[index - 1].all { it.isDigit() }) {
                favoriteCount = texts[index - 1]
                break
            }
        }

        var viewCount = ""
        for ((index, text) in texts.withIndex()) {
            if (text == "浏览" && index > 0 && texts[index - 1].all { it.isDigit() }) {
                viewCount = texts[index - 1]
                break
            }
        }

        var fastAvailable = ""
        var fastTotal = ""
        var fastPower = ""
        var superAvailable = ""
        var superTotal = ""
        var superPower = ""
        var slowAvailable = ""
        var slowTotal = ""
        var slowPower = ""

        for ((index, text) in texts.withIndex()) {
            if (text != "空闲" && text != "空") continue
            if (index <= 0) continue
            val type = texts[index - 1]
            val avail = texts.getOrNull(index + 1) ?: continue
            val total = texts.getOrNull(index + 2)?.replace("/", "") ?: continue
            var power = ""
            val end = minOf(index + 6, texts.size)
            for (j in index + 3 until end) {
                if ("kW" in texts[j]) {
                    power = texts[j]
                    break
                }
            }
            when {
                "超充" in type -> {
                    superAvailable = avail
                    superTotal = total
                    superPower = power
                }
                "快充" in type -> {
                    fastAvailable = avail
                    fastTotal = total
                    fastPower = power
                }
                "慢充" in type -> {
                    slowAvailable = avail
                    slowTotal = total
                    slowPower = power
                }
            }
        }

        var priceTrendTitle = ""
        var fastPrices = emptyList<PricePeriod>()
        var slowPrices = emptyList<PricePeriod>()
        if ("24小时价格趋势图" in texts) {
            priceTrendTitle = "24小时价格趋势图"
            val prices = mutableListOf<PricePeriod>()
            for ((index, text) in texts.withIndex()) {
                if (!text.endsWith("/度") || !text.startsWith("￥")) continue
                val priceValue = text.removePrefix("￥").removeSuffix("/度")
                var time = ""
                val end = minOf(index + 4, texts.size)
                for (j in index until end) {
                    val candidate = texts[j]
                    if (("-" in candidate && ":" in candidate) || candidate == "当前时段") {
                        time = candidate
                        break
                    }
                }
                prices.add(PricePeriod(time = time, totalPrice = priceValue))
            }
            if ("快充价格" in texts && "慢充价格" in texts) {
                val mid = prices.size / 2
                fastPrices = prices.take(mid)
                slowPrices = prices.drop(mid)
            } else {
                fastPrices = prices
            }
        }

        return StationDetail(
            stationName = stationName,
            tags = tags.toList(),
            category = category,
            facilities = facilities.toList(),
            businessHours = businessHours,
            distance = distance,
            duration = duration,
            address = address,
            operator = operator,
            currentPrice = currentPrice,
            parkingFee = parkingFee,
            occupancyFee = occupancyFee,
            favoriteCount = favoriteCount,
            viewCount = viewCount,
            fastAvailable = fastAvailable,
            fastTotal = fastTotal,
            fastPower = fastPower,
            superAvailable = superAvailable,
            superTotal = superTotal,
            superPower = superPower,
            slowAvailable = slowAvailable,
            slowTotal = slowTotal,
            slowPower = slowPower,
            priceTrendTitle = priceTrendTitle,
            fastPrices = fastPrices,
            slowPrices = slowPrices,
        )
    }
}
