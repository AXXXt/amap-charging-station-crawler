package com.tigercode.evcollector.core

import com.tigercode.evcollector.core.engine.SearchEntryPolicy
import com.tigercode.evcollector.core.model.NodeSnapshot
import com.tigercode.evcollector.core.model.Rect
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchEntryPolicyTest {
    private val mi8Keyboard = listOf(Rect(0, 1370, 1080, 2362))

    @Test
    fun lowerSuggestionCannotBeTappedThroughMi8Keyboard() {
        assertTrue(SearchEntryPolicy.isTapObscuredByKeyboard(476, 1510, mi8Keyboard))
        assertFalse(SearchEntryPolicy.isTapObscuredByKeyboard(476, 925, mi8Keyboard))
        // The same coordinates become safe once the input-method window is gone.
        assertFalse(SearchEntryPolicy.isTapObscuredByKeyboard(476, 1510, emptyList()))
    }

    @Test
    fun jitterNearKeyboardEdgeIsAlsoBlocked() {
        assertTrue(SearchEntryPolicy.isTapObscuredByKeyboard(476, 1365, mi8Keyboard))
        assertFalse(SearchEntryPolicy.isTapObscuredByKeyboard(476, 1362, mi8Keyboard))
    }

    @Test
    fun floatingKeyboardOnlyBlocksItsActualRectangle() {
        val bounds = listOf(Rect(500, 1200, 1050, 1800), Rect())
        assertTrue(SearchEntryPolicy.isTapObscuredByKeyboard(750, 1500, bounds))
        assertFalse(SearchEntryPolicy.isTapObscuredByKeyboard(250, 1500, bounds))
        assertFalse(SearchEntryPolicy.isTapObscuredByKeyboard(750, 1900, bounds))
    }

    private fun page(node: NodeSnapshot) = NodeSnapshot(
        bounds = Rect(0, 0, 1080, 2280), children = listOf(node),
    )

    @Test
    fun suggestionQueryRemainsRecognizableAfterEditorLosesFocus() {
        val editor = NodeSnapshot(
            className = "android.widget.EditText", text = "锐创汽车充电站",
            bounds = Rect(100, 80, 900, 200), focused = false,
        )
        assertTrue(SearchEntryPolicy.hasQueryEditor(page(editor), "锐创汽车充电站"))
        assertFalse(SearchEntryPolicy.hasQueryEditor(page(editor), "云快充充电站(红旗岗站)"))
        assertFalse(SearchEntryPolicy.hasQueryEditor(page(editor.copy(enabled = false)), editor.text))
    }

    @Test
    fun detailTitleOrLowerEditorCannotBeMistakenForSearchContext() {
        val title = NodeSnapshot(text = "锐创汽车充电站", bounds = Rect(100, 80, 900, 200))
        assertFalse(SearchEntryPolicy.hasQueryEditor(page(title), title.text))
        val lowerEditor = title.copy(className = "android.widget.EditText", bounds = Rect(100, 900, 900, 1000))
        assertFalse(SearchEntryPolicy.hasQueryEditor(page(lowerEditor), title.text))
    }
}
