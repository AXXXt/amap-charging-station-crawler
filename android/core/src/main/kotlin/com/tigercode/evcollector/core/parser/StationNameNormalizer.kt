package com.tigercode.evcollector.core.parser

object StationNameNormalizer {
    private val punctuation = Regex("""[\s（）()·•\-—_]+""")

    fun normalize(value: String?): String {
        var normalized = punctuation.replace(value.orEmpty(), "")
        normalized = normalized.replace("汽车充电站", "充电站")
        normalized = normalized.replace("超级充电站", "充电站")
        return normalized.lowercase()
    }
}
