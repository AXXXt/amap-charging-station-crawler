package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.Rect

object SliderVerificationPolicy {
    const val MAX_ATTEMPTS = 3

    data class Drag(val startX: Int, val startY: Int, val endX: Int, val endY: Int)

    private val simplePrompts = listOf(
        "滑动下方滑块", "向右滑动验证", "向右拖动验证", "请点我重试", "点击框体重试",
    )
    private val excludedPrompts = listOf(
        "图片", "拼图", "请选择符合描述", "拖动滑块出现完整的", "请按照说明拖动滑块",
        "账号异常", "账户异常", "访问异常", "环境异常", "封禁", "封控",
    )
    private val whitespace = Regex("\\s+")

    fun retryTarget(root: NodeSnapshot): Rect? {
        if (verificationText(root) == null) return null
        return nodes(root).firstOrNull { node ->
            node.enabled && node.bounds.isValid && "重试" in node.text &&
                "反馈" !in node.text && "反馈" !in node.contentDescription
        }?.bounds
    }

    fun drag(root: NodeSnapshot): Drag? {
        val text = verificationText(root) ?: return null
        if ("加载" in text || "loading" in text.lowercase()) return null
        return findDrag(root, null)
    }

    private fun verificationText(root: NodeSnapshot): String? {
        val text = nodes(root).flatMap { sequenceOf(it.text, it.contentDescription) }
            .joinToString("\n").replace(whitespace, "")
        if (HumanVerificationDetector.find(text) == null) return null
        if (simplePrompts.none { it in text } || excludedPrompts.any { it in text }) return null
        return text
    }

    private fun nodes(root: NodeSnapshot): Sequence<NodeSnapshot> = sequence {
        yield(root)
        for (child in root.children) yieldAll(nodes(child))
    }

    private fun findDrag(node: NodeSnapshot, track: Rect?): Drag? {
        if (!node.enabled) return null
        if (track != null && ("滑块" in node.text || node.viewId.endsWith("_n1z"))) {
            coordinates(node.bounds, track)?.let { return it }
        }
        val childTrack = if (node.viewId.endsWith("_wrapper")) node.bounds else track
        for (child in node.children) {
            findDrag(child, childTrack)?.let { return it }
        }
        return null
    }

    private fun coordinates(slider: Rect, track: Rect): Drag? {
        if (!slider.isValid || !track.isValid) return null
        val top = maxOf(slider.top, track.top)
        val bottom = minOf(slider.bottom, track.bottom)
        if (bottom <= top) return null
        val startX = slider.centerX
        if (startX < track.left || startX >= track.right) return null
        val endX = (track.right - 8).coerceIn(track.left, track.right - 1)
        if (endX <= startX) return null
        val y = top + (bottom - top) / 2
        return Drag(startX, y, endX, y)
    }
}
