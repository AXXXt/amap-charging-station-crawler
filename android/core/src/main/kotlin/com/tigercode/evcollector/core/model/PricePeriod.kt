package com.tigercode.evcollector.core.model

data class PricePeriod(
    val time: String = "",
    val totalPrice: String = "",
    val elecFee: String = "",
    val serviceFee: String = "",
    val tag: String = "",
)
