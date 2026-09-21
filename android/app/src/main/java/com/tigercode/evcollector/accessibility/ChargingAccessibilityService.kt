package com.tigercode.evcollector.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.util.Log
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.regex.Pattern
import kotlin.random.Random
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.PageKind
import com.tigercode.evcollector.core.model.Rect as SnapshotRect
import com.tigercode.evcollector.core.model.StationCandidate
import com.tigercode.evcollector.core.parser.PageAssessor

class ChargingAccessibilityService : AccessibilityService() {
    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile
    private var latestRoot: NodeSnapshot? = null

    private var lastSnapshotAt = 0L
    private var snapshotRefreshPending = false
    private var lastPageSummary = ""
    private var lastPageSummaryAt = 0L

    @Volatile
    private var windowFallbackActive = false

    private var lastNonAmapWarnAt = 0L
    private val snapshotRefreshRunnable = Runnable {
        snapshotRefreshPending = false
        refreshSnapshot()
    }

    @Volatile
    var foregroundPackage: String? = null
        private set

    companion object {
        const val AMAP_PACKAGE = "com.autonavi.minimap"
        private const val SNAPSHOT_INTERVAL_MS = 220L
        private const val PAGE_LOG_MIN_INTERVAL_MS = 1_500L
        private const val PAGE_LOG_REPEAT_INTERVAL_MS = 10_000L
        private const val PAGE_LOG_ITEM_LIMIT = 18
        private const val PAGE_LOG_ITEM_MAX_LENGTH = 80
        private const val NON_AMAP_WARN_INTERVAL_MS = 5_000L

        @Volatile
        private var instance: ChargingAccessibilityService? = null

        fun current(): ChargingAccessibilityService? = instance

        fun isConnected(): Boolean = instance != null

        fun isEnabled(context: Context): Boolean {
            val expected = ComponentName(
                context,
                ChargingAccessibilityService::class.java,
            )
            val enabledServices = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            ).orEmpty()
            return enabledServices
                .split(':')
                .mapNotNull(ComponentName::unflattenFromString)
                .any { it == expected }
        }
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i("EvCollector", "采集助手无障碍服务已连接")
        refreshSnapshot()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        event.packageName?.let { foregroundPackage = it.toString() }
        scheduleSnapshotRefresh()
    }

    override fun onInterrupt() {
        // Keep the latest snapshot; the collector pauses on the next cycle.
    }

    override fun onUnbind(intent: Intent?): Boolean {
        mainHandler.removeCallbacks(snapshotRefreshRunnable)
        snapshotRefreshPending = false
        instance = null
        latestRoot = null
        Log.w("EvCollector", "采集助手无障碍服务已解绑")
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(snapshotRefreshRunnable)
        snapshotRefreshPending = false
        instance = null
        latestRoot = null
        Log.w("EvCollector", "采集助手无障碍服务已销毁")
        super.onDestroy()
    }

    fun snapshot(): NodeSnapshot? = if (isAmapWindowActive()) latestRoot else null

    fun freshSnapshot(): NodeSnapshot? {
        val root = amapRoot() ?: return null
        return try {
            NodeSnapshotReader.toSnapshot(root)
        } finally {
            root.recycle()
        }
    }

    fun freshPageKind(expectedStation: String? = null): PageKind {
        val snapshot = freshSnapshot() ?: return PageKind.UNKNOWN
        return PageAssessor.assess(snapshot, expectedStation).kind
    }

    fun clickBackButton(): Boolean {
        val root = amapRoot() ?: return false
        try {
            val node = findNodeByTextOrDescription(root, "返回") ?: return false
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val centerX = bounds.centerX()
            val centerY = bounds.centerY()
            val clickedByGesture = !bounds.isEmpty && clickAt(centerX, centerY)
            val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
            Log.i(
                "EvCollector",
                "返回按钮 bounds=${bounds.toShortString()} 坐标点击($centerX,$centerY)=$clickedByGesture 动作点击=$clickedByAction",
            )
            node.recycle()
            return clickedByGesture || clickedByAction
        } finally {
            root.recycle()
        }
    }

    fun clickAt(x: Int, y: Int): Boolean {
        if (!isAmapWindowActive()) {
            Log.w("EvCollector", "忽略非高德页面坐标点击($x,$y)")
            return false
        }
        val jitteredX = x + Random.nextInt(-7, 8)
        val jitteredY = y + Random.nextInt(-7, 8)
        val path = Path().apply {
            moveTo(jitteredX.toFloat(), jitteredY.toFloat())
            lineTo(jitteredX.toFloat(), jitteredY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, Random.nextLong(70, 130))
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        val dispatched = dispatchGesture(gesture, null, null)
        Log.i("EvCollector", "Coordinate click($jitteredX,$jitteredY)=$dispatched")
        return dispatched
    }

    fun clickStationCard(name: String): Boolean {
        val root = amapRoot() ?: return false
        try {
            val node = findNodeByTextOrDescription(root, name) ?: return false
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val centerX = bounds.centerX()
            val centerY = bounds.centerY()
            val clickedByGesture = !bounds.isEmpty && clickAt(centerX, centerY)
            val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
            Log.i(
                "EvCollector",
                "Station card[$name] bounds=${bounds.toShortString()} actionClick=$clickedByAction coordinateClick=$clickedByGesture",
            )
            node.recycle()
            return clickedByAction || clickedByGesture
        } finally {
            root.recycle()
        }
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long) {
        if (!isAmapWindowActive()) return
        val path = Path().apply {
            moveTo((x1 + Random.nextInt(-8, 9)).toFloat(), (y1 + Random.nextInt(-8, 9)).toFloat())
            lineTo((x2 + Random.nextInt(-8, 9)).toFloat(), (y2 + Random.nextInt(-8, 9)).toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, null, null)
    }

    fun globalBack() {
        if (!isAmapWindowActive()) return
        performGlobalAction(GLOBAL_ACTION_BACK)
    }

    /**
     * Focus the actual, enabled search EditText.
     *
     * AMap exposes a disabled/placeholder EditText before the real input in
     * some versions. Picking the first EditText therefore sometimes leaves
     * the old query focused and the following input is appended to it.
     */
    fun focusSearchInput(): Boolean {
        val root = amapRoot() ?: return false
        try {
            val target = findSearchInput(root) ?: return false
            val bounds = android.graphics.Rect()
            target.getBoundsInScreen(bounds)
            val focusedByAction = target.isFocused ||
                target.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            val clickedByGesture = !bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())
            val clickedByAction = !clickedByGesture && clickNodeOrAncestor(target)
            Log.i(
                "EvCollector",
                "搜索输入框 bounds=${bounds.toShortString()} " +
                    "focus=$focusedByAction 坐标点击=$clickedByGesture 动作点击=$clickedByAction",
            )
            return focusedByAction || clickedByGesture || clickedByAction
        } finally {
            root.recycle()
        }
    }

    /** Replace the complete value instead of relying on the IME cursor state. */
    fun setFocusedText(text: String): Boolean {
        val root = amapRoot() ?: return false
        try {
            val target = findSearchInput(root) ?: findFocusedNode(root)
            if (target != null) {
                if (!target.isFocused) {
                    target.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
                }
                // Clear first. AMap's exposed input occasionally keeps the
                // previous composing text even when ACTION_SET_TEXT returns
                // true, which is how suffixes such as "%%%%" can remain.
                val clearArguments = Bundle().apply {
                    putCharSequence(
                        AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                        "",
                    )
                }
                target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, clearArguments)
                val setArguments = Bundle().apply {
                    putCharSequence(
                        AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                        text,
                    )
                }
                val updated = target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, setArguments)
                Log.i(
                    "EvCollector",
                    "写入搜索词 success=$updated expectedLength=${text.length}",
                )
                target.recycle()
                return updated
            }
        } finally {
            root.recycle()
        }
        return false
    }

    private fun findSearchInput(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val isCandidate = node.className?.toString() == "android.widget.EditText" &&
            node.isEnabled &&
            node.isVisibleToUser
        if (isCandidate) return node
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val match = findSearchInput(child)
            if (match != null) return match
            child.recycle()
        }
        return null
    }

    private fun findNodeByClass(node: AccessibilityNodeInfo, className: String): AccessibilityNodeInfo? {
        if (node.className?.toString() == className) return node
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val match = findNodeByClass(child, className)
            if (match != null) return match
            child.recycle()
        }
        return null
    }

    fun clickTopSearchBar(): Boolean {
        val root = amapRoot() ?: return false
        try {
            fun visit(node: AccessibilityNodeInfo): Boolean {
                val bounds = android.graphics.Rect()
                node.getBoundsInScreen(bounds)
                val width = bounds.width()
                val isTopSearchContainer = node.isClickable &&
                    bounds.top in 60..280 &&
                    bounds.bottom <= 320 &&
                    bounds.left <= 220 &&
                    bounds.right >= resources.displayMetrics.widthPixels - 220 &&
                    width >= resources.displayMetrics.widthPixels * 0.55
                if (isTopSearchContainer) {
                    val clickedByGesture = !bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())
                    val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
                    Log.i(
                        "EvCollector",
                        "顶部搜索框 bounds=${bounds.toShortString()} " +
                            "坐标点击=$clickedByGesture actionClick=$clickedByAction",
                    )
                    return clickedByGesture || clickedByAction
                }
                for (index in 0 until node.childCount) {
                    val child = node.getChild(index) ?: continue
                    try {
                        if (visit(child)) return true
                    } finally {
                        child.recycle()
                    }
                }
                return false
            }
            return visit(root)
        } finally {
            root.recycle()
        }
    }

    fun clickNodeByText(text: String): Boolean {
        val root = amapRoot() ?: return false
        try {
            val nodes = root.findAccessibilityNodeInfosByText(text)
            for (node in nodes) {
                if (clickNodeOrAncestor(node)) {
                    node.recycle()
                    return true
                }
                val bounds = android.graphics.Rect()
                node.getBoundsInScreen(bounds)
                if (!bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())) {
                    node.recycle()
                    return true
                }
                node.recycle()
            }
        } finally {
            root.recycle()
        }
        return false
    }

    /**
     * Detect the AMap promotional overlay close button without relying on a
     * fixed screen coordinate. The overlay exposes a clickable node whose
     * content description is "关闭". AMap also exposes a similarly named
     * clear/close control in the search bar, so that control is explicitly
     * excluded by its top-right search-chrome bounds. The charging-pile
     * dialog is excluded as well; its close button must only be handled by
     * the pile-dialog flow after all pile data has been read.
     */
    fun hasAmapAdCloseButton(): Boolean {
        val root = amapRoot() ?: return false
        return try {
            if (containsNodeText(root, "电桩详情")) return false
            val node = findAmapAdCloseNode(root)
            node?.recycle()
            node != null
        } finally {
            root.recycle()
        }
    }

    /**
     * Close the supported AMap promotional overlay, if it is present.
     * Semantic ACTION_CLICK is preferred; a coordinate gesture is only a
     * fallback for AMap builds where the accessibility action is exposed but
     * ignored. This method deliberately does not press the search-box clear
     * icon and does not close the charging-pile details dialog.
     */
    fun closeAmapAdIfPresent(): Boolean {
        val root = amapRoot() ?: return false
        try {
            if (containsNodeText(root, "电桩详情")) return false
            val node = findAmapAdCloseNode(root) ?: return false
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            var actionClicked = false
            if (node.isEnabled) {
                actionClicked = node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                if (actionClicked) {
                    node.parent?.let { parent ->
                        parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        parent.recycle()
                    }
                }
            }
            // AMap coupon overlay can accept ACTION_CLICK without actually
            // handling it. Always follow up with a coordinate gesture at the
            // close control; the old "only if semantic action failed" policy
            // left this exact overlay visible.
            val coordinateClicked = !bounds.isEmpty &&
                clickAt(bounds.centerX(), bounds.centerY())
            val clicked = actionClicked || coordinateClicked
            Log.i(
                "EvCollector",
                "检测到高德广告关闭按钮 bounds=${bounds.toShortString()} " +
                    "动作点击=$actionClicked 坐标兜底=$coordinateClicked",
            )
            node.recycle()
            return clicked
        } finally {
            root.recycle()
        }
    }

    fun clickSearchSubmitButton(): Boolean {
        val root = amapRoot() ?: return false
        try {
            val screenWidth = resources.displayMetrics.widthPixels

            fun visit(node: AccessibilityNodeInfo): Boolean {
                val label = node.text?.toString()?.trim().orEmpty()
                val description = node.contentDescription?.toString()?.trim().orEmpty()
                val bounds = android.graphics.Rect()
                node.getBoundsInScreen(bounds)
                val isTopRightSearchButton =
                    (label == "搜索" || description == "搜索") &&
                        !bounds.isEmpty &&
                        bounds.top in 60..380 &&
                        bounds.centerX() >= (screenWidth * 0.65f).toInt()
                if (isTopRightSearchButton) {
                    val clickedByGesture = clickAt(bounds.centerX(), bounds.centerY())
                    val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
                    Log.i(
                        "EvCollector",
                        "搜索提交按钮 bounds=${bounds.toShortString()} " +
                            "坐标点击=$clickedByGesture 动作点击=$clickedByAction",
                    )
                    return clickedByGesture || clickedByAction
                }
                for (index in 0 until node.childCount) {
                    val child = node.getChild(index) ?: continue
                    try {
                        if (visit(child)) return true
                    } finally {
                        child.recycle()
                    }
                }
                return false
            }

            return visit(root)
        } finally {
            root.recycle()
        }
    }

    fun findNodeBounds(patternText: String): Rect? {
        val pattern = runCatching { Pattern.compile(patternText) }.getOrNull() ?: return null
        val root = amapRoot() ?: return null
        try {
            val node = findNodeByRegex(root, pattern) ?: return null
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            node.recycle()
            return bounds
        } finally {
            root.recycle()
        }
    }
    fun findPriceEntryBounds(): Rect? {
        val pattern = Pattern.compile("涨至|降至")
        val root = amapRoot() ?: return null
        return try {
            val matches = mutableListOf<Pair<String, Rect>>()
            collectMatchingBounds(root, pattern, matches)
            selectPriceEntryMatch(matches, requireSafeArea = false)?.second?.let(::Rect)
        } finally {
            root.recycle()
        }
    }

    fun isPriceEntrySafelyVisible(bounds: Rect): Boolean {
        if (bounds.isEmpty) return false
        val screenHeight = resources.displayMetrics.heightPixels
        val safeTop = maxOf(180, (screenHeight * 0.08f).toInt())
        val safeBottom = screenHeight - maxOf(460, (screenHeight * 0.20f).toInt())
        return bounds.top >= safeTop && bounds.bottom <= safeBottom
    }

    fun clickPriceEntry(): Boolean {
        val pattern = Pattern.compile("涨至|降至")
        val root = amapRoot() ?: return false
        val match = try {
            val matches = mutableListOf<Pair<String, Rect>>()
            collectMatchingBounds(root, pattern, matches)
            selectPriceEntryMatch(matches, requireSafeArea = true)
        } finally {
            root.recycle()
        }
        if (match == null) {
            Log.i("EvCollector", "涨至/降至入口尚未进入安全点击区域")
            return false
        }
        val (text, bounds) = match
        val clicked = clickAt(bounds.centerX(), bounds.centerY())
        Log.i(
            "EvCollector",
            "安全点击价格入口[$text] bounds=${bounds.toShortString()} clicked=$clicked",
        )
        return clicked
    }

    fun findChargingPileHistoryEntryBounds(): Rect? {
        val pattern = Pattern.compile("查看历史空闲")
        val root = amapRoot() ?: return null
        return try {
            val matches = mutableListOf<Pair<String, Rect>>()
            collectMatchingBounds(root, pattern, matches)
            matches
                .map { it.second }
                .filter(::isChargingPileEntrySafelyVisible)
                .maxByOrNull { it.width() * it.height() }
                ?.let(::Rect)
        } finally {
            root.recycle()
        }
    }

    fun clickChargingPileHistoryEntry(): Boolean {
        val bounds = findChargingPileHistoryEntryBounds() ?: return false
        val clicked = clickAt(bounds.centerX(), bounds.centerY())
        Log.i(
            "EvCollector",
            "安全点击查看历史空闲 bounds=${bounds.toShortString()} clicked=$clicked",
        )
        return clicked
    }

    fun scrollChargingPileDialog(): Boolean {
        val root = snapshot() ?: return false
        if (!containsSnapshotText(root, "电桩详情")) {
            Log.w("EvCollector", "电桩详情弹窗未确认，取消弹窗滑动")
            return false
        }
        return gestureScroll(0.82f, 0.34f, 420)
    }

    fun closeChargingPileDialog(): Boolean {
        val root = amapRoot() ?: return false
        val titleBounds = try {
            val title = findNodeByTextOrDescription(root, "电桩详情") ?: return false
            val bounds = Rect()
            title.getBoundsInScreen(bounds)
            title.recycle()
            bounds
        } finally {
            root.recycle()
        }
        if (titleBounds.isEmpty) return false

        val screenWidth = resources.displayMetrics.widthPixels
        val closeX = (screenWidth - maxOf(64, (screenWidth * 0.07f).toInt()))
            .coerceIn(1, screenWidth - 1)
        val closeY = titleBounds.centerY().coerceAtLeast(1)
        val clicked = clickAt(closeX, closeY)
        Log.i(
            "EvCollector",
            "关闭电桩详情弹窗 title=${titleBounds.toShortString()} " +
                "坐标点击($closeX,$closeY)=$clicked",
        )
        return clicked
    }

    fun clickNodeByRegex(patternText: String): Boolean {
        val pattern = runCatching { Pattern.compile(patternText) }.getOrNull() ?: return false
        val root = amapRoot() ?: return false
        try {
            val node = findNodeByRegex(root, pattern)
            if (node == null) {
                val matches = mutableListOf<String>()
                collectMatchingTexts(root, Regex("涨至|降至"), matches)
                Log.i("EvCollector", "未找到节点[$patternText]，现有价格文本: ${matches.take(8).joinToString(" | ")}")
                return false
            }
            val nodeText = node.text?.toString().orEmpty()
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val centerX = bounds.centerX()
            val centerY = bounds.centerY()
            val screenHeight = resources.displayMetrics.heightPixels
            if (!bounds.isEmpty && (centerY <= 80 || centerY >= screenHeight - 160)) {
                Log.i("EvCollector", "节点[$nodeText] 在屏幕外 centerY=$centerY 不点击")
                node.recycle()
                return false
            }
            val clickedByGesture = !bounds.isEmpty && clickAt(centerX, centerY)
            Log.i(
                "EvCollector",
                "节点[$nodeText] bounds=${bounds.toShortString()} 坐标点击($centerX,$centerY)=$clickedByGesture 可点祖先:${hasClickableAncestor(node)}",
            )
            val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
            if (clickedByAction) {
                Log.i("EvCollector", "回退 ACTION_CLICK 成功")
            }
            node.recycle()
            return clickedByGesture || clickedByAction
        } finally {
            root.recycle()
        }
    }

    fun clickNodeByViewId(viewId: String): Boolean {
        val root = amapRoot() ?: return false
        try {
            val nodes = root.findAccessibilityNodeInfosByViewId(viewId) ?: return false
            for (node in nodes) {
                val bounds = android.graphics.Rect()
                node.getBoundsInScreen(bounds)
                val clickedByGesture = !bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())
                val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
                if (clickedByGesture || clickedByAction) {
                    node.recycle()
                    return true
                }
                node.recycle()
            }
        } finally {
            root.recycle()
        }
        return false
    }

    fun clickNodeByClass(className: String): Boolean {
        val root = amapRoot() ?: return false
        try {
            fun find(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
                if (node.className?.toString() == className) return node
                for (index in 0 until node.childCount) {
                    val child = node.getChild(index) ?: continue
                    val match = find(child)
                    if (match != null) return match
                    child.recycle()
                }
                return null
            }
            val node = find(root) ?: return false
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val clickedByGesture = !bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())
            val clickedByAction = !clickedByGesture && clickNodeOrAncestor(node)
            node.recycle()
            return clickedByGesture || clickedByAction
        } finally {
            root.recycle()
        }
    }

    private fun hasClickableAncestor(node: AccessibilityNodeInfo): Boolean {
        var parent = node.parent
        while (parent != null) {
            val clickable = parent.isClickable
            parent = parent.parent
            if (clickable) return true
        }
        return false
    }

    private fun collectMatchingBounds(
        node: AccessibilityNodeInfo,
        pattern: Pattern,
        output: MutableList<Pair<String, Rect>>,
    ) {
        val text = node.text?.toString().orEmpty()
        val description = node.contentDescription?.toString().orEmpty()
        val matchedText = when {
            pattern.matcher(text).find() -> text
            pattern.matcher(description).find() -> description
            else -> ""
        }
        if (matchedText.isNotEmpty()) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            if (!bounds.isEmpty) output.add(matchedText to bounds)
        }
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                collectMatchingBounds(child, pattern, output)
            } finally {
                child.recycle()
            }
        }
    }

    private fun selectPriceEntryMatch(
        matches: List<Pair<String, Rect>>,
        requireSafeArea: Boolean,
    ): Pair<String, Rect>? {
        val candidates = if (requireSafeArea) {
            matches.filter { isPriceEntrySafelyVisible(it.second) }
        } else {
            matches
        }
        val screenCenterY = resources.displayMetrics.heightPixels / 2
        return candidates.maxByOrNull { (text, bounds) ->
            val safeAreaScore = if (isPriceEntrySafelyVisible(bounds)) 1_000_000 else 0
            val entryTextScore = if (Regex("起(涨至|降至)").containsMatchIn(text)) 100_000 else 0
            safeAreaScore + entryTextScore - kotlin.math.abs(bounds.centerY() - screenCenterY)
        }
    }

    private fun collectMatchingTexts(
        node: AccessibilityNodeInfo,
        pattern: Regex,
        output: MutableList<String>,
    ) {
        node.text?.let { if (pattern.containsMatchIn(it)) output.add(it.toString()) }
        node.contentDescription?.let { if (pattern.containsMatchIn(it)) output.add(it.toString()) }
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            collectMatchingTexts(child, pattern, output)
        }
    }

    private fun findNodeByRegex(node: AccessibilityNodeInfo, pattern: Pattern): AccessibilityNodeInfo? {
        node.text?.takeIf { pattern.matcher(it).find() }?.let { return node }
        node.contentDescription?.takeIf { pattern.matcher(it).find() }?.let { return node }
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val match = findNodeByRegex(child, pattern)
            if (match != null) return match
        }
        return null
    }

    private fun containsNodeText(node: AccessibilityNodeInfo, expected: String): Boolean {
        if (node.text?.toString()?.trim() == expected ||
            node.contentDescription?.toString()?.trim() == expected
        ) return true
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                if (containsNodeText(child, expected)) return true
            } finally {
                child.recycle()
            }
        }
        return false
    }

    private fun findAmapAdCloseNode(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels

        fun isSearchChromeClose(bounds: android.graphics.Rect): Boolean =
            bounds.top <= (screenHeight * 0.18f).toInt() &&
                bounds.right >= (screenWidth * 0.80f).toInt()

        fun hasPopupAncestor(ancestors: List<android.graphics.Rect>): Boolean =
            ancestors.any { bounds ->
                bounds.isEmpty.not() &&
                    bounds.top >= (screenHeight * 0.20f).toInt() &&
                    bounds.width() >= (screenWidth * 0.55f).toInt() &&
                    bounds.height() >= (screenHeight * 0.30f).toInt()
            }

        fun visit(
            node: AccessibilityNodeInfo,
            ancestors: List<android.graphics.Rect>,
        ): AccessibilityNodeInfo? {
            val description = node.contentDescription?.toString()?.trim().orEmpty()
            val text = node.text?.toString()?.trim().orEmpty()
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val isCloseLabel = description == "关闭" || text == "关闭"
            val isAdClose = isCloseLabel && node.isClickable && node.isEnabled &&
                !bounds.isEmpty && !isSearchChromeClose(bounds) &&
                (hasPopupAncestor(ancestors) ||
                    bounds.top >= (screenHeight * 0.35f).toInt())
            if (isAdClose) return node

            val nextAncestors = ancestors + bounds
            for (index in 0 until node.childCount) {
                val child = node.getChild(index) ?: continue
                val result = visit(child, nextAncestors)
                if (result != null) return result
                child.recycle()
            }
            return null
        }

        return visit(root, emptyList())
    }

    private fun findNodeByTextOrDescription(
        node: AccessibilityNodeInfo,
        text: String,
    ): AccessibilityNodeInfo? {
        if (node.text?.toString() == text || node.contentDescription?.toString() == text) {
            return node
        }
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val match = findNodeByTextOrDescription(child, text)
            if (match != null) return match
            child.recycle()
        }
        return null
    }
    fun openAmapSearch(keyword: String) {
        val uri = Uri.Builder()
            .scheme("amapuri")
            .authority("poi")
            .appendPath("search")
            .appendQueryParameter("sourceApplication", packageName)
            .appendQueryParameter("keywords", keyword)
            .build()
        startActivity(
            Intent(Intent.ACTION_VIEW, uri)
                .setPackage(AMAP_PACKAGE)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP
                )
        )
    }

    fun openPoiDetail(station: StationCandidate) {
        val uriBuilder = Uri.Builder()
            .scheme("amapuri")
            .authority("poi")
            .appendPath("detail")
            .appendQueryParameter(
                "poiname",
                station.name,
            )
            .appendQueryParameter("poiid", station.id)
        station.latitude?.let { uriBuilder.appendQueryParameter("lat", it.toString()) }
        station.longitude?.let { uriBuilder.appendQueryParameter("lon", it.toString()) }
        startActivity(
            Intent(Intent.ACTION_VIEW, uriBuilder.build())
                .setPackage(AMAP_PACKAGE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun launchAmap(): Boolean {
        val label = runCatching {
            val info = packageManager.getApplicationInfo(AMAP_PACKAGE, 0)
            packageManager.getApplicationLabel(info).toString()
        }.getOrDefault("高德地图")
        return launchApp(AMAP_PACKAGE, label)
    }

    fun launchCollectorApp(): Boolean {
        val label = runCatching {
            val info = packageManager.getApplicationInfo(packageName, 0)
            packageManager.getApplicationLabel(info).toString()
        }.getOrDefault("采集终端")
        return launchApp(packageName, label)
    }

    private fun launchApp(targetPackage: String, label: String): Boolean {
        val launchIntent = packageManager.getLaunchIntentForPackage(targetPackage)
        if (launchIntent != null) {
            try {
                startActivity(
                    launchIntent.addFlags(
                        Intent.FLAG_ACTIVITY_NEW_TASK or
                            Intent.FLAG_ACTIVITY_SINGLE_TOP or
                            Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                    )
                )
                if (waitForForegroundPackage(targetPackage, 1_200L)) return true
            } catch (_: Exception) {
                // Android 15 may reject a background activity launch; use recents instead.
            }
        }
        return launchAppFromRecents(targetPackage, label)
    }

    private fun launchAppFromRecents(targetPackage: String, label: String): Boolean {
        if (!performGlobalAction(GLOBAL_ACTION_RECENTS)) return false
        val deadline = SystemClock.uptimeMillis() + 3_000L
        while (SystemClock.uptimeMillis() < deadline) {
            val root = rootInActiveWindow
            if (root != null) {
                val clicked = clickRecentAppNode(root, targetPackage, label)
                root.recycle()
                if (clicked && waitForForegroundPackage(targetPackage, 2_000L)) return true
            }
            SystemClock.sleep(180L)
        }
        performGlobalAction(GLOBAL_ACTION_BACK)
        return false
    }

    private fun clickRecentAppNode(
        root: AccessibilityNodeInfo,
        targetPackage: String,
        label: String,
    ): Boolean {
        val nodePackage = root.packageName?.toString().orEmpty()
        val nodeText = root.text?.toString().orEmpty()
        val nodeDescription = root.contentDescription?.toString().orEmpty()
        val matches = nodePackage == targetPackage ||
            nodeText == label || nodeDescription == label ||
            nodeText.contains(label) || nodeDescription.contains(label)
        if (matches) {
            var candidate: AccessibilityNodeInfo? = root
            repeat(4) {
                val current = candidate ?: return@repeat
                if (current.isClickable && current.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true
                }
                val bounds = Rect()
                current.getBoundsInScreen(bounds)
                if (!bounds.isEmpty && clickAt(bounds.centerX(), bounds.centerY())) {
                    return true
                }
                candidate = current.parent
            }
        }
        for (index in 0 until root.childCount) {
            val child = root.getChild(index) ?: continue
            try {
                if (clickRecentAppNode(child, targetPackage, label)) return true
            } finally {
                child.recycle()
            }
        }
        return false
    }

    private fun waitForForegroundPackage(targetPackage: String, timeoutMs: Long): Boolean {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            val root = rootInActiveWindow
            val packageName = root?.packageName?.toString()
            root?.recycle()
            if (packageName == targetPackage) return true
            SystemClock.sleep(100L)
        }
        return false
    }

    fun scrollForward(): Boolean = gestureScroll(0.83f, 0.27f, 420)

    /**
     * Swipe inside a result-list viewport.
     *
     * When distancePx is supplied, a positive value means a downward list
     * scroll (finger moves upward) and a negative value is a small correction
     * upward (finger moves downward). Keeping the gesture inside the actual
     * RecyclerView prevents the search IME or other screen controls from
     * receiving the swipe.
     */
    fun scrollWithin(bounds: SnapshotRect, distancePx: Int? = null): Boolean {
        if (!isAmapWindowActive()) return false
        if (!bounds.isValid) return scrollForward()
        val margin = minOf(180, maxOf(100, bounds.height / 8))
        val top = (bounds.top + margin).coerceAtLeast(0)
        val bottom = (bounds.bottom - margin).coerceAtMost(screenHeightPx() - 1)
        if (bottom <= top + 80) return scrollForward()

        val downward = distancePx == null || distancePx >= 0
        val maxDistance = (bottom - top - 80).coerceAtLeast(120)
        val distance = (distancePx?.let { kotlin.math.abs(it) } ?: maxDistance)
            .coerceIn(120, maxDistance)
        val startY = if (downward) bottom else top
        val endY = when {
            // Keep the legacy full-viewport behavior for target-search flows
            // that do not request a bounded step.
            distancePx == null -> top
            downward -> (startY - distance).coerceAtLeast(top)
            else -> (startY + distance).coerceAtMost(bottom)
        }
        if (kotlin.math.abs(startY - endY) < 80) return false

        val x = bounds.centerX.coerceIn(40, resources.displayMetrics.widthPixels - 40)
        val path = Path().apply {
            moveTo(x.toFloat(), startY.toFloat())
            lineTo(x.toFloat(), endY.toFloat())
        }
        // A fixed duration plus a full-height distance can be interpreted as
        // a fling on some high-density/high-refresh devices. The bounded list
        // step is short, so use a slightly longer duration to avoid momentum.
        val durationMs = if (distancePx == null) 420L else 560L
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        val dispatched = dispatchGesture(gesture, null, null)
        Log.i(
            "EvCollector",
            "列表区域滑动 bounds=$bounds distance=${if (downward) distance else -distance} " +
                "起点=($x,$startY) 终点=($x,$endY) duration=${durationMs}ms dispatched=$dispatched",
        )
        return dispatched
    }

    fun scrollSmallDown(): Boolean = gestureScroll(0.72f, 0.46f, 320)

    fun scrollSmallUp(): Boolean = gestureScroll(0.46f, 0.72f, 320)

    fun screenHeightPx(): Int = resources.displayMetrics.heightPixels

    private fun isChargingPileEntrySafelyVisible(bounds: Rect): Boolean {
        if (bounds.isEmpty) return false
        val screenHeight = resources.displayMetrics.heightPixels
        val safeTop = maxOf(160, (screenHeight * 0.07f).toInt())
        val safeBottom = screenHeight - maxOf(300, (screenHeight * 0.13f).toInt())
        return bounds.top >= safeTop && bounds.bottom <= safeBottom
    }

    private fun containsSnapshotText(root: NodeSnapshot, expected: String): Boolean {
        if (root.text.trim() == expected || root.contentDescription.trim() == expected) return true
        return root.children.any { containsSnapshotText(it, expected) }
    }

    private fun gestureScroll(
        startFraction: Float,
        endFraction: Float,
        baseDurationMs: Long,
    ): Boolean {
        if (!isAmapWindowActive()) return false
        val metrics = resources.displayMetrics
        val startY = (metrics.heightPixels * startFraction).toInt()
        val endY = (metrics.heightPixels * endFraction).toInt()
        val startX = metrics.widthPixels / 2 + Random.nextInt(-18, 19)
        val endX = metrics.widthPixels / 2 + Random.nextInt(-18, 19)
        val durationMs = (baseDurationMs + Random.nextLong(-70, 71)).coerceAtLeast(180L)
        val path = Path().apply {
            moveTo(startX.toFloat(), startY.toFloat())
            lineTo(endX.toFloat(), endY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }

    private fun refreshSnapshot() {
        val root = amapRoot() ?: run {
            latestRoot = null
            return
        }
        try {
            val snapshot = NodeSnapshotReader.toSnapshot(root)
            latestRoot = snapshot
            lastSnapshotAt = SystemClock.uptimeMillis()
            logPageSummary(snapshot)
        } finally {
            root.recycle()
        }
    }

    /**
     * Exposes a compact page summary through logcat without starting UiAutomation.
     *
     * Do not use uiautomator dump while this service is collecting: Android temporarily
     * disconnects regular accessibility services when a UiAutomation service is registered.
     */
    private fun logPageSummary(snapshot: NodeSnapshot) {
        val now = SystemClock.uptimeMillis()
        if (now - lastPageSummaryAt < PAGE_LOG_MIN_INTERVAL_MS) return

        val values = LinkedHashSet<String>()
        fun collect(node: NodeSnapshot) {
            sequenceOf(node.text, node.contentDescription)
                .map { it.replace(Regex("\\s+"), " ").trim() }
                .filter { it.isNotEmpty() }
                .forEach { values.add(it.take(PAGE_LOG_ITEM_MAX_LENGTH)) }
            if (values.size >= PAGE_LOG_ITEM_LIMIT) return
            for (child in node.children) {
                collect(child)
                if (values.size >= PAGE_LOG_ITEM_LIMIT) return
            }
        }
        collect(snapshot)

        val summary = values.take(PAGE_LOG_ITEM_LIMIT).joinToString(" | ")
        if (summary.isEmpty()) return
        if (summary == lastPageSummary && now - lastPageSummaryAt < PAGE_LOG_REPEAT_INTERVAL_MS) {
            return
        }

        lastPageSummary = summary
        lastPageSummaryAt = now
        Log.i("EvCollectorPage", summary)
    }

    private fun isAmapWindowActive(): Boolean {
        val root = rootInActiveWindow ?: return findTopmostAmapWindowRoot() != null
        val isAmap = try {
            root.packageName?.toString() == AMAP_PACKAGE
        } finally {
            root.recycle()
        }
        return isAmap || findTopmostAmapWindowRoot() != null
    }

    private fun amapRoot(): AccessibilityNodeInfo? {
        val root = rootInActiveWindow
        if (root != null) {
            if (root.packageName?.toString() == AMAP_PACKAGE) return root
            root.recycle()
        }
        // MIUI can report a stale focus window (e.g. the launcher) while AMap
        // is visibly on top, which blinds every snapshot/gesture call. Fall
        // back to scanning the window list; the fallback only succeeds when
        // AMap owns the top-most application window, so gestures stay blocked
        // while another app is genuinely in front.
        val fallback = findTopmostAmapWindowRoot()
        if (fallback != null) {
            if (!windowFallbackActive) {
                windowFallbackActive = true
                Log.w(
                    "EvCollector",
                    "焦点窗口漂移(${root?.packageName ?: "null"})，已切换为窗口列表兜底读取高德页面",
                )
            }
            return fallback
        }
        if (windowFallbackActive) {
            windowFallbackActive = false
            Log.i("EvCollector", "高德焦点窗口已恢复，退出窗口列表兜底读取")
        }
        val now = SystemClock.uptimeMillis()
        if (now - lastNonAmapWarnAt >= NON_AMAP_WARN_INTERVAL_MS) {
            lastNonAmapWarnAt = now
            Log.w(
                "EvCollector",
                "忽略非高德页面无障碍操作: ${root?.packageName ?: "null"}",
            )
        }
        return null
    }

    /**
     * Root of the top-most AMap application window, or null when another
     * application window is stacked above AMap (another app is genuinely in
     * the foreground). Only application windows participate, so IME overlays
     * and system windows cannot fake "on top".
     */
    private fun findTopmostAmapWindowRoot(): AccessibilityNodeInfo? {
        val windows = try {
            windows
        } catch (t: Throwable) {
            null
        }
        if (windows.isNullOrEmpty()) return null
        try {
            var topAppLayer = Int.MIN_VALUE
            var topAppIsAmap = false
            var bestAmapLayer = Int.MIN_VALUE
            var bestAmapWindow: AccessibilityWindowInfo? = null
            for (window in windows) {
                if (window.type != AccessibilityWindowInfo.TYPE_APPLICATION) continue
                val root = window.root ?: continue
                val pkg = try {
                    root.packageName?.toString()
                } finally {
                    root.recycle()
                }
                val layer = window.layer
                if (layer > topAppLayer) {
                    topAppLayer = layer
                    topAppIsAmap = pkg == AMAP_PACKAGE
                }
                if (pkg == AMAP_PACKAGE && layer > bestAmapLayer) {
                    bestAmapLayer = layer
                    bestAmapWindow = window
                }
            }
            if (bestAmapWindow == null || !topAppIsAmap) return null
            return bestAmapWindow.root
        } finally {
            windows.forEach { it.recycle() }
        }
    }

    private fun scheduleSnapshotRefresh() {
        if (snapshotRefreshPending) return
        val elapsed = SystemClock.uptimeMillis() - lastSnapshotAt
        val delayMs = (SNAPSHOT_INTERVAL_MS - elapsed).coerceAtLeast(0L)
        snapshotRefreshPending = true
        mainHandler.postDelayed(snapshotRefreshRunnable, delayMs)
    }

    private fun findFocusedNode(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        return node.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
    }

    private fun findClickableAncestor(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var current = node
        var parent = current.parent
        while (parent != null) {
            val grandParent = parent.parent
            if (parent.isClickable) {
                // The caller owns and recycles the node returned by this helper.
                // Recycling it here leaves a dangling AccessibilityNodeInfo and
                // makes the subsequent click fail intermittently.
                return parent
            }
            parent.recycle()
            parent = grandParent
        }
        return null
    }

    private fun clickNodeOrAncestor(node: AccessibilityNodeInfo): Boolean {
        if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
        var parent = node.parent
        while (parent != null) {
            val grandParent = parent.parent
            val clicked = parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            parent.recycle()
            if (clicked) return true
            parent = grandParent
        }
        return false
    }

}
