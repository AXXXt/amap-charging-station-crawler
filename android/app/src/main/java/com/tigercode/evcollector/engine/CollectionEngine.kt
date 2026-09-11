package com.tigercode.evcollector.engine

import android.content.Context
import com.google.gson.Gson
import com.tigercode.evcollector.AppPreferences
import com.tigercode.evcollector.accessibility.ChargingAccessibilityService
import com.tigercode.evcollector.core.engine.CollectionPacingPolicy
import com.tigercode.evcollector.core.engine.StationMatcher
import com.tigercode.evcollector.core.model.ChargingPileDetail
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.PricePeriod
import com.tigercode.evcollector.core.model.StationCandidate
import com.tigercode.evcollector.core.model.StationDetail
import com.tigercode.evcollector.core.parser.DetailPageClassifier
import com.tigercode.evcollector.core.parser.DetailPageType
import com.tigercode.evcollector.core.parser.DetailParser
import com.tigercode.evcollector.core.parser.ChargingPileDialogParser
import com.tigercode.evcollector.core.parser.PageAssessor
import com.tigercode.evcollector.core.parser.PriceDetailParser
import com.tigercode.evcollector.core.parser.ResultMerger
import com.tigercode.evcollector.core.parser.StationNameNormalizer
import com.tigercode.evcollector.data.CollectorRepository
import com.tigercode.evcollector.data.ScanTaskEntity
import java.util.ArrayDeque
import java.util.Calendar
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.math.max
import kotlin.random.Random
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

class StationNotFoundException(
    val stationName: String,
) : IllegalStateException("搜索结果中未找到目标站点: $stationName")

enum class CollectionState(val label: String) {
    IDLE("待机"),
    WAITING_SERVICE("等待无障碍服务"),
    PAUSED("限速暂停"),
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
    data class NotFound(val entryClicked: Boolean = false) : PriceEntryResult
}

/**
 * Result of the optional charging-pile dialog flow.
 *
 * A non-null StationDetail is not sufficient to say that the dialog was
 * completely handled: a slow-loading dialog can be parsed as an empty
 * snapshot. Keep completion and close confirmation explicit so the price
 * flow cannot race the dialog.
 */
private data class ChargingPileCollectionResult(
    val detail: StationDetail,
    val completed: Boolean,
    val dialogClosed: Boolean,
)

