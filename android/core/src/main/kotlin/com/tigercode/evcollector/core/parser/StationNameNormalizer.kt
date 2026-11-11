package com.tigercode.evcollector.core.parser

object StationNameNormalizer {
    // AMap renders long station names with a trailing ellipsis; older list
    // snapshots persisted that rendered text. Treat ellipsis/full stops as
    // noise so a truncated name still compares as a prefix of the full name.
    private val noise = Regex("""[\s（）()·•\-—_.．。…]+""")

    private val genericNames = setOf(
        "充电站", "汽车充电站", "重卡充电站", "新能源充电站",
        "超级充电站", "重卡快充站", "充电桩", "充电",
    ).map { normalizeText(it) }.toSet()

    fun normalize(value: String?): String =
        value.orEmpty().lineSequence().joinToString("") { normalizeLabel(it) }

    private fun normalizeLabel(value: String): String {
        val normalized = normalizeText(value)
        val opening = value.indexOfFirst { it == '(' || it == '（' }
        if (opening < 0) return normalized

        val outerName = normalizeText(value.substring(0, opening))
        if (!outerName.endsWith("充电站") || outerName in genericNames) return normalized
        val brand = outerName.removeSuffix("充电站")
        if (brand.length < 2) return normalized

        val specificName = normalizeText(value.substring(opening + 1))
        // Some POIs repeat the outer brand immediately inside the brackets.
        // Keep the brand outside and the complete site-specific part inside;
        // different roads/sites and different outer brands remain distinct.
        if (!specificName.startsWith(brand) || specificName.length - brand.length < 2) {
            return normalized
        }
        return outerName + specificName.removePrefix(brand)
    }

    private fun normalizeText(value: String): String {
        var normalized = noise.replace(value, "")
        normalized = normalized.replace("汽车充电站", "充电站")
        normalized = normalized.replace("超级充电站", "充电站")
        return normalized.lowercase()
    }

    /**
     * Persist the fuller of two names. One side can be the rendered,
     * ellipsized card title while the other is the full search query.
     */
    fun preferredDisplayName(first: String?, second: String?): String {
        val a = first.orEmpty().trim()
        val b = second.orEmpty().trim()
        if (a.isEmpty()) return b
        if (b.isEmpty()) return a
        val normalizedA = normalize(a)
        val normalizedB = normalize(b)
        if (normalizedA.length != normalizedB.length) {
            return if (normalizedA.length > normalizedB.length) a else b
        }
        val aTruncated = a.contains("...") || a.contains("…")
        val bTruncated = b.contains("...") || b.contains("…")
        return if (aTruncated && !bTruncated) b else a
    }

    /**
     * Generic names such as "汽车充电站" are not unique stations and will
     * always produce ambiguous list results. They must not be collected as a
     * specific station.
     */
    fun isGenericStationName(value: String?): Boolean {
        val raw = value.orEmpty()
        val normalized = normalize(raw)
        if (normalized.isEmpty()) return true
        if (normalized in genericNames) return true
        val hasSpecificPart = raw.contains("(") || raw.contains("（")
        return !hasSpecificPart && normalized.length <= 7
    }
}
