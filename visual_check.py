"""
visual_check.py — 视觉模型自检模块
功能：
  1. 每次操作后截图，送视觉模型判断页面状态
  2. 检测弹窗遮挡（权限请求、广告、切换应用提示等）
  3. 检测误触导致的页面跳转
  4. 自动恢复机制
"""
import uiautomator2 as u2
import time
import base64
import os
from enum import Enum


class PageState(Enum):
    """页面状态枚举"""
    DETAIL_PAGE = "detail_page"       # 充电站详情页
    SEARCH_RESULTS = "search_results"  # 搜索结果列表
    MAP_VIEW = "map_view"             # 地图主页
    POPUP_BLOCKING = "popup_blocking" # 弹窗遮挡
    UNKNOWN = "unknown"               # 未知状态
    ERROR = "error"                   # 异常页面


class VisualChecker:
    """
    视觉模型自检器
    
    使用方式：
      checker = VisualChecker(device, visual_model_func)
      state = checker.check()
      if state == PageState.POPUP_BLOCKING:
          checker.recover()
    
    visual_model_func 签名:
      def visual_model(image_path: str) -> dict:
          return {
              "page_type": "detail_page|search_results|map_view|popup|other",
              "has_popup": True/False,
              "popup_description": "...",
              "is_normal": True/False,
              "suggestion": "..."
          }
    """
    
    # 页面特征关键词（用于无视觉模型时的 fallback 文本检测）
    DETAIL_KEYWORDS = ["营业时间", "电站信息", "24小时价格趋势图", "扫码充电"]
    SEARCH_KEYWORDS = ["充电站", "搜索", "重卡"]
    POPUP_KEYWORDS = ["允许", "始终允许", "仅在使用时允许", "拒绝", "更新", "评价",
                       "跳过", "关闭", "我知道了", "立即体验"]
    
    def __init__(self, device: u2.Device, visual_model_func=None,
                 screenshot_dir=r"C:\Users\26381\Desktop\adb-first\screenshots"):
        self.d = device
        self.visual_model = visual_model_func
        self.screenshot_dir = screenshot_dir
        os.makedirs(screenshot_dir, exist_ok=True)
        self.check_count = 0
    
    def take_screenshot(self, label="check"):
        """截图并保存"""
        self.check_count += 1
        filename = f"check_{self.check_count:04d}_{label}.png"
        filepath = os.path.join(self.screenshot_dir, filename)
        self.d.screenshot(filepath)
        return filepath
    
    def check_text_fallback(self):
        """基于XML文本的页面状态检测（fallback方案）"""
        xml = self.d.dump_hierarchy()
        
        # 检查弹窗关键词
        for kw in self.POPUP_KEYWORDS:
            if kw in xml:
                return PageState.POPUP_BLOCKING
        
        # 检查详情页关键词
        detail_score = sum(1 for kw in self.DETAIL_KEYWORDS if kw in xml)
        if detail_score >= 3:
            return PageState.DETAIL_PAGE
        
        # 检查搜索结果
        search_score = sum(1 for kw in self.SEARCH_KEYWORDS if kw in xml)
        if search_score >= 2:
            return PageState.SEARCH_RESULTS
        
        return PageState.UNKNOWN
    
    def check(self, use_visual=True):
        """
        检查当前页面状态
        
        Args:
            use_visual: 是否使用视觉模型（False则用文本fallback）
        
        Returns:
            (PageState, dict) — 页面状态和额外信息
        """
        filepath = self.take_screenshot()
        
        info = {"screenshot": filepath}
        
        if use_visual and self.visual_model:
            try:
                result = self.visual_model(filepath)
                info["visual_result"] = result
                
                page_type = result.get("page_type", "other")
                has_popup = result.get("has_popup", False)
                
                if has_popup or page_type == "popup":
                    info["popup_desc"] = result.get("popup_description", "")
                    return PageState.POPUP_BLOCKING, info
                
                type_map = {
                    "detail_page": PageState.DETAIL_PAGE,
                    "search_results": PageState.SEARCH_RESULTS,
                    "map_view": PageState.MAP_VIEW,
                }
                return type_map.get(page_type, PageState.UNKNOWN), info
                
            except Exception as e:
                print(f"  [!] 视觉模型调用失败: {e}, 使用文本fallback")
        
        # Fallback: text-based check
        state = self.check_text_fallback()
        info["fallback"] = True
        return state, info
    
    def recover(self, expected_state=PageState.DETAIL_PAGE):
        """
        尝试恢复到预期页面状态
        
        Returns:
            bool — 是否成功恢复
        """
        print("  [恢复] 检测到异常状态，尝试恢复...")
        
        # Step 1: 尝试关闭弹窗
        for attempt in range(3):
            state, info = self.check(use_visual=False)
            if state == expected_state:
                print("  [恢复] ✅ 页面已正常")
                return True
            
            if state == PageState.POPUP_BLOCKING:
                print(f"  [恢复] 尝试关闭弹窗 (第{attempt+1}次)")
                # 尝试点击可能的关闭按钮
                self._dismiss_popup()
                time.sleep(2)
            else:
                # 不是弹窗，尝试返回
                print(f"  [恢复] 当前状态: {state}, 尝试返回")
                self.d.press("back")
                time.sleep(2)
        
        print("  [恢复] ❌ 自动恢复失败，需要人工介入")
        return False
    
    def _dismiss_popup(self):
        """尝试关闭弹窗"""
        xml = self.d.dump_hierarchy()
        
        # 查找可能的关闭文本
        dismiss_texts = ["关闭", "跳过", "取消", "我知道了", "暂不", "以后再说",
                         "同意", "允许", "始终允许", "仅在使用时允许"]
        
        import re
        for text in dismiss_texts:
            pattern = f'text="{text}"[^>]*bounds="\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]"'
            m = re.search(pattern, xml)
            if m:
                x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                print(f"    点击 '{text}' at ({cx}, {cy})")
                self.d.click(cx, cy)
                return
        
        # 尝试点击屏幕边缘（关闭弹窗的常见方式）
        self.d.click(100, 200)
    
    def ensure_detail_page(self, max_retries=3):
        """
        确保在详情页，如不在则尝试恢复
        
        Returns:
            bool — 是否在详情页
        """
        for i in range(max_retries):
            state, info = self.check()
            
            if state == PageState.DETAIL_PAGE:
                return True
            
            if state == PageState.POPUP_BLOCKING:
                print(f"  [自检] 弹窗: {info.get('popup_desc', '未知')}")
                self._dismiss_popup()
                time.sleep(2)
                continue
            
            print(f"  [自检] 不在详情页 (状态: {state}), 尝试恢复...")
            if not self.recover(PageState.DETAIL_PAGE):
                return False
        
        return False


