package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.ChargingPileDetail
import com.tigercode.evcollector.core.model.ChargingPileDialogSnapshot
import com.tigercode.evcollector.core.model.NodeSnapshot

object ChargingPileDialogParser {
    private val totalPattern = Regex("""全部\s*共\s*(\d+)""")
    private val separateTotalPattern = Regex("""^共\s*(\d+)$""")
    private val inlineDevicePattern = Regex("""设备编号\s*[:：]?\s*(\S+)""")
    private val chargingTypes = listOf("超充", "快充", "慢充")
    private val statuses = listOf(
        "空闲", "充电中", "使用中", "占用", "已满", "故障", "离线",
        "维护中", "预约", "不可用",
    )
    private val fieldLabels = setOf("设备编号", "额定功率", "额定电流", "额定电压")

    fun isDialog(root: NodeSnapshot): Boolean =
        collectValues(root).any { it == "电桩详情" }

    fun parse(root: NodeSnapshot): ChargingPileDialogSnapshot {
        val allValues = collectValues(root)
        val expectedTotal = allValues
            .firstNotNullOfOrNull { value ->
                totalPattern.find(value)?.groupValues?.getOrNull(1)?.toIntOrNull()
            }
            ?: allValues
                .firstNotNullOfOrNull { value ->
                    separateTotalPattern.find(value)?.groupValues?.getOrNull(1)?.toIntOrNull()
                }
            ?: 0

        val bestByDevice = linkedMapOf<String, ChargingPileDetail>()

        fun walk(node: NodeSnapshot) {
            val values = collectValues(node)
            val deviceIds = extractDeviceIds(values)
            if (deviceIds.size == 1 && hasPileFields(values)) {
                val pile = parsePile(values, deviceIds.first())
                if (pile.deviceId.isNotBlank()) {
                    val current = bestByDevice[pile.deviceId]
                    if (current == null || pile.filledFieldCount > current.filledFieldCount) {
                        bestByDevice[pile.deviceId] = pile
                    }
                }
            }
            node.children.forEach(::walk)
        }
        walk(root)

        if (bestByDevice.isEmpty()) {
            parseFlatCards(allValues).forEach { pile ->
                bestByDevice[pile.deviceId] = pile
            }
        }

        return ChargingPileDialogSnapshot(
            expectedTotal = expectedTotal,
            piles = bestByDevice.values.toList(),
        )
    }

    fun merge(
        existing: List<ChargingPileDetail>,
        incoming: List<ChargingPileDetail>,
    ): List<ChargingPileDetail> {
        val byDevice = linkedMapOf<String, ChargingPileDetail>()
        existing.filter { it.deviceId.isNotBlank() }.forEach { byDevice[it.deviceId] = it }
        incoming.filter { it.deviceId.isNotBlank() }.forEach { pile ->
            val current = byDevice[pile.deviceId]
            byDevice[pile.deviceId] = if (current == null) pile else mergePile(current, pile)
        }
        return byDevice.values.toList()
    }

    private fun parseFlatCards(values: List<String>): List<ChargingPileDetail> {
        val starts = values.indices.filter { index ->
            values[index] == "设备编号" || inlineDevicePattern.containsMatchIn(values[index])
        }
        return starts.mapNotNull { start ->
            val end = starts.firstOrNull { it > start } ?: values.size
            val segmentStart = (start - 3).coerceAtLeast(0)
            val segment = values.subList(segmentStart, end)
            val deviceId = extractDeviceIds(segment).firstOrNull().orEmpty()
            parsePile(segment, deviceId).takeIf { it.deviceId.isNotBlank() }
        }
    }

    private fun parsePile(values: List<String>, deviceId: String): ChargingPileDetail {
        val chargingType = chargingTypes.firstOrNull { type ->
            values.any { it == type }
        }.orEmpty()
        val status = statuses.firstOrNull { status ->
            values.any { it == status }
        }.orEmpty()
        val equipmentType = values.firstOrNull { value ->
            value != "设备编号" &&
                (value == "直流设备" || value == "交流设备" || value.contains("直流超充"))
        }.orEmpty()
        val standard = values.firstOrNull { it.startsWith("国标") }.orEmpty()

        return ChargingPileDetail(
            deviceId = deviceId,
            chargingType = chargingType.ifBlank {
                when {
                    "超充" in equipmentType -> "超充"
                    else -> ""
                }
            },
            status = status,
            ratedPower = valueAfterLabel(values, "额定功率"),
            ratedCurrent = valueAfterLabel(values, "额定电流"),
            ratedVoltage = valueAfterLabel(values, "额定电压"),
            equipmentType = equipmentType,
            standard = standard,
        )
    }

    private fun valueAfterLabel(values: List<String>, label: String): String {
        for ((index, value) in values.withIndex()) {
            if (value == label) {
                return values
                    .drop(index + 1)
                    .firstOrNull { it.isNotBlank() && it !in fieldLabels }
                    .orEmpty()
            }
            if (value.startsWith(label)) {
                return value.removePrefix(label).trimStart(' ', ':', '：')
            }
        }
        return ""
    }

    private fun extractDeviceIds(values: List<String>): List<String> {
        val result = linkedSetOf<String>()
        for ((index, value) in values.withIndex()) {
            val inline = inlineDevicePattern.find(value)?.groupValues?.getOrNull(1).orEmpty()
            if (inline.isNotBlank()) result.add(inline)
            if (value == "设备编号") {
                val candidate = values.getOrNull(index + 1).orEmpty()
                if (candidate.isNotBlank() && candidate !in fieldLabels) result.add(candidate)
            }
        }
        return result.toList()
    }

    private fun hasPileFields(values: List<String>): Boolean =
        values.any { it == "额定功率" || it.startsWith("额定功率") } &&
            values.any { it == "额定电流" || it.startsWith("额定电流") }

    private fun collectValues(root: NodeSnapshot): List<String> {
        val values = mutableListOf<String>()
        fun walk(node: NodeSnapshot) {
            sequenceOf(node.text, node.contentDescription)
                .map { it.replace(Regex("""\s+"""), " ").trim() }
                .filter { it.isNotBlank() }
                .forEach(values::add)
            node.children.forEach(::walk)
        }
        walk(root)
        return values
    }

    private fun mergePile(
        first: ChargingPileDetail,
        second: ChargingPileDetail,
    ): ChargingPileDetail = ChargingPileDetail(
        deviceId = first.deviceId.ifBlank { second.deviceId },
        chargingType = first.chargingType.ifBlank { second.chargingType },
        status = first.status.ifBlank { second.status },
        ratedPower = first.ratedPower.ifBlank { second.ratedPower },
        ratedCurrent = first.ratedCurrent.ifBlank { second.ratedCurrent },
        ratedVoltage = first.ratedVoltage.ifBlank { second.ratedVoltage },
        equipmentType = first.equipmentType.ifBlank { second.equipmentType },
        standard = first.standard.ifBlank { second.standard },
    )
}
