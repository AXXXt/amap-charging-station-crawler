package com.tigercode.evcollector.core.engine

object HumanVerificationDetector {
    private val phrases = listOf(
        "请按照说明进行验证",
        "拖动滑块出现完整的",
        "请按照说明拖动滑块",
        "向右拖动滑块填充拼图",
    )

    /** Returns the matched phrase, tolerating whitespace inserted by accessibility nodes. */
    fun find(pageText: String): String? {
        val compactText = pageText.replace(Regex("\\s+"), "")
        return phrases.firstOrNull { phrase ->
            phrase in pageText || phrase in compactText
        }
    }
}