# ============================================================
# 视觉模型接口适配器
# ============================================================
class VisualModelAdapter:
    """
    视觉模型适配器 — 用户可以将自己的视觉模型接入
    
    使用示例:
        def my_vision_model(image_path):
            # 调用你的视觉模型API
            response = your_vision_api.analyze(image_path)
            return {
                "page_type": response.page_type,
                "has_popup": response.has_popup,
                "popup_description": response.popup_text,
            }
        
        adapter = VisualModelAdapter(my_vision_model)
        checker = VisualChecker(device, visual_model_func=adapter)
    """
    
    def __init__(self, model_func):
        """
        Args:
            model_func: callable(image_path: str) -> dict
                返回格式: {
                    "page_type": "detail_page|search_results|map_view|popup|other",
                    "has_popup": bool,
                    "popup_description": str,
                    "is_normal": bool,
                    "suggestion": str
                }
        """
        self.model_func = model_func
    
    def __call__(self, image_path):
        return self.model_func(image_path)


# ============================================================
# 快速集成到 Crawler
# ============================================================
def integrate_with_crawler(crawler_instance, visual_model_func=None):
    """
    将视觉自检集成到采集器中
    
    Usage:
        from crawler import AmapCrawler
        crawler = AmapCrawler()
        integrate_with_crawler(crawler, my_visual_model)
        crawler.run_all()
    """
    checker = VisualChecker(crawler_instance.d, visual_model_func)
    
    # 注入自检：在进入详情页后验证
    original_collect = crawler_instance.collect_detail
    
    def checked_collect(station, city):
        # 进入详情页
        crawler_instance.d.click(station["cx"], station["cy"])
        time.sleep(3)
        
        # 视觉自检
        is_ok = checker.ensure_detail_page(max_retries=2)
        if not is_ok:
            print(f"    [!] 自检失败，跳过 {station['name'][:30]}")
            crawler_instance.d.press("back")
            return None
        
        # 正常采集
        return original_collect(station, city)
    
    crawler_instance.collect_detail = checked_collect
    return checker


if __name__ == "__main__":
    # 测试：纯文本fallback模式
    d = u2.connect("RFCXA0W194D")
    checker = VisualChecker(d, visual_model_func=None)
    
    state, info = checker.check(use_visual=False)
    print(f"当前页面状态: {state}")
    print(f"截图: {info['screenshot']}")
