package com.tigercode.evcollector.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.util.Log
import android.os.Looper
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.regex.Pattern
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

        @Volatile
        private var instance: ChargingAccessibilityService? = null

        fun current(): ChargingAccessibilityService? = instance

        fun isConnected(): Boolean = instance != null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
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
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(snapshotRefreshRunnable)
        snapshotRefreshPending = false
        instance = null
        latestRoot = null
        super.onDestroy()
    }

    fun snapshot(): NodeSnapshot? = latestRoot

    fun freshSnapshot(): NodeSnapshot? {
        val root = rootInActiveWindow ?: return null
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
        val root = rootInActiveWindow ?: return false
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
        val path = Path().apply {
            moveTo(x.toFloat(), y.toFloat())
            lineTo(x.toFloat(), y.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, 90)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        val dispatched = dispatchGesture(gesture, null, null)
        Log.i("EvCollector", "Coordinate click($x,$y)=$dispatched")
        return dispatched
    }

    fun clickStationCard(name: String): Boolean {
        val root = rootInActiveWindow ?: return false
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
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, null, null)
    }

    fun globalBack() {
        performGlobalAction(GLOBAL_ACTION_BACK)
    }

    fun setFocusedText(text: String) {
        val root = rootInActiveWindow ?: return
        try {
            val focused = findFocusedNode(root)
            if (focused != null) {
                val arguments = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
                }
                focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
                focused.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    fun clickNodeByText(text: String): Boolean {
        val root = rootInActiveWindow ?: return false
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

    fun findNodeBounds(patternText: String): Rect? {
        val pattern = runCatching { Pattern.compile(patternText) }.getOrNull() ?: return null
        val root = rootInActiveWindow ?: return null
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
    fun clickNodeByRegex(patternText: String): Boolean {
        val pattern = runCatching { Pattern.compile(patternText) }.getOrNull() ?: return false
        val root = rootInActiveWindow ?: return false
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
        val root = rootInActiveWindow ?: return false
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

    private fun hasClickableAncestor(node: AccessibilityNodeInfo): Boolean {
        var parent = node.parent
        while (parent != null) {
            val clickable = parent.isClickable
            parent = parent.parent
            if (clickable) return true
        }
        return false
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

    fun launchAmap() {
        val launchIntent = packageManager.getLaunchIntentForPackage(AMAP_PACKAGE) ?: return
        startActivity(
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun scrollForward(): Boolean = gestureScroll(0.83f, 0.27f, 420)

    fun scrollWithin(bounds: SnapshotRect): Boolean {
        if (!bounds.isValid) return scrollForward()
        val margin = minOf(180, maxOf(100, bounds.height / 8))
        val startY = (bounds.bottom - margin).coerceAtMost(screenHeightPx() - 1)
        val endY = (bounds.top + margin).coerceAtLeast(0)
        if (startY <= endY + 80) return scrollForward()

        val x = bounds.centerX.coerceIn(40, resources.displayMetrics.widthPixels - 40)
        val path = Path().apply {
            moveTo(x.toFloat(), startY.toFloat())
            lineTo(x.toFloat(), endY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, 420)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        val dispatched = dispatchGesture(gesture, null, null)
        Log.i(
            "EvCollector",
            "列表区域滑动 bounds=$bounds 起点=($x,$startY) 终点=($x,$endY) dispatched=$dispatched",
        )
        return dispatched
    }

    fun scrollSmallDown(): Boolean = gestureScroll(0.72f, 0.46f, 320)

    fun scrollSmallUp(): Boolean = gestureScroll(0.46f, 0.72f, 320)

    fun screenHeightPx(): Int = resources.displayMetrics.heightPixels

    private fun gestureScroll(
        startFraction: Float,
        endFraction: Float,
        durationMs: Long,
    ): Boolean {
        val metrics = resources.displayMetrics
        val startY = (metrics.heightPixels * startFraction).toInt()
        val endY = (metrics.heightPixels * endFraction).toInt()
        val path = Path().apply {
            moveTo((metrics.widthPixels / 2).toFloat(), startY.toFloat())
            lineTo((metrics.widthPixels / 2).toFloat(), endY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }

    private fun refreshSnapshot() {
        val root = rootInActiveWindow ?: return
        try {
            latestRoot = NodeSnapshotReader.toSnapshot(root)
            lastSnapshotAt = SystemClock.uptimeMillis()
        } finally {
            root.recycle()
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
                parent.recycle()
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

