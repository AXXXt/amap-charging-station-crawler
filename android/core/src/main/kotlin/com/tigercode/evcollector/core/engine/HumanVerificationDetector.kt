package com.tigercode.evcollector.core.engine

object HumanVerificationDetector {
    private val phrases = listOf(
        "请按照说明进行验证",
        "请选择符合描述",
        "没有新图片可以点后",
        "拖动滑块出现完整的",
        "请按照说明拖动滑块",
        "向右拖动滑块填充拼图",
        "安全验证",
        "滑块验证",
        "点击验证",
        "完成拼图",
        "账号异常",
        "账户异常",
        "访问异常",
        "环境异常",
        "已被封禁",
        "账号封禁",
        "账户封禁",
        "封控",
    )

    /** Returns the matched phrase, tolerating whitespace inserted by accessibility nodes. */
    fun find(pageText: String): String? {
        val compactText = pageText.replace(Regex("\\s+"), "")
        return phrases.firstOrNull { phrase ->
            phrase in pageText || phrase in compactText
        }
    }
}
