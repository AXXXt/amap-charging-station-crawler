package com.tigercode.evcollector.network

import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class DeviceCommandProcessorTest {
    private class Device(val mode: String = "DEVICE_DISPATCH") {
        val command = DeviceCommandDto(11, "START", mode, "QUEUED", 2, true)
        var control = FleetControlDto(supported = true, stopGeneration = 2, releaseComplete = true, deviceCommand = command)
        var receipt: DeviceCommandReceipt? = null
        var starts = 0
        var stops = 0
        val receipts = mutableListOf<String>()
        var onStop: () -> Unit = {}
        var onStart: () -> Unit = {}
        fun processor() = DeviceCommandProcessor(mode, { "server|device" }, { receipt }, { receipt = it },
            { control }, { stops++; onStop() }, { starts++; onStart() }, { c, status, _ -> receipts.add("${c.commandId}:$status") })
    }

    @Test fun bothPlatformModesStartAndAcknowledgeExactlyOnce() = runBlocking {
        for (mode in listOf("DEVICE_DISPATCH", "PRIORITY_DETAIL")) {
            val device = Device(mode)
            val processor = device.processor()
            processor.apply(device.control)
            processor.apply(device.control) // Lost acknowledgement: resend receipt, never restart service.
            assertEquals(1, device.starts)
            assertEquals(listOf("11:APPLIED", "11:APPLIED"), device.receipts)
        }
    }

    @Test fun globalStopOrReleaseBarrierPreventsStart() = runBlocking {
        for (control in listOf(
            Device().control.copy(stopRequired = true),
            Device().control.copy(releaseComplete = false),
            Device().control.copy(stopGeneration = 3),
        )) {
            val device = Device()
            device.processor().apply(control)
            assertEquals(0, device.starts)
            assertTrue(device.receipts.isEmpty())
        }
    }

    @Test fun newStopDuringEngineShutdownCancelsOldStart() = runBlocking {
        val device = Device()
        device.onStop = { device.control = device.control.copy(stopRequired = true,
            deviceCommand = device.command.copy(commandId = 12, action = "STOP")) }
        device.processor().apply(device.control)
        assertEquals(0, device.starts)
        assertTrue(device.receipts.isEmpty())
    }

    @Test fun globalStopAfterStartupStopsAgainAndRejectsStaleReceipt() = runBlocking {
        val device = Device()
        device.onStart = { device.control = device.control.copy(stopGeneration = 3, stopRequired = true, deviceCommand = null) }
        device.processor().apply(device.control)
        assertEquals(1, device.starts)
        assertEquals(2, device.stops)
        assertTrue(device.receipts.isEmpty())
    }

    @Test fun interruptedStartIsFailedWithoutReplayingMapLaunch() = runBlocking {
        val device = Device()
        device.receipt = DeviceCommandReceipt("server|device|2|11", "EXECUTING")
        device.processor().apply(device.control)
        assertEquals(0, device.starts)
        assertEquals(1, device.stops)
        assertEquals(listOf("11:FAILED"), device.receipts)
    }

    @Test fun wrongPlatformModeFailsInsteadOfStartingAnotherCollectionMode() = runBlocking {
        val device = Device()
        device.control = device.control.copy(deviceCommand = device.command.copy(mode = "PRIORITY_DETAIL"))
        device.processor().apply(device.control)
        assertEquals(0, device.starts)
        assertEquals(listOf("11:FAILED"), device.receipts)
    }

    @Test fun stopTakesEffectBeforeReleaseCompletesAndAcknowledgesAfterward() = runBlocking {
        val device = Device()
        device.control = device.control.copy(stopRequired = true, releaseComplete = false,
            deviceCommand = device.command.copy(action = "STOP", releaseComplete = false))
        val processor = device.processor()
        processor.apply(device.control)
        assertEquals(1, device.stops)
        assertTrue(device.receipts.isEmpty())
        device.control = device.control.copy(releaseComplete = true,
            deviceCommand = device.control.deviceCommand!!.copy(releaseComplete = true))
        processor.apply(device.control)
        assertEquals(listOf("11:APPLIED"), device.receipts)
    }

    @Test fun failedStartupStopsAndRetriesOnlyTheFailureReceipt() = runBlocking {
        val device = Device()
        device.onStart = { throw IllegalStateException("无障碍不可用") }
        val processor = device.processor()
        processor.apply(device.control)
        processor.apply(device.control)
        assertEquals(1, device.starts)
        assertEquals(2, device.stops)
        assertEquals(listOf("11:FAILED", "11:FAILED"), device.receipts)
    }

    @Test fun alreadyAcknowledgedCommandDoesNotRestartAfterManualStop() = runBlocking {
        val device = Device()
        device.processor().apply(device.control.copy(deviceCommand = device.command.copy(status = "APPLIED")))
        assertEquals(0, device.starts)
        assertEquals(0, device.stops)
    }
}
