package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.control.MapProcessPolicy
import org.junit.Assert.*
import org.junit.Test

class MapProcessPolicyTest {
    private val baidu = MapProcessPolicy.targetFor("com.tigercode.bdcollector")

    @Test fun variantsCanOnlyStopTheirPairedMap() {
        assertEquals("com.baidu.BaiduMap", baidu.packageName)
        assertEquals("com.autonavi.minimap", MapProcessPolicy.targetFor("com.tigercode.evcollector").packageName)
    }

    @Test(expected = IllegalArgumentException::class)
    fun unknownApplicationCannotChooseAPackage() {
        MapProcessPolicy.targetFor("com.baidu.BaiduMap; reboot")
    }

    @Test fun packageStateIsScopedToExactAndroidUser() {
        val dump = """
            User 0: installed=true stopped=false notLaunched=false
            User 10: installed=true stopped=true notLaunched=false
        """.trimIndent()
        assertEquals(false, MapProcessPolicy.stoppedForUser(dump, 0))
        assertEquals(true, MapProcessPolicy.stoppedForUser(dump, 10))
        assertNull(MapProcessPolicy.stoppedForUser(dump, 1))
    }

    @Test fun missingOrMalformedStoppedStateIsUnknown() {
        assertNull(MapProcessPolicy.stoppedForUser("Unable to find package", 0))
        assertNull(MapProcessPolicy.stoppedForUser("User 0: notStopped=true", 0))
        assertNull(MapProcessPolicy.stoppedForUser("User 0: stopped=unknown", 0))
        assertNull(MapProcessPolicy.stoppedForUser("User 0: stopped=trueX", 0))
    }

    @Test fun includesMainAndAuxiliaryProcessesButNotOtherPackagesOrUsers() {
        val dump = """
              UID NAME
            10275 com.baidu.BaiduMap
            10275 com.baidu.BaiduMap:MapCoreService
            10276 com.baidu.BaiduMapFake
            10276 com.baidu.BaiduMapFake:worker
            1010275 com.baidu.BaiduMap:otherUser
            2000 shizuku_server
        """.trimIndent()
        assertEquals(listOf("com.baidu.BaiduMap", "com.baidu.BaiduMap:MapCoreService"),
            MapProcessPolicy.runningMapProcesses(dump, baidu, 0))
        assertEquals(listOf("com.baidu.BaiduMap:otherUser"),
            MapProcessPolicy.runningMapProcesses(dump, baidu, 10))
    }

    @Test fun kernelProcessNamesMayContainSpaces() {
        val dump = "UID NAME\n0 [irq/292-q6v5 wd]\n10275 com.baidu.BaiduMap:MapCoreService\n"
        assertEquals(listOf("com.baidu.BaiduMap:MapCoreService"),
            MapProcessPolicy.runningMapProcesses(dump, baidu, 0))
    }

    @Test fun supportsAmapAndEmptyProcessSet() {
        val amap = MapProcessPolicy.targetFor("com.tigercode.evcollector")
        assertEquals(listOf("com.autonavi.minimap:push"),
            MapProcessPolicy.runningMapProcesses("UID NAME\n10300 com.autonavi.minimap:push\n", amap, 0))
        assertTrue(MapProcessPolicy.runningMapProcesses("UID NAME\n2000 shizuku_server\n", baidu, 0).isEmpty())
    }

    @Test(expected = IllegalStateException::class)
    fun permissionDeniedOutputCannotBeMistakenForNoProcesses() {
        MapProcessPolicy.runningMapProcesses("Permission denied", baidu, 0)
    }

    @Test(expected = IllegalStateException::class)
    fun truncatedProcessOutputCannotPassVerification() {
        MapProcessPolicy.runningMapProcesses("UID NAME\n10275", baidu, 0)
    }

    @Test(expected = IllegalStateException::class)
    fun namedUidCannotPassAsNumericUid() {
        MapProcessPolicy.runningMapProcesses("UID NAME\nu0_a275 com.baidu.BaiduMap", baidu, 0)
    }
}
