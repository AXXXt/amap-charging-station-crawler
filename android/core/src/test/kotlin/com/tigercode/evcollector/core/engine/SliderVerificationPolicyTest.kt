package com.tigercode.evcollector.core.engine

import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.Rect
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SliderVerificationPolicyTest {
    @Test
    fun limitsAttemptsToThree() {
        assertEquals(3, SliderVerificationPolicy.MAX_ATTEMPTS)
    }

    @Test
    fun acceptsDynamicIdsAndNestedSliderWithoutText() {
        for (id in listOf("nc_1", "nc_27", "nc_1048")) {
            val root = page(track(id = id, slider = slider(id = id)))

            assertEquals(SliderVerificationPolicy.Drag(120, 225, 392, 225), SliderVerificationPolicy.drag(root))
        }
    }

    @Test
    fun acceptsTextSliderAndUsesVerticalIntersectionCenter() {
        val root = page(track(slider = slider().copy(text = "滑块", viewId = "")))

        // Slider Y is 180..250; track Y is 200..260, so the shared center is 225.
        assertEquals(SliderVerificationPolicy.Drag(120, 225, 392, 225), SliderVerificationPolicy.drag(root))
    }

    @Test
    fun acceptsEverySimplePromptIncludingWhitespaceAndDescriptions() {
        for (prompt in listOf("滑动下方滑块", "向右滑动验证", "向右拖动验证", "请点我重试", "点击框体重试")) {
            val root = NodeSnapshot(
                contentDescription = prompt.toList().joinToString("\n"),
                children = listOf(track()),
            )

            assertEquals(SliderVerificationPolicy.Drag(120, 225, 392, 225), SliderVerificationPolicy.drag(root))
        }
    }

    @Test
    fun doesNotDragWhileLoading() {
        for (message in listOf("加载中", "正在加载，请稍候", "Loading...")) {
            assertNull(SliderVerificationPolicy.drag(page(track(), prompt = "向右滑动验证 $message")))
        }
    }

    @Test
    fun excludesImageAndPuzzleChallengesEvenWithSimplePrompt() {
        for (message in listOf("请选择符合描述的所有图片", "向右拖动滑块填充拼图", "拖动滑块出现完整的一个蝴蝶后就松开")) {
            val root = page(track(), retry(), prompt = "向右滑动验证 $message")

            assertNull(SliderVerificationPolicy.drag(root))
            assertNull(SliderVerificationPolicy.retryTarget(root))
        }
    }

    @Test
    fun excludesAccountAndAccessRestrictionsEvenWithSimplePrompt() {
        for (message in listOf("账号异常", "账户异常", "访问异常", "环境异常", "已被封禁", "账号封控")) {
            val root = page(track(), retry(), prompt = "向右拖动验证 $message")

            assertNull(SliderVerificationPolicy.drag(root))
            assertNull(SliderVerificationPolicy.retryTarget(root))
        }
    }

    @Test
    fun requiresSimpleVerificationContextForBothActions() {
        for (prompt in listOf("充电站详情", "安全验证", "滑块验证", "点我反馈")) {
            val root = page(track(), retry(), prompt = prompt)

            assertNull(SliderVerificationPolicy.drag(root))
            assertNull(SliderVerificationPolicy.retryTarget(root))
        }
    }

    @Test
    fun requiresTrackAncestorRatherThanSiblingOrRootBounds() {
        assertNull(SliderVerificationPolicy.drag(page(slider())))
        assertNull(SliderVerificationPolicy.drag(page(track().copy(children = emptyList()), slider())))
    }

    @Test
    fun rejectsInvalidSliderAndTrackBounds() {
        for (bounds in listOf(Rect(), Rect(150, 200, 100, 250), Rect(100, 250, 150, 200))) {
            assertNull(SliderVerificationPolicy.drag(page(track(slider = slider().copy(bounds = bounds)))))
            assertNull(SliderVerificationPolicy.drag(page(track().copy(bounds = bounds))))
        }
    }

    @Test
    fun rejectsMissingVerticalIntersectionAndStartOutsideTrack() {
        for (bounds in listOf(
            Rect(100, 260, 140, 300),
            Rect(100, 100, 140, 200),
            Rect(0, 200, 40, 250),
            Rect(400, 200, 440, 250),
        )) {
            assertNull(SliderVerificationPolicy.drag(page(track(slider = slider().copy(bounds = bounds)))))
        }
    }

    @Test
    fun requiresEndToBeStrictlyRightOfStart() {
        for (bounds in listOf(Rect(380, 200, 404, 250), Rect(390, 200, 404, 250))) {
            assertNull(SliderVerificationPolicy.drag(page(track(slider = slider().copy(bounds = bounds)))))
        }
        val root = page(track(slider = slider().copy(bounds = Rect(380, 200, 400, 250))))
        assertEquals(SliderVerificationPolicy.Drag(390, 225, 392, 225), SliderVerificationPolicy.drag(root))
    }

    @Test
    fun clampsEndpointInsideVeryNarrowTrackWithoutDraggingLeft() {
        val root = page(track(slider = slider().copy(bounds = Rect(100, 200, 102, 240)))
            .copy(bounds = Rect(100, 200, 105, 260)))

        assertNull(SliderVerificationPolicy.drag(root))
    }

    @Test
    fun skipsDisabledSliderAndTrack() {
        assertNull(SliderVerificationPolicy.drag(page(track(slider = slider().copy(enabled = false)))))
        assertNull(SliderVerificationPolicy.drag(page(track().copy(enabled = false))))
    }

    @Test
    fun continuesSearchingAfterInvalidSlider() {
        val root = page(track(slider = slider().copy(bounds = Rect())), track(id = "nc_9"))

        assertEquals(SliderVerificationPolicy.Drag(120, 225, 392, 225), SliderVerificationPolicy.drag(root))
    }

    @Test
    fun returnsEnabledRetryBoundsAndIgnoresFeedback() {
        val expected = Rect(160, 300, 350, 350)
        val root = page(
            retry().copy(text = "点我反馈"),
            retry().copy(text = "重试问题反馈"),
            retry().copy(contentDescription = "反馈"),
            retry().copy(enabled = false),
            retry().copy(bounds = Rect()),
            retry().copy(text = "点击框体重试", bounds = expected),
        )

        assertEquals(expected, SliderVerificationPolicy.retryTarget(root))
    }

    @Test
    fun retryPromptAloneProvidesContextWithoutRequiringTrack() {
        val retry = retry().copy(text = "请点我重试")
        val root = page(retry, prompt = "")

        assertEquals(retry.bounds, SliderVerificationPolicy.retryTarget(root))
        assertNull(SliderVerificationPolicy.drag(root))
    }

    @Test
    fun feedbackAloneNeverBecomesRetryTarget() {
        assertNull(SliderVerificationPolicy.retryTarget(page(retry().copy(text = "点我反馈"))))
    }

    private fun page(vararg children: NodeSnapshot, prompt: String = "滑动下方滑块") = NodeSnapshot(
        text = prompt,
        bounds = Rect(0, 0, 500, 800),
        children = children.toList(),
    )

    private fun track(id: String = "nc_1", slider: NodeSnapshot = slider(id)) = NodeSnapshot(
        viewId = "com.example:id/${id}_wrapper",
        bounds = Rect(80, 200, 400, 260),
        children = listOf(NodeSnapshot(children = listOf(slider))),
    )

    private fun slider(id: String = "nc_1") = NodeSnapshot(
        viewId = "com.example:id/${id}_n1z",
        bounds = Rect(100, 180, 140, 250),
    )

    private fun retry() = NodeSnapshot(text = "重试", bounds = Rect(100, 300, 300, 350))
}
