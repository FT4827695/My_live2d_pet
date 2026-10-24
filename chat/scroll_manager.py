# chat/scroll_manager.py
import time
from typing import List, Tuple, Optional, Callable

class ScrollManager:
    def __init__(
        self,
        get_messages_func: Callable[[], List[Tuple[str, str]]],
        get_text_width_func: Callable[[str], float],
        width: int,
        height: int,
        msg_top: int,
        input_h: int,
        msg_bottom_margin: int,
        msg_line_h: int,
        scale: float,
        text_margin: int,
        msg_y_pad: int = 0,
    ):
        self.get_messages = get_messages_func
        self.get_text_width = get_text_width_func
        self.width = width
        self.height = height
        self.msg_top = msg_top
        self.input_h = input_h
        self.msg_bottom_margin = msg_bottom_margin
        self.msg_line_h = msg_line_h
        self.scale = scale
        self.text_margin = text_margin
        self.msg_y_pad = msg_y_pad

        # 滚动状态
        self.scroll_offset = 0
        self.scrollbar_visible = False
        self.cursor_in_msg_area = False
        self.dragging_scroll = False
        self.drag_start_y = 0
        self.drag_start_offset = 0
        self.scrollbar_rect: Optional[Tuple[int, int, int, int]] = None
        self.thumb_rect: Optional[Tuple[int, int, int, int]] = None
        self.is_hovering_thumb = False
        self.can_start_drag_on_move = False
        self.pending_drag_start_y = 0
        self.pending_drag_start_offset = 0
        self.last_scroll_update_time = 0

        # 总行数缓存 / 提供者
        self.cached_total_lines: Optional[int] = None
        self.total_lines_provider: Optional[Callable[[], int]] = None

        # 自动滚动标志：True 表示允许自动跟随新消息滚动到底部
        self.auto_scroll = True

    # ==================== 总行数 ====================

    def set_total_lines_provider(self, provider: Optional[Callable[[], int]]):
        self.total_lines_provider = provider

    def set_total_lines(self, total_lines: int):
        self.cached_total_lines = max(0, int(total_lines))

    def _get_total_lines(self) -> int:
        if self.total_lines_provider is not None:
            try:
                value = self.total_lines_provider()
                if value is None:
                    value = 0
                return max(0, int(value))
            except Exception:
                pass

        if self.cached_total_lines is not None:
            return max(0, int(self.cached_total_lines))

        # 回退：直接计算（旧版兼容）
        total = 0
        from services import TextUtils
        for sender, text in self.get_messages():
            total += len(TextUtils.get_message_lines(
                sender, text,
                self.width - self.text_margin,
                self.get_text_width
            ))
        self.cached_total_lines = total
        return total

    def invalidate_cache(self):
        self.cached_total_lines = None

    def _get_max_visible_lines(self) -> int:
        available_height = (self.height - self.input_h - self.msg_bottom_margin
                            - self.msg_top - self.msg_y_pad)
        if available_height <= 0:
            return 1
        return available_height // self.msg_line_h

    def _need_scrollbar(self) -> bool:
        return self._get_total_lines() > self._get_max_visible_lines()

    # ==================== 滚动操作 ====================

    def scroll_to_bottom(self, force: bool = False) -> bool:
        """滚动到底部，并设置 auto_scroll = True"""
        total_lines = self._get_total_lines()
        max_visible = self._get_max_visible_lines()
        target_offset = max(0, total_lines - max_visible)
        if force or target_offset > self.scroll_offset:
            if self.scroll_offset != target_offset:
                self.scroll_offset = target_offset
                self.auto_scroll = True
                return True
        # 如果已经在底部，也确保 auto_scroll 为 True
        if self.scroll_offset >= target_offset:
            self.auto_scroll = True
        return False

    def scroll(self, delta: int):
        """用户滚动（滚轮、键盘等），更新 auto_scroll"""
        total_lines = self._get_total_lines()
        max_visible = self._get_max_visible_lines()
        max_offset = max(0, total_lines - max_visible)
        if total_lines > 0:
            max_offset = min(max_offset, total_lines - 1)
        new_offset = self.scroll_offset + delta
        self.scroll_offset = max(0, min(new_offset, max_offset))
        # 检查是否到达底部
        self.auto_scroll = (self.scroll_offset >= max_offset)

    def update_scrollbar_visibility(self, cursor_in_area: bool = None):
        if cursor_in_area is not None:
            self.cursor_in_msg_area = cursor_in_area
        if self.cursor_in_msg_area:
            self.scrollbar_visible = self._need_scrollbar()
        else:
            self.scrollbar_visible = False

    def start_drag(self, y: int):
        self.dragging_scroll = True
        self.drag_start_y = y
        self.drag_start_offset = self.scroll_offset
        self.can_start_drag_on_move = False

    def update_drag(self, y: int) -> bool:
        if not self.dragging_scroll or self.scrollbar_rect is None:
            return False
        dy = y - self.drag_start_y
        total_lines = self._get_total_lines()
        max_visible = self._get_max_visible_lines()
        max_offset = max(0, total_lines - max_visible)
        if max_offset <= 0:
            return False
        sx, sy, sw, sh = self.scrollbar_rect
        thumb_h = max(int(20 * self.scale), (max_visible / max(1, total_lines)) * sh)
        move_ratio = dy / (sh - thumb_h)
        new_offset = int(round(self.drag_start_offset + move_ratio * max_offset))
        new_offset = max(0, min(new_offset, max_offset))
        if new_offset != self.scroll_offset:
            self.scroll_offset = new_offset
            self.auto_scroll = (self.scroll_offset >= max_offset)
            now = time.time()
            if now - self.last_scroll_update_time >= 0.1:
                self.last_scroll_update_time = now
                return True
        return False

    def click_track(self, y: int) -> bool:
        total_lines = self._get_total_lines()
        max_visible = self._get_max_visible_lines()
        max_offset = max(0, total_lines - max_visible)
        if max_offset <= 0 or self.scrollbar_rect is None:
            return False
        sx, sy, sw, sh = self.scrollbar_rect
        ratio = max(0.0, min(1.0, (y - sy) / sh))
        target_offset = int(round(ratio * max_offset))
        if y <= sy + int(20 * self.scale):
            target_offset = 0
        elif y >= sy + sh - int(20 * self.scale):
            target_offset = max_offset
        if target_offset != self.scroll_offset:
            self.scroll_offset = target_offset
            self.auto_scroll = (self.scroll_offset >= max_offset)
            return True
        else:
            # 点击轨道但未改变偏移，则翻页
            new_offset = self.scroll_offset + (max_visible if y > sy + sh / 2 else -max_visible)
            new_offset = max(0, min(max_offset, new_offset))
            if new_offset != self.scroll_offset:
                self.scroll_offset = new_offset
                self.auto_scroll = (self.scroll_offset >= max_offset)
                return True
        # 允许后续拖动
        self.pending_drag_start_y = y
        self.pending_drag_start_offset = self.scroll_offset
        self.can_start_drag_on_move = True
        return False
