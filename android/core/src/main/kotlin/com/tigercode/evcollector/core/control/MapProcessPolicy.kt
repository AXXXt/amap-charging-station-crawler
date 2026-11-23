package com.tigercode.evcollector.core.control

/** Fixed targets only: the privileged service never accepts an arbitrary package or command. */
object MapProcessPolicy {
    data class Target(val packageName: String, val displayName: String)

    fun targetFor(applicationId: String): Target = when (applicationId) {
        "com.tigercode.bdcollector" -> Target("com.baidu.BaiduMap", "百度地图")
        "com.tigercode.evcollector" -> Target("com.autonavi.minimap", "高德地图")
        else -> throw IllegalArgumentException("不支持的采集应用")
    }

    /** Unknown/missing user state must never be treated as a successful stop. */
    fun stoppedForUser(packageDump: String, userId: Int): Boolean? {
        require(userId >= 0)
        val line = packageDump.lineSequence().firstOrNull {
            Regex("^\\s*User\\s+$userId:").containsMatchIn(it)
        } ?: return null
        return Regex("(?:^|\\s)stopped=(true|false)(?:\\s|$)")
            .find(line)?.groupValues?.get(1)?.toBooleanStrictOrNull()
    }

    /** Input is specifically `ps -A -o UID,NAME`; isolate Android users by numeric UID. */
    fun runningMapProcesses(processDump: String, target: Target, userId: Int): List<String> {
        require(userId >= 0)
        val lines = processDump.lineSequence().filter { it.isNotBlank() }.toList()
        check(lines.firstOrNull()?.trim()?.split(Regex("\\s+")) == listOf("UID", "NAME")) {
            "无法核实进程列表表头"
        }
        return lines.drop(1).mapNotNull { line ->
            val fields = line.trim().split(Regex("\\s+"), limit = 2)
            check(fields.size == 2) { "无法核实进程列表格式" }
            val uid = checkNotNull(fields[0].toIntOrNull()) { "无法核实进程所属用户" }
            val name = fields[1]
            name.takeIf {
                uid / 100_000 == userId &&
                    (it == target.packageName || it.startsWith(target.packageName + ":"))
            }
        }.distinct()
    }
}