class CollectionEngine(
    private val context: Context,
    private val repository: CollectorRepository,
    private val uploadLocalResults: suspend () -> Int,
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

    @Volatile
    private var riskCooldownMs = 10 * 60 * 1000L

    private val batchTargetMin = 25
    private val batchTargetMax = 30
    private val batchCooldownMinMs = 12 * 60 * 1000L
    private val batchCooldownMaxMs = 20 * 60 * 1000L
    private val detailEntryTimes = ArrayDeque<Long>().apply {
        addAll(AppPreferences.pacingDetailEntryTimes(context))
    }
    private var batchCompletedCount = AppPreferences.pacingBatchCompletedCount(context)
    private var batchTarget = AppPreferences.pacingBatchTarget(context)
        .takeIf { it in batchTargetMin..batchTargetMax }
        ?: Random.nextInt(batchTargetMin, batchTargetMax + 1)
    private var batchCooldownUntilMs = AppPreferences.pacingCooldownUntil(context)

    private val MAX_PRICE_CLICK_ATTEMPTS = 2
    private val MAX_CHARGING_PILE_SCROLLS = 30

    private val riskPhrases = listOf(
        "操作过于频繁",
        "操作太频繁",
        "请求过于频繁",
        "访问过于频繁",
        "您操作太快了",
    )

    private val listLoadingKeywords = listOf("正在加载", "加载中", "加载更多")
    private val listEndKeywords = listOf("暂无更多内容", "没有更多了", "没有更多结果", "已经到底了")

    init {
        val nowMs = System.currentTimeMillis()
        if (batchCooldownUntilMs in 1..nowMs) {
            resetBatchCycle()
        } else {
            persistPacingState(nowMs)
        }
    }

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
                val details = runRegion(city, district, keyword)
                try {
                    val uploaded = uploadLocalResults()
                    emit(
                        CollectionState.DONE,
                        "采集完成，已同步 ${details.size} 条结果；本次上传 $uploaded 条待同步记录",
                        collectedCount,
                        "",
                    )
                } catch (error: Exception) {
                    emit(
                        CollectionState.DONE,
                        "采集完成，${details.size} 条结果已本地保存；数据库同步失败，稍后可在同步时重试",
                        collectedCount,
                        "",
                    )
                    AppPreferences.appendLog(
                        context,
                        "本地扫描结果同步失败: ${error.message ?: error.javaClass.simpleName}",
                    )
                }
            } catch (error: CancellationException) {
                throw error
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
                ensureAmapAdClosed(service)

                val query = buildQuery(city, district, keyword)
                emit(CollectionState.SCANNING_RESULTS, "搜索 $query", 0, "")
                humanDelay(1500L, 3000L)
                if (!openSearchViaSearchBar(service, query)) {
                    service.openAmapSearch(query)
                }
                var snapshot = waitForSearchResults(service, query)
                if (snapshot == null) {
                    if (stopRequested) {
                        emit(CollectionState.STOPPED, "搜索未完成，停止本次采集", 0, "")
                        return@withContext emptyList()
                    }
                    throw IllegalStateException("高德地图搜索页打开失败: $query")
                }

                val collected = mutableListOf<StationDetail>()
                val seenCards = mutableSetOf<String>()
                var unchangedScrolls = 0
                var lastPageSignature: String? = null
                var loadingWaits = 0
                var consecutiveRecoveryFails = 0

                while (!stopRequested) {
                    awaitReadyForNextTask()
                    if (stopRequested) break
                    snapshot = waitForPage(6000, 550) { root ->
                        val kind = PageAssessor.assess(root).kind
                        kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
                    }
                    if (snapshot == null) {
                        consecutiveRecoveryFails += 1
                        emit(
                            CollectionState.RETURNING,
                            "搜索结果页不可用，尝试重新打开列表",
                            collected.size,
                            "",
                        )
                        var recovered = false
                        repeat(2) {
                            if (stopRequested) return@repeat
                            recoverSearchResults(service, query)
                            snapshot = waitForPage(7000, 600) { root ->
                                val kind = PageAssessor.assess(root).kind
                                kind == PageKind.SEARCH_RESULTS || kind == PageKind.POPUP
                            }
                            if (snapshot != null) {
                                recovered = true
                                return@repeat
                            }
                            if (consecutiveRecoveryFails >= 2) {
                                humanDelay(6000L, 10000L)
                            }
                        }
                        if (!recovered) {
                            val waitMs = max(8000L, 15_000L * consecutiveRecoveryFails)
                            emit(
                                CollectionState.RETURNING,
                                "列表恢复失败，等待 ${waitMs / 1000} 秒后继续",
                                collected.size,
                                "",
                            )
                            humanDelay(waitMs)
                        }
                        continue
                    }

                    if (!ensureAmapAdClosed(service)) {
                        humanDelay(500L, 900L)
                        continue
                    }
                    if (PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                        dismissPopup(service)
                        continue
                    }

                    if (hasRiskPhrase(snapshot)) {
                        handleRiskPause(snapshot)
                        break
                    }

                    val cards = StationMatcher.visibleStationCards(snapshot)
                    if (cards.isNotEmpty()) consecutiveRecoveryFails = 0
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
                                humanDelay(1000L, 1600L)
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
                            if (!scrollResultsDown(service, snapshot, query)) {
                                recoverSearchResults(service, query)
                                snapshot = waitForSearchResults(service, query) ?: break
                            }
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
                        if (!scrollResultsDown(service, snapshot, query)) {
                            recoverSearchResults(service, query)
                            snapshot = waitForSearchResults(service, query) ?: break
                        }
                        continue
                    }
                    unchangedScrolls = 0
                    loadingWaits = 0
                    lastPageSignature = null

                    if (!ensureAmapAdClosed(service)) {
                        humanDelay(500L, 900L)
                        continue
                    }
                    seenCards.add(cardFingerprint(nextCard))
                    humanDelay(800L, 1800L)
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

    suspend fun runStationTask(
        stationId: String,
        stationName: String,
        address: String,
        latitude: Double?,
        longitude: Double?,
        remoteTaskId: String = "",
    ): StationDetail? = runMutex.withLock {
        withContext(Dispatchers.Main.immediate) {
            stopRequested = false
            collectedCount = 0
            val requestedName = stationName.trim()
            require(requestedName.isNotBlank()) { "站点名称不能为空" }
            awaitReadyForNextTask()
            if (stopRequested) {
                emit(CollectionState.STOPPED, "限速等待期间收到停止请求", 0, requestedName)
                stopRequested = false
                return@withContext null
            }
            val service = waitForService() ?: throw IllegalStateException(
                "无障碍服务未连接，请先到系统设置开启采集助手"
            )

            emit(CollectionState.OPENING_AMAP, "打开高德地图", 0, requestedName)
            ensureAmapAdClosed(service)
            emit(CollectionState.SCANNING_RESULTS, "搜索 $requestedName", 0, requestedName)
            humanDelay(1500L, 3000L)
            if (!openSearchViaSearchBar(service, requestedName)) {
                service.openAmapSearch(requestedName)
            }
            val initialSnapshot = waitForSearchResults(service, requestedName)
            if (initialSnapshot == null) {
                if (stopRequested) {
                    emit(CollectionState.STOPPED, "搜索未完成，停止本次采集", 0, requestedName)
                    stopRequested = false
                    return@withContext null
                }
                throw IllegalStateException("高德地图搜索页打开失败: $requestedName")
            }
            var snapshot: NodeSnapshot = initialSnapshot

            var detail: StationDetail? = null
            var matchedTarget = false
            var scrollAttempts = 0
            var unchangedScrolls = 0
            var lastSignature = ""
            while (!stopRequested && scrollAttempts <= 6) {
                if (PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                    dismissPopup(service)
                    snapshot = waitForPage(8000, 650) { root ->
                        PageAssessor.assess(root).kind == PageKind.SEARCH_RESULTS
                    } ?: break
                    continue
                }
                if (hasRiskPhrase(snapshot)) {
                    handleRiskPause(snapshot)
                    break
                }

                val match = StationMatcher.bestMatch(snapshot, requestedName)
                if (match != null) {
                    matchedTarget = true
                    val card = StationCandidate(
                        name = requestedName,
                        id = stationId,
                        address = address,
                        latitude = latitude,
                        longitude = longitude,
                        centerX = match.centerX,
                        centerY = match.centerY,
                        searchQuery = requestedName,
                        clickVisible = true,
                    )
                    detail = collectStation(
                        service = service,
                        card = card,
                        city = "",
                        district = "",
                        query = requestedName,
                        remoteTaskId = remoteTaskId,
                    )
                    break
                }

                if (isListEnd(snapshot) || scrollAttempts >= 6) break
                val signature = StationMatcher.visibleStationCards(snapshot)
                    .joinToString("|") { cardFingerprint(it) }
                unchangedScrolls = if (signature.isNotBlank() && signature == lastSignature) {
                    unchangedScrolls + 1
                } else {
                    0
                }
                if (unchangedScrolls >= 2) break
                lastSignature = signature

                emit(
                    CollectionState.SCANNING_RESULTS,
                    "未匹配到目标站点，向下滚动查找 (${scrollAttempts + 1}/6)",
                    0,
                    requestedName,
                )
                // Remote station-detail tasks keep their existing target-search
                // behavior. The conservative overlap step is for batch/list
                // scanning only, where skipping an unseen card is costly.
                val scrolled = scrollResultsDown(
                    service,
                    snapshot,
                    requestedName,
                    conservative = false,
                )
                if (!scrolled) {
                    recoverSearchResults(service, requestedName)
                    snapshot = waitForSearchResults(service, requestedName) ?: break
                    continue
                }
                scrollAttempts += 1
                snapshot = waitForPage(7000, 600) { root ->
                    isConfirmedSearchResults(root, requestedName) ||
                        PageAssessor.assess(root).kind == PageKind.POPUP
                } ?: break
            }

            if (detail != null) {
                collectedCount = 1
                emit(CollectionState.DONE, "目标站点采集完成", 1, requestedName)
            } else if (stopRequested) {
                emit(CollectionState.STOPPED, "目标站点采集已停止", 0, requestedName)
            } else if (!matchedTarget) {
                emit(
                    CollectionState.DONE,
                    "搜索结果中没有对应站点，已跳过: $requestedName",
                    0,
                    requestedName,
                )
                stopRequested = false
                throw StationNotFoundException(requestedName)
            } else {
                emit(CollectionState.ERROR, "已找到目标站点，但详情采集失败: $requestedName", 0, requestedName)
            }
            stopRequested = false
            detail
        }
    }

    private suspend fun collectStation(
        service: ChargingAccessibilityService,
        card: StationCandidate,
        city: String,
        district: String,
        query: String,
        remoteTaskId: String = "",
    ): StationDetail? {
        val centerX = card.centerX ?: return null
        val centerY = card.centerY ?: return null
        emit(
            CollectionState.OPENING_DETAIL,
            "进入详情: ${card.name}",
            collectedCount,
            card.name,
        )
        if (!awaitDetailEntryPermit(card.name)) return null
        val clicked = service.clickAt(centerX, centerY) ||
            service.clickStationCard(card.name) ||
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
            if (!awaitDetailEntryPermit(card.name)) return null
            service.clickAt(centerX, centerY) || service.clickStationCard(card.name)
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
            humanDelay(1400L, 2200L)
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
        var chargingPileDialogHandled = false
        collectChargingPileDetailsIfVisible(service, card.name)?.let { result ->
            merged = ResultMerger.merge(merged, result.detail)
            chargingPileDialogHandled = result.completed && result.dialogClosed
            if (!result.completed) {
                AppPreferences.appendLog(
                    context,
                    "电桩详情尚未完整采集，禁止直接认为流程完成: ${card.name}",
                )
            }
            if (!result.dialogClosed) {
                AppPreferences.appendLog(
                    context,
                    "电桩详情弹窗尚未确认关闭: ${card.name}",
                )
            }
        }
        // A slow first render can make the first dialog attempt return a
        // partial result. Retry once before the price flow, never after it.
        if (!chargingPileDialogHandled && !stopRequested &&
            service.findChargingPileHistoryEntryBounds() != null
        ) {
            collectChargingPileDetailsIfVisible(service, card.name)?.let { result ->
                merged = ResultMerger.merge(merged, result.detail)
                chargingPileDialogHandled = result.completed && result.dialogClosed
                if (!result.completed) {
                    AppPreferences.appendLog(
                        context,
                        "重试后电桩详情仍未完整采集: ${card.name}",
                    )
                }
            }
        }
        if (chargingPileDialogHandled) {
            emit(
                CollectionState.READING_DETAIL,
                "电桩详情已完成，开始采集分时电价",
                collectedCount,
                card.name,
            )
            val refreshedDetail = waitForPage(5000L, 400L) { root ->
                val assessment = PageAssessor.assess(root, card.name)
                assessment.kind == PageKind.DETAIL ||
                    (assessment.expectedStationVisible &&
                        assessment.kind != PageKind.SEARCH_RESULTS &&
                        assessment.kind != PageKind.POPUP)
            } ?: service.freshSnapshot()
            if (refreshedDetail != null) detailSnapshot = refreshedDetail
        }
        val pageInfo = DetailPageClassifier.classify(detailSnapshot)
        val scrolls = maxOf(
            when (pageInfo.type) {
                DetailPageType.BASIC -> 3
                DetailPageType.FULL_TREND -> 5
                else -> minOf(maxOf(pageInfo.scrollsNeeded, 1), 3)
            },
            3,
        )

        // Never start the price flow while a pile dialog or an AMap ad is
        // still present. Use fresh accessibility snapshots because cached
        // state can lag behind a visual close animation.
        val adClosedBeforePrice = ensureAmapAdClosed(service)
        val dialogClosedBeforePrice = adClosedBeforePrice &&
            closeChargingPileDialogAndReturn(service, card.name)
        if (!dialogClosedBeforePrice) {
            AppPreferences.appendLog(
                context,
                "分时电价前无法确认电桩弹窗已关闭，跳过价格入口: ${card.name}",
            )
        }

        var priceClickAttempts = 0
        var priceEntry: PriceEntryResult = PriceEntryResult.NotFound()
        if (dialogClosedBeforePrice) {
        priceEntry = openPriceDetail(service, card.name, priceClickAttempts)
        if (priceEntry is PriceEntryResult.NotFound && priceEntry.entryClicked) {
            priceClickAttempts += 1
        }
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
                    humanDelay(300L, 700L)
                    service.scrollSmallDown()
                }
                humanDelay(1800L, 2600L)
                val scrolledSnapshot = service.snapshot() ?: continue
                val parsed = DetailParser.parse(scrolledSnapshot)
                if (parsed.filledFieldCount > 0) {
                    merged = ResultMerger.merge(merged, parsed)
                }
                val assessment = PageAssessor.assess(scrolledSnapshot, card.name)
                if (assessment.kind == PageKind.PRICE_DETAIL) {
                    service.globalBack()
                    humanDelay(1000L, 1800L)
                    continue
                } else if (assessment.kind == PageKind.SEARCH_RESULTS) {
                    break
                }
                if (priceEntry is PriceEntryResult.NotFound) {
                    priceEntry = openPriceDetail(service, card.name, priceClickAttempts)
                    if (priceEntry is PriceEntryResult.NotFound && priceEntry.entryClicked) {
                        priceClickAttempts += 1
                    }
                    if (priceEntry is PriceEntryResult.Opened) break
                }
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
                humanDelay(1800L, 3000L)
            }
            is PriceEntryResult.UniformPrice -> {
                emit(
                    CollectionState.READING_DETAIL,
                    "全时段统一价，无需点击分时入口",
                    collectedCount,
                    card.name,
                )
                backOneLevel(service)
                humanDelay(1800L, 3000L)
            }
            is PriceEntryResult.NotFound -> {
                emit(
                    CollectionState.READING_DETAIL,
                    "未找到可点击的价格入口",
                    collectedCount,
                    card.name,
                )
                backOneLevel(service)
                humanDelay(1800L, 3000L)
            }
        }

        if (stopRequested) {
            emit(CollectionState.RETURNING, "已停止，跳过保存", collectedCount, card.name)
            return null
        }

        emit(CollectionState.SAVING, "保存站点: ${merged.stationName}", collectedCount, merged.stationName)
        saveDetail(card, merged, city, district, query, remoteTaskId)
        recordStationCompletedForPacing(merged.stationName)
        humanDelay(900L, 1700L)
        emit(CollectionState.RETURNING, "返回搜索结果", collectedCount, card.name)
        if (stopRequested) return merged
        val backKind = service.freshPageKind(card.name)
        if (backKind == PageKind.DETAIL || backKind == PageKind.PRICE_DETAIL || backKind == PageKind.UNKNOWN) {
            humanDelay(1800L, 3000L)
            backOneLevel(service)
        } else {
            emit(CollectionState.RETURNING, "当前页非详情($backKind)，不再返回", collectedCount, card.name)
        }
        var returned: NodeSnapshot? = null
        repeat(2) {
            if (stopRequested || returned != null) return@repeat
            returned = waitForPage(6500, 650) { root ->
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
                            "回到首页，稍候重新打开搜索列表",
                            collectedCount,
                            card.name,
                        )
                        humanDelay(2500L, 4000L)
                        openSearchFromHome(service, query)
                    }
                    else -> clickBackToSearch(service)
                }
                humanDelay(1800L, 3000L)
            } else if (PageAssessor.assess(returned).kind == PageKind.POPUP) {
                dismissPopup(service)
                returned = null
            }
        }
        if (returned == null) {
            emit(CollectionState.RETURNING, "返回列表失败，稍后重新搜索", collectedCount, card.name)
            humanDelay(5000L, 9000L)
        }
        return merged
    }

    private suspend fun openPriceDetail(
        service: ChargingAccessibilityService,
        stationName: String,
        clickAttempts: Int = 0,
    ): PriceEntryResult {
        if (stopRequested) return PriceEntryResult.NotFound()
        val root = service.freshSnapshot()
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
        val priceBounds = service.findPriceEntryBounds()
        if (priceBounds != null && !service.isPriceEntrySafelyVisible(priceBounds)) {
            emit(
                CollectionState.OPENING_PRICE,
                "价格入口在屏幕外，先小幅滚动",
                collectedCount,
                stationName,
            )
            return PriceEntryResult.NotFound()
        }
        if (priceBounds == null && hasTrendTitle) {
            emit(
                CollectionState.OPENING_PRICE,
                "检测到24小时价格趋势图，继续小幅滚动查找涨至/降至",
                collectedCount,
                stationName,
            )
            return PriceEntryResult.NotFound()
        }
        if (stopRequested) return PriceEntryResult.NotFound()
        if (clickAttempts >= MAX_PRICE_CLICK_ATTEMPTS) {
            emit(
                CollectionState.OPENING_PRICE,
                "价格入口点击未生效，已达到重试上限",
                collectedCount,
                stationName,
            )
            return PriceEntryResult.NotFound()
        }
        emit(
            CollectionState.OPENING_PRICE,
            "打开分时电价入口: 涨至|降至",
            collectedCount,
            stationName,
        )
        humanDelay(2000L, 3500L)
        if (!service.clickPriceEntry()) {
            return PriceEntryResult.NotFound()
        }
        val snapshot = waitForPage(8000, 750) { root ->
            PageAssessor.assess(root).kind == PageKind.PRICE_DETAIL
        }
        AppPreferences.appendLog(context, if (snapshot != null) "分时电价页已打开" else "点击涨至|降至后未识别到分时电价页")
        if (snapshot != null) return PriceEntryResult.Opened(snapshot)
        if (stopRequested) return PriceEntryResult.NotFound()
        val afterClick = service.snapshot()
        val afterClickKind = if (afterClick != null) PageAssessor.assess(afterClick).kind else PageKind.UNKNOWN
        if (afterClickKind == PageKind.PRICE_DETAIL ||
            afterClickKind == PageKind.UNKNOWN ||
            afterClickKind == PageKind.POPUP
        ) {
            service.globalBack()
            humanDelay(1800L, 3000L)
        }
        return PriceEntryResult.NotFound(entryClicked = true)
    }
    private suspend fun saveDetail(
        card: StationCandidate,
        detail: StationDetail,
        city: String,
        district: String,
        query: String,
        remoteTaskId: String,
    ) {
        val stationName = detail.stationName.ifBlank { card.name }
        val stationId = card.id.ifBlank {
            "amap-" + Integer.toHexString(
                StationNameNormalizer.normalize(stationName).hashCode()
            )
        }
        val now = System.currentTimeMillis()
        val payload = linkedMapOf<String, Any?>(
            "stationName" to stationName,
            "remoteTaskId" to remoteTaskId,
            "sourceStationId" to card.id,
            "requestedStationName" to card.name,
            "sourceAddress" to card.address,
            "sourceLatitude" to card.latitude,
            "sourceLongitude" to card.longitude,
            "tags" to detail.tags,
            "category" to detail.category,
            "facilities" to detail.facilities,
            "businessHours" to detail.businessHours,
            "distance" to detail.distance,
            "duration" to detail.duration,
            "address" to detail.address.ifBlank { card.address },
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
            "chargingPileCount" to detail.chargingPiles.size,
            "chargingPileTotal" to detail.chargingPileTotal,
            "chargingPiles" to detail.chargingPiles,
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
            address = detail.address.ifBlank { card.address },
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
        // The engine builds a durable outbox record here. The authoritative
        // task lease lives on the server, so this synthetic local entity must
        // not be treated as a locally claimed task.
        repository.saveResult(task, resultJson, markLocalTaskSucceeded = false)
    }

    private suspend fun collectChargingPileDetailsIfVisible(
        service: ChargingAccessibilityService,
        stationName: String,
    ): ChargingPileCollectionResult? {
        if (stopRequested) return null
        val current = service.freshSnapshot() ?: return null
        var dialogSnapshot = current.takeIf(ChargingPileDialogParser::isDialog)

        if (dialogSnapshot == null) {
            val entryBounds = service.findChargingPileHistoryEntryBounds() ?: return null
            emit(
                CollectionState.READING_DETAIL,
                "打开查看历史空闲，采集电桩详情",
                collectedCount,
                stationName,
            )
            humanDelay(700L, 1200L)
            if (!service.clickChargingPileHistoryEntry()) {
                AppPreferences.appendLog(context, "查看历史空闲入口点击失败: $entryBounds")
                return null
            }
            dialogSnapshot = waitForPage(10_000, 400) { root ->
                ChargingPileDialogParser.isDialog(root)
            }
            if (dialogSnapshot == null) {
                // The dialog can finish opening just after the last regular
                // poll. Always use a fresh snapshot for this final check.
                dialogSnapshot = service.freshSnapshot()
                    ?.takeIf(ChargingPileDialogParser::isDialog)
            }
            if (dialogSnapshot == null) {
                AppPreferences.appendLog(context, "点击查看历史空闲后未识别到电桩详情弹窗")
                return null
            }
        }

        // Seeing the dialog title is not enough: AMap may expose the shell of
        // the dialog before the pile list has rendered. Wait for usable data
        // and for one repeated, stable snapshot before parsing/scrolling.
        var readySnapshot = dialogSnapshot
        var previousReadySignature = ""
        var stableReadyPolls = 0
        val readyDeadline = System.currentTimeMillis() + 12_000L
        while (!stopRequested && System.currentTimeMillis() < readyDeadline) {
            val fresh = service.freshSnapshot()
            if (fresh == null || !ChargingPileDialogParser.isDialog(fresh)) break
            readySnapshot = fresh
            val parsed = ChargingPileDialogParser.parse(fresh)
            val signature = buildPileSignature(parsed)
            val hasUsableContent = parsed.expectedTotal > 0 || parsed.piles.isNotEmpty()
            stableReadyPolls = if (hasUsableContent && signature == previousReadySignature) {
                stableReadyPolls + 1
            } else {
                0
            }
            previousReadySignature = signature
            if (hasUsableContent && stableReadyPolls >= 1) break
            delay(450L)
        }
        dialogSnapshot = readySnapshot
        val initialParsed = ChargingPileDialogParser.parse(dialogSnapshot)
        val contentReady = initialParsed.expectedTotal > 0 || initialParsed.piles.isNotEmpty()
        if (!contentReady) {
            AppPreferences.appendLog(context, "电桩详情弹窗已出现但内容仍未加载完成: $stationName")
        }

        var expectedTotal = 0
        var piles = emptyList<ChargingPileDetail>()
        var previousSignature = ""
        var noProgressScrolls = 0
        var reachedBottom = false
        var activeDialogSnapshot = checkNotNull(dialogSnapshot)
        var dialogClosed = false

        try {
            for (scrollIndex in 0..MAX_CHARGING_PILE_SCROLLS) {
                if (stopRequested) break
                val parsed = ChargingPileDialogParser.parse(activeDialogSnapshot)
                expectedTotal = maxOf(expectedTotal, parsed.expectedTotal)
                val beforeCount = piles.size
                piles = ChargingPileDialogParser.merge(piles, parsed.piles)
                val signature = buildPileSignature(parsed)
                noProgressScrolls = if (
                    piles.size == beforeCount &&
                    signature.isNotBlank() &&
                    signature == previousSignature
                ) {
                    noProgressScrolls + 1
                } else {
                    0
                }
                previousSignature = signature

                emit(
                    CollectionState.READING_DETAIL,
                    "读取电桩详情 ${piles.size}/${expectedTotal.takeIf { it > 0 } ?: "?"}",
                    collectedCount,
                    stationName,
                )
                // A known total needs one stable no-op scroll at the bottom;
                // an unknown total needs three unchanged snapshots. Merely
                // seeing the dialog or reaching a failed scroll is not enough.
                val reachedExpectedTotal = expectedTotal > 0 && piles.size >= expectedTotal
                val confirmedAtBottom = reachedExpectedTotal && noProgressScrolls >= 1
                val stableWithoutKnownTotal = expectedTotal <= 0 &&
                    piles.isNotEmpty() && noProgressScrolls >= 3
                if (confirmedAtBottom || stableWithoutKnownTotal) {
                    reachedBottom = true
                    break
                }
                if (scrollIndex >= MAX_CHARGING_PILE_SCROLLS) {
                    reachedBottom = piles.isNotEmpty() && expectedTotal > 0 &&
                        piles.size >= expectedTotal
                    break
                }

                val stillOpen = service.freshSnapshot()
                    ?.let(ChargingPileDialogParser::isDialog)
                    ?: false
                if (!stillOpen || !service.scrollChargingPileDialog()) break
                humanDelay(1100L, 1700L)
                activeDialogSnapshot = service.freshSnapshot()
                    ?.takeIf(ChargingPileDialogParser::isDialog)
                    ?: break
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Throwable) {
            AppPreferences.appendLog(
                context,
                "电桩详情采集异常，继续原流程: ${error.message ?: error.javaClass.simpleName}",
            )
        } finally {
            // Price lookup is not allowed to start until this function has
            // confirmed that the visual dialog is actually gone.
            dialogClosed = closeChargingPileDialogAndReturn(service, stationName)
        }

        val complete = contentReady && reachedBottom &&
            ((expectedTotal > 0 && piles.size >= expectedTotal) ||
                (expectedTotal <= 0 && piles.isNotEmpty())) &&
            dialogClosed
        if (complete) {
            AppPreferences.appendLog(
                context,
                "电桩详情采集完成: ${piles.size}/${expectedTotal.takeIf { it > 0 } ?: "?"}",
            )
        } else {
            AppPreferences.appendLog(
                context,
                "电桩详情采集未完成: ${piles.size}/${expectedTotal.takeIf { it > 0 } ?: "?"}, " +
                    "bottom=$reachedBottom closed=$dialogClosed",
            )
        }
        return ChargingPileCollectionResult(
            detail = StationDetail(
                stationName = stationName,
                chargingPileTotal = expectedTotal,
                chargingPiles = piles,
            ),
            completed = complete,
            dialogClosed = dialogClosed,
        )
    }

    private fun buildPileSignature(snapshot: com.tigercode.evcollector.core.model.ChargingPileDialogSnapshot): String =
        "${snapshot.expectedTotal}:" +
            snapshot.piles.joinToString("|") {
                "${it.deviceId}:${it.filledFieldCount}:${it.chargingType}:${it.ratedPower}:" +
                    "${it.ratedCurrent}:${it.ratedVoltage}:${it.status}"
            }

    private suspend fun closeChargingPileDialogAndReturn(
        service: ChargingAccessibilityService,
        stationName: String,
    ): Boolean {
        // Do not trust the cached accessibility tree here. It can still
        // describe the pre-close state while the dialog is animating.
        val snapshot = service.freshSnapshot()
        if (snapshot == null || !ChargingPileDialogParser.isDialog(snapshot)) return true

        emit(
            CollectionState.READING_DETAIL,
            "关闭电桩详情，继续站点采集",
            collectedCount,
            stationName,
        )
        val clicked = service.closeChargingPileDialog()
        if (!clicked) {
            AppPreferences.appendLog(context, "关闭电桩详情按钮点击失败，改用返回键: $stationName")
            service.globalBack()
        }
        if (stopRequested) return false

        val closed = waitForPage(5000, 300) { root ->
            !ChargingPileDialogParser.isDialog(root)
        }
        if (closed != null) {
            // Avoid racing the exit animation. Confirm with a fresh tree after
            // the animation, rather than relying on the cached snapshot.
            humanDelay(900L, 1500L)
            val stillOpen = service.freshSnapshot()
                ?.let(ChargingPileDialogParser::isDialog)
                ?: false
            if (!stillOpen) {
                AppPreferences.appendLog(context, "电桩详情弹窗已确认关闭: $stationName")
                return true
            }
        }

        // A close click can be swallowed while the dialog is rendering. Use
        // one back-key fallback, then require a fresh snapshot to prove that
        // the overlay is gone before allowing price lookup.
        val stillOpen = service.freshSnapshot()
            ?.let(ChargingPileDialogParser::isDialog)
            ?: false
        if (stillOpen) {
            AppPreferences.appendLog(context, "电桩详情弹窗仍未关闭，使用返回键重试: $stationName")
            service.globalBack()
            humanDelay(900L, 1400L)
        }
        val finalOpen = service.freshSnapshot()
            ?.let(ChargingPileDialogParser::isDialog)
            ?: false
        val confirmedClosed = !finalOpen
        AppPreferences.appendLog(
            context,
            if (confirmedClosed) {
                "电桩详情弹窗已确认关闭: $stationName"
            } else {
                "电桩详情弹窗关闭确认失败: $stationName"
            },
        )
        return confirmedClosed
    }

    private suspend fun ensureAmapAdClosed(service: ChargingAccessibilityService): Boolean {
        if (!service.hasAmapAdCloseButton()) return true

        AppPreferences.appendLog(context, "检测到高德广告弹窗，尝试关闭")
        repeat(3) { attempt ->
            if (service.closeAmapAdIfPresent()) {
                repeat(10) {
                    delay(180L)
                    if (!service.hasAmapAdCloseButton()) {
                        AppPreferences.appendLog(
                            context,
                            "高德广告已确认关闭(${attempt + 1}/3)",
                        )
                        return true
                    }
                }
            }
            // The coupon close control can be visible for a short time and
            // can swallow the first gesture, so keep a short settle delay
            // before both retries.
            delay(350L)
        }

        AppPreferences.appendLog(context, "高德广告关闭失败，暂停当前页面操作")
        return false
    }

    private suspend fun dismissPopup(service: ChargingAccessibilityService) {
        // This promotional overlay is not a standard Android dialog and has
        // no text button such as "我知道了". Handle it before the generic
        // popup fallback so we never continue behind its full-screen mask.
        if (service.hasAmapAdCloseButton()) {
            ensureAmapAdClosed(service)
            return
        }
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
            humanDelay(900L, 1600L)
            val snapshot = service.snapshot() ?: continue
            if (PageAssessor.assess(snapshot).kind != PageKind.POPUP) return
        }
        service.globalBack()
        humanDelay(1000L, 1800L)
    }

    private suspend fun waitForSearchResults(
        service: ChargingAccessibilityService,
        query: String,
    ): NodeSnapshot? {
        repeat(2) { attempt ->
            if (attempt > 0) {
                humanDelay(5000L, 8000L)
                recoverSearchResults(service, query)
            }
            var snapshot = waitForPage(15000, 850) { root ->
                isConfirmedSearchResults(root, query) ||
                    PageAssessor.assess(root).kind == PageKind.POPUP
            }
            if (snapshot != null && PageAssessor.assess(snapshot).kind == PageKind.POPUP) {
                dismissPopup(service)
                snapshot = waitForPage(12000, 800) { root ->
                    isConfirmedSearchResults(root, query)
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
                if (snapshot != null) {
                    if (hasRiskPhrase(snapshot)) {
                        handleRiskPause(snapshot)
                        return null
                    }
                    if (predicate(snapshot)) return snapshot
                }
                val fresh = service.freshSnapshot()
                if (fresh != null) {
                    if (hasRiskPhrase(fresh)) {
                        handleRiskPause(fresh)
                        return null
                    }
                    if (predicate(fresh)) return fresh
                }
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
        query: String,
        conservative: Boolean = true,
    ): Boolean {
        if (!ensureAmapAdClosed(service)) {
            AppPreferences.appendLog(context, "广告弹窗未能关闭，禁止执行搜索结果滑动: $query")
            return false
        }
        // The search editor can leave result-card nodes in the accessibility
        // tree while the keyboard is open. Never swipe that state: a full
        // screen gesture hits the IME (including the '%' key) rather than the
        // AMap result list.
        if (!isConfirmedSearchResults(root, query)) {
            AppPreferences.appendLog(context, "搜索尚未提交，禁止滑动: $query")
            return false
        }
        val viewport = StationMatcher.listViewport(root)
        val beforeFingerprints = if (conservative) {
            StationMatcher.visibleStationCards(root)
                .map(::cardFingerprint)
                .toSet()
        } else {
            emptySet()
        }
        val recommendedDistance = if (conservative) {
            StationMatcher.recommendedScrollDistance(root)
        } else {
            null
        }
        var dispatched = if (viewport != null) {
            service.scrollWithin(viewport, recommendedDistance)
        } else {
            // Only use the full-screen fallback after the submitted-result
            // guard above has passed; it is unsafe while the keyboard is open.
            service.scrollForward()
        }
        if (!dispatched) {
            humanDelay(350L, 800L)
            dispatched = if (viewport != null) {
                service.scrollWithin(viewport, recommendedDistance)
            } else {
                service.scrollForward()
            }
        }
        if (dispatched) {
            AppPreferences.appendLog(
                context,
                if (conservative) {
                    "本机扫描列表小步滚动 distance=${recommendedDistance ?: "fallback"}: " +
                        "${beforeFingerprints.size} 个可见站点"
                } else {
                    "目标站点搜索列表滚动: $query"
                },
            )
            humanDelay(if (conservative) 750L else 900L, if (conservative) 1050L else 1500L)

            if (conservative && viewport != null && beforeFingerprints.size >= 2) {
                val after = service.freshSnapshot()
                if (after != null &&
                    PageAssessor.assess(after).kind == PageKind.SEARCH_RESULTS
                ) {
                    val afterFingerprints = StationMatcher.visibleStationCards(after)
                        .map(::cardFingerprint)
                        .toSet()
                    val hasOverlap = beforeFingerprints.any { it in afterFingerprints }
                    if (afterFingerprints.isNotEmpty() && !hasOverlap && !isListEnd(after)) {
                        // A device/AMap combination may still turn a short
                        // gesture into a fling. Reverse the same small step
                        // once to restore an overlap anchor before the caller
                        // chooses the next unseen card.
                        val correction = recommendedDistance ?: 320
                        AppPreferences.appendLog(
                            context,
                            "滚动后未保留列表重叠卡片，执行一次反向校正 distance=$correction: $query",
                        )
                        service.scrollWithin(viewport, -correction)
                        humanDelay(650L, 900L)
                    }
                }
            }
        }
        return dispatched
    }

    private fun backOneLevel(service: ChargingAccessibilityService) {
        if (!service.clickBackButton()) {
            service.globalBack()
        }
    }

    private suspend fun openSearchFromHome(service: ChargingAccessibilityService, query: String) {
        if (openSearchViaSearchBar(service, query)) return
        AppPreferences.appendLog(context, "搜索框入口未生效，深链兜底: $query")
        service.openAmapSearch(query)
        humanDelay(2500L, 4000L)
    }

    private suspend fun openSearchViaSearchBar(
        service: ChargingAccessibilityService,
        query: String,
    ): Boolean {
        if (stopRequested) return false
        if (!ensureAmapAdClosed(service)) return false
        val pageKind = service.freshPageKind()
        when (pageKind) {
            PageKind.SEARCH_RESULTS -> {
                val currentSnapshot = service.freshSnapshot()
                if (currentSnapshot != null && searchQueryMatches(currentSnapshot, query)) return true
                val searchBarOpened = service.clickNodeByRegex("搜索框") ||
                    service.clickTopSearchBar()
                if (!searchBarOpened) {
                    AppPreferences.appendLog(context, "未能打开当前搜索框，深链切换搜索: $query")
                    return false
                }
                humanDelay(900L, 1500L)
            }
            PageKind.POPUP -> {
                dismissPopup(service)
                return false
            }
            else -> Unit
        }

        if (pageKind != PageKind.SEARCH_RESULTS) {
            val searchBarClicked = service.clickNodeByViewId(
                "com.autonavi.minimap:id/maphome_searchbar_bg"
            )
            humanDelay(1200L, 2000L)
            if (!searchBarClicked && !service.clickNodeByRegex("搜索框|搜索地点")) {
                // A previous recovery may already have opened the search page
                // without exposing a home-style search bar. Try the visible
                // input directly instead of replacing a usable editor with a
                // deep link.
                if (!service.focusSearchInput()) {
                    AppPreferences.appendLog(context, "未找到首页搜索框或已打开的搜索输入框，等待深链兜底")
                    return false
                }
                AppPreferences.appendLog(context, "首页搜索入口未识别，继续使用已打开的搜索输入框")
            }
            if (!searchBarClicked) humanDelay(900L, 1500L)
        }

        if (!service.focusSearchInput()) {
            AppPreferences.appendLog(context, "搜索输入框未出现，深链切换搜索: $query")
            return false
        }
        humanDelay(700L, 1300L)

        var written = false
        for (attempt in 0 until 2) {
            if (service.setFocusedText(query)) {
                humanDelay(250L, 500L)
                val inputSnapshot = service.freshSnapshot()
                written = inputSnapshot != null && searchQueryMatches(inputSnapshot, query)
                if (written) break
                val actual = inputSnapshot?.let(::searchBoxQuery).orEmpty()
                AppPreferences.appendLog(
                    context,
                    "搜索词校验不一致(${attempt + 1}/2): expected=$query actual=$actual",
                )
                if (attempt == 0) {
                    service.focusSearchInput()
                    humanDelay(300L, 600L)
                }
            }
        }
        if (!written) {
            AppPreferences.appendLog(context, "搜索词写入失败，深链切换搜索: $query")
            return false
        }
        humanDelay(800L, 1400L)
        val submitted = service.clickSearchSubmitButton() ||
            service.clickNodeByRegex("^搜索$")
        if (!submitted) {
            AppPreferences.appendLog(context, "搜索按钮未生效，深链切换搜索: $query")
            return false
        }
        val confirmed = waitForPage(5000L, 250L) { root ->
            isConfirmedSearchResults(root, query) ||
                PageAssessor.assess(root).kind == PageKind.POPUP
        } != null
        if (!confirmed) {
            AppPreferences.appendLog(context, "点击搜索后仍停留在输入框，深链重开: $query")
        }
        return confirmed
    }

    private fun isConfirmedSearchResults(root: NodeSnapshot, query: String): Boolean {
        if (PageAssessor.assess(root).kind != PageKind.SEARCH_RESULTS) return false
        // The AMap search editor may be present above stale result cards. A
        // focused EditText means the keyboard/input page is still active.
        if (hasFocusedSearchInput(root)) return false
        val queryApplied = searchQueryMatches(root, query)
        val targetVisible = StationMatcher.bestMatch(root, query) != null
        return queryApplied || targetVisible
    }

    private fun hasFocusedSearchInput(root: NodeSnapshot): Boolean {
        fun walk(node: NodeSnapshot): Boolean {
            if (node.className == "android.widget.EditText" &&
                node.enabled && node.focused && node.bounds.isValid
            ) return true
            return node.children.any(::walk)
        }
        return walk(root)
    }

    private fun searchBoxQuery(root: NodeSnapshot): String? {
        val prefix = Regex("^搜索框[，,：:]\\s*(.*)$")
        var topBarText: String? = null
        fun walk(node: NodeSnapshot): String? {
            val description = node.contentDescription.trim()
            val match = prefix.find(description)
            if (match != null && match.groupValues[1].isNotBlank()) return match.groupValues[1].trim()
            val bounds = node.bounds
            if (topBarText == null &&
                node.text.isNotBlank() &&
                bounds.top in 70..260 &&
                bounds.bottom <= 300 &&
                bounds.left >= 80 &&
                bounds.right >= 500 &&
                node.text !in setOf("搜索", "返回", "关闭")
            ) {
                topBarText = node.text.trim()
            }
            node.children.forEach { child ->
                walk(child)?.let { return it }
            }
            return null
        }
        return walk(root) ?: topBarText
    }

    private fun searchQueryMatches(root: NodeSnapshot, query: String): Boolean {
        val displayed = searchBoxQuery(root) ?: return false
        val expectedNormalized = StationNameNormalizer.normalize(query)
        val displayedNormalized = StationNameNormalizer.normalize(displayed)
        // Do not use contains here. For example, a stale query such as
        // "A站点%%%%" contains "A站点" and would make the caller believe the
        // new search was already submitted, so the search button is skipped.
        return displayedNormalized == expectedNormalized
    }

    private suspend fun recoverSearchResults(service: ChargingAccessibilityService, query: String) {
        when (service.freshPageKind()) {
            PageKind.SEARCH_RESULTS -> {
                val snapshot = service.freshSnapshot()
                if (snapshot != null && searchQueryMatches(snapshot, query)) return
                if (!openSearchViaSearchBar(service, query)) service.openAmapSearch(query)
            }
            PageKind.POPUP -> dismissPopup(service)
            PageKind.HOME -> openSearchFromHome(service, query)
            else -> clickBackToSearch(service)
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
        repeat(3) {
            service.scrollSmallUp()
            humanDelay(450L, 900L)
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



    /**
     * Wait before claiming/touching the next station. The state is persisted so
     * restarting the service does not accidentally erase an active cooldown.
     */
    suspend fun awaitReadyForNextTask() {
        val nowMs = System.currentTimeMillis()
        if (batchCooldownUntilMs <= 0L) return
        if (batchCooldownUntilMs <= nowMs) {
            resetBatchCycle()
            return
        }

        val initialRemainingMs = batchCooldownUntilMs - nowMs
        emit(
            CollectionState.PAUSED,
            "批次限速暂停中，剩余约 ${minutesRoundedUp(initialRemainingMs)} 分钟",
            collectedCount,
            "",
        )
        var lastProgressMinute = minutesRoundedUp(initialRemainingMs)
        while (!stopRequested) {
            val remainingMs = batchCooldownUntilMs - System.currentTimeMillis()
            if (remainingMs <= 0L) break
            delay(minOf(remainingMs, 30_000L))
            val remainingMinute = minutesRoundedUp(
                batchCooldownUntilMs - System.currentTimeMillis(),
            )
            if (remainingMinute <= 1 || lastProgressMinute - remainingMinute >= 5) {
                lastProgressMinute = remainingMinute
                emit(
                    CollectionState.PAUSED,
                    "批次限速暂停中，剩余约 $remainingMinute 分钟",
                    collectedCount,
                    "",
                )
            }
        }
        if (!stopRequested) {
            resetBatchCycle()
            emit(
                CollectionState.SCANNING_RESULTS,
                "批次限速暂停结束，继续领取和采集任务",
                collectedCount,
                "",
            )
        }
    }

    private suspend fun awaitDetailEntryPermit(stationName: String): Boolean {
        val hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY)
        val delayRange = CollectionPacingPolicy.delayRangeForHour(hour)
        val cadenceDelayMs = randomLongInclusive(delayRange.minMs, delayRange.maxMs)
        emit(
            CollectionState.PAUSED,
            "${dayPeriodLabel(hour)}访问节奏：详情打开前停留 ${cadenceDelayMs / 1000} 秒",
            collectedCount,
            stationName,
        )
        if (!delayWhileRunning(cadenceDelayMs)) return false

        while (!stopRequested) {
            val nowMs = System.currentTimeMillis()
            replaceDetailEntryHistory(
                CollectionPacingPolicy.pruneDetailEntries(detailEntryTimes, nowMs),
            )
            val requiredDelayMs = CollectionPacingPolicy.requiredDetailDelayMs(
                timestamps = detailEntryTimes,
                nowMs = nowMs,
            )
            if (requiredDelayMs <= 0L) break
            emit(
                CollectionState.PAUSED,
                "详情访问窗口已达安全上限，等待 ${secondsRoundedUp(requiredDelayMs)} 秒",
                collectedCount,
                stationName,
            )
            if (!delayWhileRunning(requiredDelayMs)) return false
        }
        if (stopRequested) return false

        detailEntryTimes.addLast(System.currentTimeMillis())
        persistPacingState()
        return true
    }

    private fun recordStationCompletedForPacing(stationName: String) {
        batchCompletedCount += 1
        if (batchCooldownUntilMs <= 0L && batchCompletedCount >= batchTarget) {
            val cooldownMs = randomLongInclusive(batchCooldownMinMs, batchCooldownMaxMs)
            batchCooldownUntilMs = System.currentTimeMillis() + cooldownMs
            emit(
                CollectionState.PAUSED,
                "本批次已采集 $batchCompletedCount 个站点，当前站点收尾后暂停 " +
                    "${minutesRoundedUp(cooldownMs)} 分钟",
                collectedCount,
                stationName,
            )
        }
        persistPacingState()
    }

    private suspend fun delayWhileRunning(durationMs: Long): Boolean {
        var remainingMs = durationMs.coerceAtLeast(0L)
        while (remainingMs > 0L) {
            if (stopRequested) return false
            val stepMs = minOf(remainingMs, 1_000L)
            delay(stepMs)
            remainingMs -= stepMs
        }
        return !stopRequested
    }

    private fun resetBatchCycle() {
        batchCompletedCount = 0
        batchTarget = Random.nextInt(batchTargetMin, batchTargetMax + 1)
        batchCooldownUntilMs = 0L
        persistPacingState()
    }

    private fun persistPacingState(nowMs: Long = System.currentTimeMillis()) {
        replaceDetailEntryHistory(
            CollectionPacingPolicy.pruneDetailEntries(detailEntryTimes, nowMs),
        )
        AppPreferences.savePacingState(
            context = context,
            detailEntryTimes = detailEntryTimes,
            batchCompletedCount = batchCompletedCount,
            batchTarget = batchTarget,
            cooldownUntilMs = batchCooldownUntilMs,
        )
    }

    private fun replaceDetailEntryHistory(entries: Collection<Long>) {
        detailEntryTimes.clear()
        detailEntryTimes.addAll(entries)
    }

    private fun randomLongInclusive(minMs: Long, maxMs: Long): Long =
        if (maxMs <= minMs) minMs else Random.nextLong(minMs, maxMs + 1L)

    private fun dayPeriodLabel(hour: Int): String = when (hour) {
        in 6..11 -> "上午"
        in 12..17 -> "下午"
        in 18..22 -> "晚间"
        else -> "夜间"
    }

    private fun minutesRoundedUp(durationMs: Long): Long =
        (durationMs.coerceAtLeast(0L) + 59_999L) / 60_000L

    private fun secondsRoundedUp(durationMs: Long): Long =
        (durationMs.coerceAtLeast(0L) + 999L) / 1_000L

    private suspend fun humanDelay(baseMs: Long, maxMs: Long = (baseMs * 1.25).toLong()) {
        val span = (maxMs - baseMs).coerceAtLeast(50L)
        delay(baseMs + Random.nextLong(0, span + 1))
    }

    private fun hasRiskPhrase(root: NodeSnapshot): Boolean {
        val pageText = collectPageText(root)
        return riskPhrases.any { it in pageText }
    }

    private suspend fun handleRiskPause(root: NodeSnapshot) {
        val pageText = collectPageText(root)
        val phrase = riskPhrases.firstOrNull { it in pageText } ?: "操作过于频繁"
        emit(
            CollectionState.ERROR,
            "检测到风控提示($phrase)，暂停采集 ${riskCooldownMs / 1000} 秒",
            collectedCount,
            "",
        )
        AppPreferences.appendLog(context, "风控暂停: $phrase")
        var elapsed = 0L
        while (elapsed < riskCooldownMs) {
            if (stopRequested) break
            val step = 30_000L
            delay(step)
            elapsed += step
            emit(
                CollectionState.ERROR,
                "风控冷却中，剩余 ${(riskCooldownMs - elapsed).coerceAtLeast(0L) / 1000} 秒",
                collectedCount,
                "",
            )
        }
        if (!stopRequested) {
            stopRequested = true
            emit(CollectionState.STOPPED, "风控冷却结束，停止本次采集", collectedCount, "")
        }
    }


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
