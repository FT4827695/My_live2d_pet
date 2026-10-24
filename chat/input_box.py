# chat/input_box.py

import time
import unicodedata
from typing import List, Tuple, Optional

from services import TextUtils


class InputBox:

    def __init__(
        self,
        width: int,
        font_getter,
        line_spacing: int,
        padding: int,
        visible_lines: int, font_size: int = 26
    ):
        self.width = width
        self._font_getter = font_getter
        self._font = font_getter(font_size)

        self.line_spacing = line_spacing
        self.padding = padding
        self.visible_lines = max(1, visible_lines)

        # 真实文本
        self.text = ""

        # 视觉行
        self.lines: List[str] = [""]

        # 每个视觉行在 self.text 中的起始绝对索引
        self.line_starts: List[int] = [0]

        # 光标状态
        self.cursor_line = 0
        self.cursor_col = 0
        self.cursor_pos = 0

        # 滚动
        self.scroll_offset = 0

        # 选择
        self.selection_start: Optional[int] = None
        self.selection_end: Optional[int] = None

        # 兼容外部可能使用的拖曳选择标记
        self.selecting = False

        # 光标闪烁
        self._cursor_visible = True
        self._last_cursor_time = 0.0

        # 缓存
        self._cached_lines: List[str] = []
        self._cached_text = ""

        # 可用宽度
        self.available_width = max(1, width)

    # ================= 基础工具 =================

    def _get_text_width(self, text: str) -> float:
        if hasattr(self._font, "getlength"):
            return self._font.getlength(text)
        return self._font.getsize(text)[0]

    def _sanitize_text(self, text: str) -> str:
        if not text:
            return ""

        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")
        text = text.replace("\x0b", "\n")
        text = text.replace("\x0c", "\n")
        text = text.replace("\t", "    ")

        result = []
        for ch in text:
            if ch == "\n":
                result.append(ch)
                continue

            code = ord(ch)

            # 移除常见不可见控制字符和行分隔符
            if code < 32:
                continue
            if code in (0x85, 0x2028, 0x2029):
                continue

            result.append(ch)

        return "".join(result)

    # ================= 换行与索引映射 =================

    def _wrap_paragraph(self, paragraph: str) -> List[str]:
        if paragraph == "":
            return [""]

        self.available_width = max(1, self.width)

        # 优先使用已有 TextUtils
        try:
            wrapped = TextUtils.wrap_text_to_lines(
                paragraph,
                self.available_width,
                self._get_text_width
            )

            if wrapped and "".join(wrapped) == paragraph:
                return wrapped
        except Exception:
            pass

        # 内部安全回退换行
        lines: List[str] = []
        current = ""
        current_width = 0.0

        for ch in paragraph:
            ch_width = self._get_text_width(ch)

            if current and current_width + ch_width > self.available_width:
                lines.append(current)
                current = ch
                current_width = ch_width
            else:
                current += ch
                current_width += ch_width

        lines.append(current)

        if not lines:
            lines = [""]

        return lines

    def _rebuild_lines(self):
        text = self.text if isinstance(self.text, str) else ""
        self.text = text
        self.available_width = max(1, self.width)

        lines: List[str] = []
        line_starts: List[int] = []

        paragraphs = text.split("\n")
        current_pos = 0

        for p_idx, paragraph in enumerate(paragraphs):
            para_lines = self._wrap_paragraph(paragraph)
            if not para_lines:
                para_lines = [""]

            offset = 0
            for line in para_lines:
                lines.append(line)
                line_starts.append(current_pos + offset)
                offset += len(line)

            current_pos += len(paragraph)

            if p_idx != len(paragraphs) - 1:
                current_pos += 1

        if not lines:
            lines = [""]
            line_starts = [0]

        self.lines = lines
        self.line_starts = line_starts

        self._cached_lines = self.lines[:]
        self._cached_text = self.text

        self.cursor_pos = max(0, min(self.cursor_pos, len(self.text)))
        self.cursor_line, self.cursor_col = self._abs_to_cursor(self.cursor_pos)

    def _abs_to_cursor(self, abs_idx: int) -> Tuple[int, int]:
        abs_idx = max(0, min(abs_idx, len(self.text)))

        if not self.lines:
            return 0, 0

        if (
            not getattr(self, "line_starts", None)
            or len(self.line_starts) != len(self.lines)
        ):
            remaining = abs_idx
            for i, line in enumerate(self.lines):
                if remaining <= len(line):
                    return i, remaining
                remaining -= len(line)
            return len(self.lines) - 1, len(self.lines[-1])

        lo = 0
        hi = len(self.lines) - 1

        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= abs_idx:
                lo = mid
            else:
                hi = mid - 1

        line = lo
        col = abs_idx - self.line_starts[line]
        line_len = len(self.lines[line])

        if col < 0:
            col = 0
        elif col > line_len:
            col = line_len

        return line, col

    def _line_col_to_abs(self, line: int, col: int) -> int:
        if not self.lines:
            return 0

        line = max(0, min(line, len(self.lines) - 1))
        col = max(0, min(col, len(self.lines[line])))

        if (
            getattr(self, "line_starts", None)
            and len(self.line_starts) == len(self.lines)
        ):
            abs_idx = self.line_starts[line] + col
        else:
            abs_idx = col + sum(len(l) for l in self.lines[:line])

        return max(0, min(abs_idx, len(self.text)))

    def get_line_abs_start(self, line: int) -> int:
        """返回指定视觉行在 self.text 中的起始绝对索引（正确处理换行符）"""
        if (getattr(self, "line_starts", None)
                and len(self.line_starts) == len(self.lines)
                and 0 <= line < len(self.line_starts)):
            return self.line_starts[line]
        return sum(len(l) for l in self.lines[:line])
    
    def _cursor_to_abs(self) -> int:
        self.cursor_pos = max(0, min(self.cursor_pos, len(self.text)))
        return self.cursor_pos

    def _set_cursor_from_abs(self, abs_idx: int):
        self.cursor_pos = max(0, min(abs_idx, len(self.text)))
        self.cursor_line, self.cursor_col = self._abs_to_cursor(self.cursor_pos)

    def _ensure_cursor_visible(self):
        if self.cursor_line < self.scroll_offset:
            self.scroll_offset = self.cursor_line
        elif self.cursor_line >= self.scroll_offset + self.visible_lines:
            self.scroll_offset = self.cursor_line - self.visible_lines + 1

        max_off = max(0, len(self.lines) - self.visible_lines)
        self.scroll_offset = max(0, min(self.scroll_offset, max_off))

    # ================= 字符边界 =================

    def _prev_char_boundary(self, text: str, index: int) -> int:
        if index <= 0:
            return 0

        try:
            import grapheme
            prev = grapheme.prev_break(text, index)
            if prev is not None:
                return max(0, prev)
        except Exception:
            pass

        i = index

        while i > 0 and unicodedata.combining(text[i - 1]):
            i -= 1

        if i < index:
            i -= 1
        else:
            i = index - 1

        return max(0, i)

    def _next_char_boundary(self, text: str, index: int) -> int:
        if index >= len(text):
            return len(text)

        try:
            import grapheme
            nxt = grapheme.next_break(text, index)
            if nxt is not None:
                return min(len(text), nxt)
        except Exception:
            pass

        i = index + 1

        while i < len(text) and unicodedata.combining(text[i]):
            i += 1

        return i

    # ================= 选择 =================

    def _has_selection(self) -> bool:
        return (
            self.selection_start is not None
            and self.selection_end is not None
            and self.selection_start != self.selection_end
        )

    def _get_selection_range(self) -> Optional[Tuple[int, int]]:
        if not self._has_selection():
            return None

        return (
            min(self.selection_start, self.selection_end),
            max(self.selection_start, self.selection_end)
        )

    def _delete_selection(self) -> int:
        if not self._has_selection():
            return self._cursor_to_abs()

        start, end = self._get_selection_range()

        self.text = self.text[:start] + self.text[end:]
        self.selection_start = None
        self.selection_end = None

        self._rebuild_lines()
        self._set_cursor_from_abs(start)
        self._ensure_cursor_visible()

        return start

    def select_all(self):
        self.selection_start = 0
        self.selection_end = len(self.text)
        self._set_cursor_from_abs(len(self.text))

    def select_word_at(self, abs_idx: int):
        text = self.text
        if not text:
            return
        abs_idx = max(0, min(abs_idx, len(text)))
        check_idx = min(abs_idx, len(text) - 1)

        def char_type(c):
            if c.isspace():
                return 'space'
            code = ord(c)
            if (0x4E00 <= code <= 0x9FFF or
                    0x3400 <= code <= 0x4DBF or
                    0xF900 <= code <= 0xFAFF):
                return 'cjk'
            if c.isalnum():
                return 'word'
            return 'punct'

        target = char_type(text[check_idx])
        start = check_idx
        end = check_idx
        while start > 0 and char_type(text[start - 1]) == target:
            start -= 1
        while end < len(text) - 1 and char_type(text[end + 1]) == target:
            end += 1

        self.selection_start = start
        self.selection_end = end + 1
        self._set_cursor_from_abs(end + 1)
        self.reset_cursor_blink()
        
    def clear_selection(self):
        self.selection_start = None
        self.selection_end = None

    def get_selection(self) -> Optional[Tuple[int, int]]:
        return self._get_selection_range()

    def is_selection_active(self) -> bool:
        return self._has_selection()

    # ================= 文本操作 =================

    def set_text(self, text: str):
        self.text = self._sanitize_text(text)
        self.selection_start = None
        self.selection_end = None
        self.cursor_pos = 0

        self._rebuild_lines()
        self._set_cursor_from_abs(0)
        self._ensure_cursor_visible()

    def get_text(self) -> str:
        return self.text

    def insert_text(self, text: str, pos: int = None):
        text = self._sanitize_text(text)
        if not text:
            return

        if self._has_selection():
            start, end = self._get_selection_range()
            self.text = self.text[:start] + self.text[end:]
            pos = start
            self.selection_start = None
            self.selection_end = None
        else:
            if pos is None:
                pos = self._cursor_to_abs()
            pos = max(0, min(pos, len(self.text)))

        self.text = self.text[:pos] + text + self.text[pos:]

        self._rebuild_lines()
        self._set_cursor_from_abs(pos + len(text))
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    def delete_backward(self):
        if self._has_selection():
            self._delete_selection()
            self.reset_cursor_blink()
            return

        abs_idx = self._cursor_to_abs()
        if abs_idx <= 0:
            return

        start = self._prev_char_boundary(self.text, abs_idx)

        self.text = self.text[:start] + self.text[abs_idx:]

        self._rebuild_lines()
        self._set_cursor_from_abs(start)
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    def delete_forward(self):
        if self._has_selection():
            self._delete_selection()
            self.reset_cursor_blink()
            return

        abs_idx = self._cursor_to_abs()
        if abs_idx >= len(self.text):
            return

        end = self._next_char_boundary(self.text, abs_idx)

        self.text = self.text[:abs_idx] + self.text[end:]

        self._rebuild_lines()
        self._set_cursor_from_abs(abs_idx)
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    def delete_word_backward(self):
        if self._has_selection():
            self._delete_selection()
            self.reset_cursor_blink()
            return

        abs_idx = self._cursor_to_abs()
        if abs_idx <= 0:
            return

        text = self.text
        i = abs_idx

        # 跳过光标前的空白
        while i > 0 and text[i - 1].isspace():
            i -= 1

        # 删除连续非空白内容
        while i > 0 and not text[i - 1].isspace():
            i -= 1

        self.text = text[:i] + text[abs_idx:]

        self._rebuild_lines()
        self._set_cursor_from_abs(i)
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    # ================= 剪贴板 =================

    def copy_selected(self) -> str:
        if not self._has_selection():
            return ""

        start, end = self._get_selection_range()
        return self.text[start:end]

    def cut_selected(self) -> str:
        if not self._has_selection():
            return ""

        start, end = self._get_selection_range()
        selected = self.text[start:end]

        self._delete_selection()
        self.reset_cursor_blink()

        return selected

    def paste(self, text: str):
        text = self._sanitize_text(text)
        if not text:
            return

        if self._has_selection():
            start, end = self._get_selection_range()
            self.text = self.text[:start] + self.text[end:]
            self.selection_start = None
            self.selection_end = None
            pos = start
        else:
            pos = self._cursor_to_abs()

        self.text = self.text[:pos] + text + self.text[pos:]

        self._rebuild_lines()
        self._set_cursor_from_abs(pos + len(text))
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    # ================= 光标移动 =================

    def move_cursor_left(self):
        if self._has_selection():
            start, _ = self._get_selection_range()
            self.clear_selection()
            self._set_cursor_from_abs(start)
            self._ensure_cursor_visible()
            self.reset_cursor_blink()
            return

        new_pos = self._prev_char_boundary(self.text, self._cursor_to_abs())
        self._set_cursor_from_abs(new_pos)
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    def move_cursor_right(self):
        if self._has_selection():
            _, end = self._get_selection_range()
            self.clear_selection()
            self._set_cursor_from_abs(end)
            self._ensure_cursor_visible()
            self.reset_cursor_blink()
            return

        new_pos = self._next_char_boundary(self.text, self._cursor_to_abs())
        self._set_cursor_from_abs(new_pos)
        self._ensure_cursor_visible()
        self.reset_cursor_blink()

    def move_cursor_up(self):
        if self._has_selection():
            self.clear_selection()

        if self.cursor_line > 0:
            new_line = self.cursor_line - 1
            new_col = min(self.cursor_col, len(self.lines[new_line]))
            new_abs = self._line_col_to_abs(new_line, new_col)

            self._set_cursor_from_abs(new_abs)
            self._ensure_cursor_visible()
            self.reset_cursor_blink()

    def move_cursor_down(self):
        if self._has_selection():
            self.clear_selection()

        if self.cursor_line < len(self.lines) - 1:
            new_line = self.cursor_line + 1
            new_col = min(self.cursor_col, len(self.lines[new_line]))
            new_abs = self._line_col_to_abs(new_line, new_col)

            self._set_cursor_from_abs(new_abs)
            self._ensure_cursor_visible()
            self.reset_cursor_blink()

    def set_cursor(self, abs_idx: int):
        self._set_cursor_from_abs(abs_idx)
        self._ensure_cursor_visible()

    # ================= 滚动 =================

    def scroll(self, delta: int):
        max_off = max(0, len(self.lines) - self.visible_lines)
        self.scroll_offset = max(0, min(self.scroll_offset + delta, max_off))

    # ================= 查询接口 =================

    def get_cursor_position(self) -> Tuple[int, int]:
        return self.cursor_line, self.cursor_col

    def get_cursor_abs_index(self) -> int:
        return self._cursor_to_abs()

    def get_text_lines(self) -> List[str]:
        return self.lines

    def get_visible_lines_range(self) -> Tuple[int, int]:
        start = self.scroll_offset
        end = min(start + self.visible_lines, len(self.lines)) - 1
        end = max(start, end)
        return start, end

    # ================= 光标闪烁 =================

    def update_cursor_blink(self, current_time: float) -> bool:
        if current_time - self._last_cursor_time >= 0.5:
            self._last_cursor_time = current_time
            self._cursor_visible = not self._cursor_visible
        return self._cursor_visible

    def reset_cursor_blink(self):
        self._cursor_visible = True
        self._last_cursor_time = time.time()

    def get_cursor_visible(self) -> bool:
        return self._cursor_visible

    # ================= 宽度变化 =================

    def set_width(self, width: int):
        self.width = width
        self.available_width = max(1, width)

        self._rebuild_lines()
        self._set_cursor_from_abs(self.cursor_pos)
        self._ensure_cursor_visible()
