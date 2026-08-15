package com.tigercode.evcollector.core.parser

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PricePeriod

object PriceDetailParser {
    private val periodRegex = Regex("""^\d{2}:\d{2}[-~]\d{2}:\d{2}$""")
    private val decimalPriceRegex = Regex("""^\.\d+$""")
    private val fullPriceRegex = Regex("""^\d+\.\d+$""")

    fun parse(root: NodeSnapshot): List<PricePeriod> {
        val texts = mutableListOf<String>()
        fun walk(node: NodeSnapshot) {
            val text = node.text.trim()
            if (text.isNotEmpty()) texts.add(text)
            node.children.forEach { walk(it) }
        }
        walk(root)

        val periods = mutableListOf<PricePeriod>()
        var current: PricePeriod? = null
        var state = State.IDLE
        var intBuffer = ""

        for (text in texts) {
            if (periodRegex.matches(text)) {
                current?.let { periods.add(it) }
                current = PricePeriod(time = text)
                state = State.IDLE
                intBuffer = ""
                continue
            }

            if (current == null) continue

            if (text == "最低" || text == "当前计费时段") {
                current = current?.copy(tag = text)
                continue
            }

            if (text.length <= 2 && text.all { it.isDigit() }) {
                intBuffer = text
                continue
            }

            if ("参考价" in text) {
                state = State.REFERENCE
                intBuffer = ""
                continue
            }
            if ("电费:" in text || "电费" in text) {
                state = State.ELECTRICITY
                intBuffer = ""
                continue
            }
            if ("服务费:" in text || "服务费" in text) {
                state = State.SERVICE
                intBuffer = ""
                continue
            }

            if (decimalPriceRegex.matches(text)) {
                val price = (if (intBuffer.isNotEmpty()) intBuffer else "0") + text
                current = current?.copy(
                    totalPrice = if (state == State.REFERENCE) price else current!!.totalPrice,
                    elecFee = if (state == State.ELECTRICITY) price else current!!.elecFee,
                    serviceFee = if (state == State.SERVICE) price else current!!.serviceFee,
                )
                state = State.IDLE
                intBuffer = ""
                continue
            }

            if (fullPriceRegex.matches(text)) {
                current = current?.copy(
                    totalPrice = if (state == State.REFERENCE) text else current!!.totalPrice,
                    elecFee = if (state == State.ELECTRICITY) text else current!!.elecFee,
                    serviceFee = if (state == State.SERVICE) text else current!!.serviceFee,
                )
                state = State.IDLE
                continue
            }
        }

        current?.let { periods.add(it) }
        return periods
    }

    private enum class State {
        IDLE,
        REFERENCE,
        ELECTRICITY,
        SERVICE,
    }
}
