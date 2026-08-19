package com.tigercode.evcollector.engine

import android.content.Context
import com.google.gson.Gson
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.engine.StationMatcher
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationCandidate
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.core.parser.DetailPageClassifier
import com.tigercode.evcollector.core.parser.DetailPageType
import com.tigercode.evcollector.core.parser.DetailParser
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.PriceDetailParser
import com.tigercode.evcollector.core.parser.ResultMerger
import com.tigercode.evcollector.core.parser.StationNameNormalizer
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.data.ScanTaskEntity
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

enum class CollectionState(val label: String) {
    IDLE("待机"),
    WAITING_SERVICE("等待无障碍服务"),
    OPENING_AMAP("打开高德地图"),
    SCANNING_RESULTS("扫描搜索结果"),
    OPENING_DETAIL("进入站点详情"),
    READING_DETAIL("读取站点详情"),
    OPENING_PRICE("打开分时电价"),
    READING_PRICE("读取分时电价"),
    RETURNING("返回列表"),
    SAVING("保存站点"),
    DONE("完成"),
    STOPPED("已停止"),
    ERROR("异常"),
}

fun interface CollectionListener {
    fun onEvent(
        state: CollectionState,
        message: String,
        stationCount: Int,
        currentStation: String,
    )
}

private sealed interface PriceEntryResult {
    data class Opened(val snapshot: NodeSnapshot) : PriceEntryResult
    object UniformPrice : PriceEntryResult
    object NotFound : PriceEntryResult
}

