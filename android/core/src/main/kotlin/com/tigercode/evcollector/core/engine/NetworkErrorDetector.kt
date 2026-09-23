package com.tigercode.evcollector.core.engine

object NetworkErrorDetector {
    private val phrases = listOf(
        "没网了",
        "网络好像开小差了",
        "刷新重试",
    )

    /** Returns the matched phrase, tolerating whitespace inserted by accessibility nodes. */
    fun find(pageText: String): String? {
        val compactText = pageText.replace(Regex("\\s+"), "")
        return phrases.firstOrNull { phrase ->
            phrase in pageText || phrase in compactText
        }
    }
}
