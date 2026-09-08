package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.model.ChargingPileDetail
import com.tigercode.evcollector.core.parser.ChargingPileDialogParser
import com.tigercode.evcollector.core.parser.XmlSnapshotParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ChargingPileDialogParserTest {
    @Test
    fun parsesVisiblePileCardsAndExpectedTotal() {
        val root = checkNotNull(
            XmlSnapshotParser.parse(
                """
                <hierarchy>
                    <node text="电桩详情" />
                    <node text="全部 共20" />
                    <node>
                        <node text="快充" />
                        <node text="空闲" />
                        <node text="设备编号" />
                        <node text="MAK001_1" />
                        <node text="额定功率" />
                        <node text="120kW" />
                        <node text="额定电流" />
                        <node text="250A" />
                        <node text="额定电压" />
                        <node text="1000V-1000V" />
                        <node text="直流设备" />
                        <node text="国标2015" />
                    </node>
                    <node>
                        <node text="超充" />
                        <node text="设备编号 MAK002_1" />
                        <node text="额定功率 320kW" />
                        <node text="额定电流 250A" />
                        <node text="额定电压 1000V-1000V" />
                        <node text="直流超充" />
                        <node text="国标2015" />
                    </node>
                </hierarchy>
                """.trimIndent()
            )
        )

        val result = ChargingPileDialogParser.parse(root)

        assertTrue(ChargingPileDialogParser.isDialog(root))
        assertEquals(20, result.expectedTotal)
        assertEquals(2, result.piles.size)
        assertEquals(
            ChargingPileDetail(
                deviceId = "MAK001_1",
                chargingType = "快充",
                status = "空闲",
                ratedPower = "120kW",
                ratedCurrent = "250A",
                ratedVoltage = "1000V-1000V",
                equipmentType = "直流设备",
                standard = "国标2015",
            ),
            result.piles.first(),
        )
        assertEquals("超充", result.piles.last().chargingType)
        assertEquals("320kW", result.piles.last().ratedPower)
    }

    @Test
    fun parsesExpectedTotalWhenAllAndCountAreSeparateNodes() {
        val root = checkNotNull(
            XmlSnapshotParser.parse(
                """
                <hierarchy>
                    <node text="电桩详情" />
                    <node text="全部" />
                    <node text="共26" />
                    <node>
                        <node text="快充" />
                        <node text="设备编号 MAK001_1" />
                        <node text="额定功率 60kW" />
                        <node text="额定电流 253A" />
                        <node text="额定电压 200V-1000V" />
                    </node>
                </hierarchy>
                """.trimIndent()
            )
        )

        val result = ChargingPileDialogParser.parse(root)

        assertEquals(26, result.expectedTotal)
        assertEquals(1, result.piles.size)
    }

    @Test
    fun mergesOverlappingScrollResultsByDeviceId() {
        val merged = ChargingPileDialogParser.merge(
            listOf(ChargingPileDetail(deviceId = "A", chargingType = "快充")),
            listOf(
                ChargingPileDetail(deviceId = "A", ratedPower = "120kW"),
                ChargingPileDetail(deviceId = "B", chargingType = "超充"),
            ),
        )

        assertEquals(2, merged.size)
        assertEquals("快充", merged.first().chargingType)
        assertEquals("120kW", merged.first().ratedPower)
    }
}