class CollectionEngine(
    private val context: Context,
    private val repository: CollectorRepository,
) {
    private val gson = Gson()
    private val listeners = CopyOnWriteArrayList<CollectionListener>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val runMutex = Mutex()
    private var activeJob: Job? = null

    @Volatile
    private var stopRequested = false

    @Volatile
    private var collectedCount = 0

    private val listLoadingKeywords = listOf("正在加载", "加载中", "加载更多")
    private val listEndKeywords = listOf("暂无更多内容", "没有更多了", "没有更多结果", "已经到底了")

    fun addListener(listener: CollectionListener) {
        listeners.addIfAbsent(listener)
    }

    fun removeListener(listener: CollectionListener) {
        listeners.remove(listener)
    }

    fun isRunning(): Boolean = activeJob?.isActive == true

    fun startLocal(
        city: String,
        district: String,
        keyword: String,
        listener: CollectionListener? = null,
    ) {
        listener?.let { listeners.addIfAbsent(it) }
        if (activeJob?.isActive == true) {
            emit(CollectionState.SCANNING_RESULTS, "采集任务已在运行", collectedCount, "")
            return
        }
        stopRequested = false
        collectedCount = 0
        activeJob = scope.launch {
            try {
                runRegion(city, district, keyword)
            } catch (error: Exception) {
                stopRequested = false
                emit(
                    CollectionState.ERROR,
                    error.message ?: error.javaClass.simpleName,
                    collectedCount,
                    "",
                )
                AppPreferences.appendLog(
                    context,
                    "采集异常: ${error.message ?: error.javaClass.simpleName}",
                )
            }
        }
    }

    fun stop() {
        stopRequested = true
        emit(CollectionState.STOPPED, "停止请求已发送", collectedCount, "")
    }

    suspend fun runRegion(city: String, district: String, keyword: String): List<StationDetail> =
        runMutex.withLock {
            withContext(Dispatchers.Main.immediate) {
                stopRequested = false
                collectedCount = 0
                val service = waitForService() ?: throw IllegalStateException(
                    "无障碍服务未连接，请先到系统设置开启采集助手"
                )
                emit(CollectionState.OPENING_AMAP, "打开高德地图", 0, "")

                val query = buildQuery(city, district, keyword)
                emit(CollectionState.SCANNING_RESULTS, "搜索 $query", 0, "")
                service.openAmapSearch(query)
                var snapshot = waitForSearchResults(service, query)
                if (snapshot == null) {
                    throw IllegalStateException("高德地图搜索页打开失败: $query")
                }

                val collected = mutableListOf<StationDetail>()
                val seenCards = mutableSetOf<String>()
                var unchangedScrolls = 0
                var lastPageSignature: String? = null
                var loadingWaits = 0

                while (!stopRequested) {
                    snapshot = waitForPage(6000, 550) { root ->
                        val kind = PageAssessor.assess(root).kind
                        kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
                    }
                    if (snapshot == null) {
                        emit(
                            CollectionState.RETURNING,
                            "搜索结果页不可用，尝试重新搜索",
                            collected.size,
                            "",
                        )
                        when (service.freshPageKind()) {
                            PageKind.HOME -> openSearchFromHome(service, query)
                            PageKind.POPUP -> dismissPopup(service)
                            PageKind.SEARCH_RESULTS -> Unit
                            else -> clickBackToSearch(service)
                        }
                        snapshot = waitForPage(4000, 550) { root ->
                            val kind = PageAssessor.assess(root).kind
                            kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
                        }
                        if (snapshot != null) continue
                        service.openAmapSearch(query)
                        snapshot = waitForPage(10000, 650) { root ->
                            val kind = PageAssessor.assess(root).kind
                            kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
                        }
                        if (snapshot == null) break
                        unchangedScrolls = 0
                        lastPageSignature = null
                        loadingWaits = 0
                        if (PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                            dismissPopup(service)
                            continue
                        }
                        ensureListTop(service, snapshot)
                        emit(
                            CollectionState.SCANNING_RESULTS,
                            "重新搜索完成，继续扫描新结果",
                            collected.size,
                            "",
                        )
                        continue
                    }

                    if (PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                        dismissPopup(service)
                        continue
                    }

                    val cards = StationMatcher.visibleStationCards(snapshot)
                    val nextCard = cards.firstOrNull { card ->
                        card.clickVisible &&
                        cardFingerprint(card) !in seenCards
                    }
                    if (nextCard == null) {
                        if (isListLoading(snapshot)) {
                            loadingWaits += 1
                            if (loadingWaits < 4) {
                                emit(
                                    CollectionState.SCANNING_RESULTS,
                                    "列表加载中，等待更多站点",
                                    collected.size,
                                    "",
                                )
                                delay(1200)
                                continue
                            }
                            loadingWaits = 0
                        }
                        if (isListEnd(snapshot)) {
                            emit(
                                CollectionState.DONE,
                                "列表已加载到末尾，结束扫描",
                                collected.size,
                                "",
                            )
                            break
                        }
                        val currentSignature = cards.joinToString("|") { cardFingerprint(it) }
                        if (currentSignature != lastPageSignature) {
                            lastPageSignature = currentSignature
                            unchangedScrolls = 0
                            emit(
                                CollectionState.SCANNING_RESULTS,
                                "当前页均为已采集站点，继续向下滚动",
                                collected.size,
                                "",
                            )
                            scrollResultsDown(service, snapshot)
                            continue
                        }
                        unchangedScrolls += 1
                        if (unchangedScrolls >= 2) {
                            emit(
                                CollectionState.DONE,
                                "连续滚动页面无变化，结束扫描",
                                collected.size,
                                "",
                            )
                            break
                        }
                        emit(
                            CollectionState.SCANNING_RESULTS,
                            "向下滚动查找更多站点",
                            collected.size,
                            "",
                        )
                        scrollResultsDown(service, snapshot)
                        continue
                    }
                    unchangedScrolls = 0
                    loadingWaits = 0
                    lastPageSignature = null

                    seenCards.add(cardFingerprint(nextCard))
                    val detail = collectStation(service, nextCard, city, district, query)
                    if (detail != null) {
                        collected.add(detail)
                        collectedCount = collected.size
                    }
                }

                emit(
                    if (stopRequested) CollectionState.STOPPED else CollectionState.DONE,
                    "采集结束，共 ${collected.size} 个站点",
                    collected.size,
                    "",
                )
                stopRequested = false
                collected
            }
        }

    private suspend fun collectStation(
        service: ChargingAccessibilityService,
        card: StationCandidate,
        city: String,
        district: String,
        query: String,
    ): StationDetail? {
        val centerX = card.centerX ?: return null
        val centerY = card.centerY ?: return null
        emit(
            CollectionState.OPENING_DETAIL,
            "进入详情: ${card.name}",
            collectedCount,
            card.name,
        )
        val clicked = service.clickStationCard(card.name) ||
            service.clickAt(centerX, centerY) ||
            service.clickNodeByText(card.name)
        var detailSnapshot = waitForPage(14000, 700) { root ->
            val assessment = PageAssessor.assess(root, card.name)
            assessment.kind == PageKind.DETAIL ||
                (assessment.expectedStationVisible &&
                    assessment.kind != PageKind.SEARCH_RESULTS &&
                    assessment.kind != PageKind.POPUP)
        }
        if (detailSnapshot == null && clicked) {
            emit(
                CollectionState.OPENING_DETAIL,
                "详情打开超时，重试点击",
                collectedCount,
                card.name,
            )
            service.clickStationCard(card.name)
            detailSnapshot = waitForPage(14000, 700) { root ->
                val assessment = PageAssessor.assess(root, card.name)
                assessment.kind == PageKind.DETAIL ||
                    (assessment.expectedStationVisible &&
                        assessment.kind != PageKind.SEARCH_RESULTS &&
                        assessment.kind != PageKind.POPUP)
            }
        }
        if (detailSnapshot == null) {
            emit(
                CollectionState.ERROR,
                "未能打开站点详情: ${card.name}",
                collectedCount,
                card.name,
            )
            service.globalBack()
            delay(1200)
            return null
        }

        val rendered = waitForPage(2500, 350) { root ->
            DetailPageClassifier.classify(root).scrollsNeeded > 0
        }
        if (rendered != null) detailSnapshot = rendered

        var merged = DetailParser.parse(detailSnapshot)
        if (merged.stationName.isBlank()) {
            merged = merged.copy(stationName = card.name)
        }
        val pageInfo = DetailPageClassifier.classify(detailSnapshot)
        val scrolls = when (pageInfo.type) {
            DetailPageType.BASIC -> 3
            else -> minOf(maxOf(pageInfo.scrollsNeeded, 1), 3)
        }

        var priceEntry = openPriceDetail(service, card.name)
        if (priceEntry !is PriceEntryResult.Opened) {
            for (scrollIndex in 1..scrolls) {
                if (stopRequested) break
                emit(
                    CollectionState.READING_DETAIL,
                    "详情滚动 $scrollIndex/$scrolls",
                    collectedCount,
                    card.name,
                )
                if (!service.scrollSmallDown()) {
                    delay(200)
                    service.scrollSmallDown()
                }
                delay(900)
                val scrolledSnapshot = service.snapshot() ?: continue
                val parsed = DetailParser.parse(scrolledSnapshot)
                if (parsed.filledFieldCount > 0) {
                    merged = ResultMerger.merge(merged, parsed)
                }
                val assessment = PageAssessor.assess(scrolledSnapshot, card.name)
                if (assessment.kind == PageKind.PRICE_DETAIL) {
                    service.globalBack()
                    delay(800)
                    continue
                } else if (assessment.kind == PageKind.SEARCH_RESULTS) {
                    break
                }
                if (priceEntry is PriceEntryResult.NotFound) {
                    priceEntry = openPriceDetail(service, card.name)
                    if (priceEntry is PriceEntryResult.Opened) break
                }
            }
        }

        when (priceEntry) {
            is PriceEntryResult.Opened -> {
                emit(
                    CollectionState.READING_PRICE,
                    "读取分时电价",
                    collectedCount,
                    card.name,
                )
                val periods = PriceDetailParser.parse(priceEntry.snapshot)
                merged = merged.copy(
                    fastPrices = mergePricePeriods(merged.fastPrices, periods),
                )
                backOneLevel(service)
                waitForPage(7000, 650) { root ->
                    val assessment = PageAssessor.assess(root, card.name)
                    assessment.kind == PageKind.DETAIL ||
                        (assessment.expectedStationVisible &&
                            assessment.kind != PageKind.SEARCH_RESULTS &&
                            assessment.kind != PageKind.POPUP)
                }
                delay(600)
            }
            is PriceEntryResult.UniformPrice -> {
                emit(
                    CollectionState.READING_DETAIL,
                    "全时段统一价，无需点击分时入口",
                    collectedCount,
                    card.name,
                )
                backOneLevel(service)
                delay(800)
            }
            is PriceEntryResult.NotFound -> {
                emit(
                    CollectionState.READING_DETAIL,
                    "未找到可点击的价格入口",
                    collectedCount,
                    card.name,
                )
                backOneLevel(service)
                delay(800)
            }
        }

        emit(CollectionState.SAVING, "保存站点: ${merged.stationName}", collectedCount, merged.stationName)
        saveDetail(card, merged, city, district, query)
        emit(CollectionState.RETURNING, "返回搜索结果", collectedCount, card.name)
        val backKind = service.freshPageKind(card.name)
        if (backKind == PageKind.DETAIL || backKind == PageKind.PRICE_DETAIL || backKind == PageKind.UNKNOWN) {
            backOneLevel(service)
        } else {
            emit(CollectionState.RETURNING, "当前页非详情($backKind)，不再返回", collectedCount, card.name)
        }
        var returned: NodeSnapshot? = null
        repeat(4) {
            if (stopRequested || returned != null) return@repeat
            returned = waitForPage(5000, 600) { root ->
                val kind = PageAssessor.assess(root).kind
                kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
            }
            if (returned == null) {
                val currentKind = service.freshPageKind()
                when (currentKind) {
                    PageKind.SEARCH_RESULTS -> Unit
                    PageKind.POPUP -> dismissPopup(service)
                    PageKind.HOME -> {
                        emit(
                            CollectionState.RETURNING,
                            "已返回高德首页，重新打开搜索列表",
                            collectedCount,
                            card.name,
                        )
                        openSearchFromHome(service, query)
                    }
                    else -> clickBackToSearch(service)
                }
            } else if (PageAssessor.assess(returned).kind == PageKind.POPUP) {
                dismissPopup(service)
                returned = null
            }
        }
        if (returned == null) {
            emit(CollectionState.RETURNING, "返回列表失败，稍后重新搜索", collectedCount, card.name)
        }
        return merged
    }

    private suspend fun openPriceDetail(
        service: ChargingAccessibilityService,
        stationName: String,
    ): PriceEntryResult {
        if (stopRequested) return PriceEntryResult.NotFound
        val root = service.snapshot()
        if (root != null && DetailPageClassifier.hasUniformPrice(root)) {
            emit(
                CollectionState.OPENING_PRICE,
                "检测到全时段统一价，无需点击",
                collectedCount,
                stationName,
            )
            AppPreferences.appendLog(context, "检测到全时段统一价，无需点击")
            return PriceEntryResult.UniformPrice
        }
        val hasTrendTitle = root != null &&
            DetailPageClassifier.classify(root).features.getValue("has_price_trend")
        val screenHeight = service.screenHeightPx()
        val priceBounds = service.findNodeBounds("涨至|降至")
        if (priceBounds != null &&
            !priceBounds.isEmpty &&
            (priceBounds.centerY() >= screenHeight - 160 || priceBounds.centerY() <= 80)
        ) {
            emit(
                CollectionState.OPENING_PRICE,
                "价格入口在屏幕外，先小幅滚动",
                collectedCount,
                stationName,
            )
            return PriceEntryResult.NotFound
        }
        if (priceBounds == null && hasTrendTitle) {
            emit(
                CollectionState.OPENING_PRICE,
                "检测到24小时价格趋势图，继续小幅滚动查找涨至/降至",
                collectedCount,
                stationName,
            )
            return PriceEntryResult.NotFound
        }
        if (stopRequested) return PriceEntryResult.NotFound
        emit(
            CollectionState.OPENING_PRICE,
            "打开分时电价入口: 涨至|降至",
            collectedCount,
            stationName,
        )
        if (!service.clickNodeByRegex("涨至|降至")) {
            return PriceEntryResult.NotFound
        }
        val snapshot = waitForPage(8000, 650) { root ->
            PageAssessor.assess(root).kind == PageKind.PRICE_DETAIL
        }
        AppPreferences.appendLog(context, if (snapshot != null) "分时电价页已打开" else "点击涨至|降至后未识别到分时电价页")
        if (snapshot != null) return PriceEntryResult.Opened(snapshot)
        val afterClick = service.snapshot()
        val afterClickKind = if (afterClick != null) PageAssessor.assess(afterClick).kind else PageKind.UNKNOWN
        if (afterClickKind == PageKind.PRICE_DETAIL ||
            afterClickKind == PageKind.UNKNOWN ||
            afterClickKind == PageKind.POPUP
        ) {
            service.globalBack()
            delay(800)
        }
        return PriceEntryResult.NotFound
    }
    private suspend fun saveDetail(
        card: StationCandidate,
        detail: StationDetail,
        city: String,
        district: String,
        query: String,
    ) {
        val stationName = detail.stationName.ifBlank { card.name }
        val stationId = "amap-" + Integer.toHexString(
            StationNameNormalizer.normalize(stationName).hashCode()
        )
        val now = System.currentTimeMillis()
        val payload = linkedMapOf<String, Any?>(
            "stationName" to stationName,
            "tags" to detail.tags,
            "category" to detail.category,
            "facilities" to detail.facilities,
            "businessHours" to detail.businessHours,
            "distance" to detail.distance,
            "duration" to detail.duration,
            "address" to detail.address,
            "operator" to detail.operator,
            "currentPrice" to detail.currentPrice,
            "parkingFee" to detail.parkingFee,
            "occupancyFee" to detail.occupancyFee,
            "favoriteCount" to detail.favoriteCount,
            "viewCount" to detail.viewCount,
            "fastAvailable" to detail.fastAvailable,
            "fastTotal" to detail.fastTotal,
            "fastPower" to detail.fastPower,
            "superAvailable" to detail.superAvailable,
            "superTotal" to detail.superTotal,
            "superPower" to detail.superPower,
            "slowAvailable" to detail.slowAvailable,
            "slowTotal" to detail.slowTotal,
            "slowPower" to detail.slowPower,
            "priceTrendTitle" to detail.priceTrendTitle,
            "fastPrices" to detail.fastPrices,
            "slowPrices" to detail.slowPrices,
            "city" to city,
            "district" to district,
            "searchQuery" to query,
            "latitude" to card.latitude,
            "longitude" to card.longitude,
            "collectedAt" to now,
        )
        val resultJson = gson.toJson(payload)
        val task = ScanTaskEntity(
            stationId = stationId,
            name = stationName,
            address = detail.address,
            latitude = card.latitude,
            longitude = card.longitude,
            payloadJson = resultJson,
            status = "running",
            attempts = 0,
            maxAttempts = 3,
            leaseToken = UUID.randomUUID().toString(),
            leaseExpiresAt = now + 180_000L,
            resultJson = null,
            lastError = "",
            createdAt = now,
            updatedAt = now,
        )
        repository.saveResult(task, resultJson)
    }

    private suspend fun dismissPopup(service: ChargingAccessibilityService) {
        val keywords = listOf(
            "我知道了",
            "暂不更新",
            "以后再说",
            "仅在使用中允许",
            "使用应用时允许",
            "始终允许",
            "同意",
            "取消",
        )
        for (keyword in keywords) {
            if (stopRequested) break
            service.clickNodeByText(keyword)
            delay(900)
            val snapshot = service.snapshot() ?: continue
            if (PageAssessor.assess(snapshot).kind != PageKind.POPUP) return
        }
        service.globalBack()
        delay(900)
    }

    private suspend fun waitForSearchResults(
        service: ChargingAccessibilityService,
        query: String,
    ): NodeSnapshot? {
        repeat(2) { attempt ->
            if (attempt > 0) {
                service.globalBack()
                delay(900)
                service.openAmapSearch(query)
            }
            var snapshot = waitForPage(15000, 700) { root ->
                val kind = PageAssessor.assess(root).kind
                kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
            }
            if (snapshot != null && PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                dismissPopup(service)
                snapshot = waitForPage(12000, 650) { root ->
                    PageAssessor.assess(root).kind == PageKind.SEARCH_RESULTS
                }
            }
            if (snapshot != null) return snapshot
        }
        return null
    }

    private suspend fun waitForService(timeoutMs: Long = 10_000L): ChargingAccessibilityService? {
        emit(CollectionState.WAITING_SERVICE, "等待无障碍服务连接", collectedCount, "")
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val service = ChargingAccessibilityService.current()
            if (service != null) return service
            delay(250)
        }
        return null
    }

    private suspend fun waitForPage(
        timeoutMs: Long,
        intervalMs: Long,
        predicate: (NodeSnapshot) -> Boolean,
    ): NodeSnapshot? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (stopRequested) return null
            val service = ChargingAccessibilityService.current()
            if (service != null) {
                val snapshot = service.snapshot()
                if (snapshot != null && predicate(snapshot)) return snapshot
                val fresh = service.freshSnapshot()
                if (fresh != null && predicate(fresh)) return fresh
            }
            delay(intervalMs)
        }
        return null
    }

    private fun buildQuery(city: String, district: String, keyword: String): String {
        if (keyword.isNotBlank()) return keyword.trim()
        if (district.isNotBlank()) {
            return if (city.isNotBlank()) "${city}${district}重卡充电站" else "${district}重卡充电站"
        }
        if (city.isNotBlank()) return "${city}重卡充电站"
        return "重卡充电站"
    }

    private fun cardFingerprint(card: StationCandidate): String =
        StationNameNormalizer.normalize(card.name)

    private suspend fun scrollResultsDown(
        service: ChargingAccessibilityService,
        root: NodeSnapshot,
    ) {
        val viewport = StationMatcher.listViewport(root)
        val dispatched = if (viewport != null) {
            service.scrollWithin(viewport)
        } else {
            service.scrollForward()
        }
        if (!dispatched) {
            delay(250)
            if (viewport != null) {
                service.scrollWithin(viewport)
            } else {
                service.scrollForward()
            }
        }
        delay(850)
    }

    private fun backOneLevel(service: ChargingAccessibilityService) {
        if (!service.clickBackButton()) {
            service.globalBack()
        }
    }

    private suspend fun openSearchFromHome(service: ChargingAccessibilityService, query: String) {
        service.openAmapSearch(query)
        delay(1200)
        val kind = service.freshPageKind()
        if (kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP) return
        AppPreferences.appendLog(context, "深链未返回列表，改用搜索框输入: $query")
        service.clickNodeByViewId("com.autonavi.minimap:id/maphome_searchbar_bg")
        delay(900)
        service.setFocusedText(query)
        delay(400)
        if (!service.clickNodeByText("搜索")) {
            service.clickNodeByRegex("搜索")
        }
    }

    private fun clickBackToSearch(service: ChargingAccessibilityService) {
        backOneLevel(service)
    }

    private suspend fun ensureListTop(service: ChargingAccessibilityService, root: NodeSnapshot) {
        val pageText = collectPageText(root)
        if ("展开列表" in pageText || "扫码充电补贴" in pageText) return
        val viewportTop = StationMatcher.listViewport(root)?.top ?: 0
        val topY = StationMatcher.visibleStationCards(root).minOfOrNull { it.bounds?.top ?: 0 } ?: 0
        if (topY <= viewportTop + 52) return
        repeat(4) {
            service.scrollSmallUp()
            delay(350)
            val snapshot = service.snapshot() ?: return
            val nextViewportTop = StationMatcher.listViewport(snapshot)?.top ?: viewportTop
            val nextTop = StationMatcher.visibleStationCards(snapshot).minOfOrNull { it.bounds?.top ?: 0 } ?: 0
            if (nextTop <= nextViewportTop + 52) return
        }
    }

    private fun isListLoading(root: NodeSnapshot): Boolean =
        listLoadingKeywords.any { it in collectPageText(root) }

    private fun isListEnd(root: NodeSnapshot): Boolean =
        listEndKeywords.any { it in collectPageText(root) }

    private fun collectPageText(root: NodeSnapshot): String {
        val values = mutableListOf<String>()
        fun walk(node: NodeSnapshot) {
            if (node.text.isNotBlank()) values.add(node.text)
            if (node.contentDescription.isNotBlank()) values.add(node.contentDescription)
            node.children.forEach { walk(it) }
        }
        walk(root)
        return values.joinToString("\n")
    }

    private fun mergePricePeriods(
        existing: List<PricePeriod>,
        incoming: List<PricePeriod>,
    ): List<PricePeriod> {
        if (incoming.isEmpty()) return existing
        if (existing.isEmpty()) return incoming
        val byKey = existing.associateBy { priceKey(it) }.toMutableMap()
        for (period in incoming) {
            val key = priceKey(period)
            val current = byKey[key]
            byKey[key] = if (current == null) {
                period
            } else {
                PricePeriod(
                    time = current.time.ifEmpty { period.time },
                    totalPrice = current.totalPrice.ifEmpty { period.totalPrice },
                    elecFee = current.elecFee.ifEmpty { period.elecFee },
                    serviceFee = current.serviceFee.ifEmpty { period.serviceFee },
                    tag = current.tag.ifEmpty { period.tag },
                )
            }
        }
        return byKey.values.toList()
    }

    private fun priceKey(period: PricePeriod): String =
        period.time.ifBlank { period.tag.ifBlank { "${period.totalPrice}|${period.elecFee}|${period.serviceFee}" } }

    private fun emit(
        state: CollectionState,
        message: String,
        stationCount: Int,
        currentStation: String,
    ) {
        AppPreferences.appendLog(context, message)
        listeners.forEach { listener ->
            listener.onEvent(state, message, stationCount, currentStation)
        }
    }
}
