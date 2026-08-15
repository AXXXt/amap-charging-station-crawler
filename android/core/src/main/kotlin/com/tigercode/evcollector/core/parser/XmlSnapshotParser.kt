package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.Rect
import org.w3c.dom.Element
import org.xml.sax.InputSource
import java.io.StringReader
import javax.xml.parsers.DocumentBuilderFactory

object XmlSnapshotParser {
    fun parse(xml: String): NodeSnapshot? {
        return try {
            val factory = DocumentBuilderFactory.newInstance()
            factory.isNamespaceAware = false
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
            val document = factory.newDocumentBuilder().parse(InputSource(StringReader(xml)))
            document.documentElement?.let { parseElement(it) }
        } catch (_: Exception) {
            null
        }
    }

    private fun parseElement(element: Element): NodeSnapshot {
        val children = mutableListOf<NodeSnapshot>()
        val childNodes = element.childNodes
        for (index in 0 until childNodes.length) {
            val child = childNodes.item(index)
            if (child is Element) {
                children.add(parseElement(child))
            }
        }
        return NodeSnapshot(
            text = element.getAttribute("text").trim(),
            contentDescription = element.getAttribute("content-desc").trim(),
            viewId = element.getAttribute("resource-id").trim(),
            className = element.getAttribute("class").trim(),
            bounds = parseBounds(element.getAttribute("bounds")),
            clickable = element.getAttribute("clickable") == "true",
            scrollable = element.getAttribute("scrollable") == "true",
            enabled = element.getAttribute("enabled") != "false",
            selected = element.getAttribute("selected") == "true",
            children = children,
        )
    }

    private fun parseBounds(value: String): Rect {
        val match = Regex("""\[(\d+),(\d+)\]\[(\d+),(\d+)\]""").find(value) ?: return Rect()
        val numbers = match.groupValues.drop(1).mapNotNull { it.toIntOrNull() }
        if (numbers.size != 4) return Rect()
        return Rect(numbers[0], numbers[1], numbers[2], numbers[3])
    }
}
