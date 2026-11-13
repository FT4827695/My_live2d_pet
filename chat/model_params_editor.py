# chat/model_params_editor.py
import os
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import glfw
except Exception:
    glfw = None

from PIL import Image, ImageDraw

from .input_box import InputBox

try:
    from .menu_manager import (
        KV_CACHE_OPTIONS_FULL,
        MTP_OPTIONS_FULL,
        ModelSettings,
    )
except Exception:
    # 兜底
    KV_CACHE_OPTIONS_FULL = ["f16", "q8_0", "iq4_nl", "q4_0", "q8_iq4"]
    MTP_OPTIONS_FULL = list(range(9))
    ModelSettings = None


DEFAULT_TITLE = "模型高级设置"


# (key, label, group, kind, spec)
# group: "load" / "sampling"
# kind:  "int" / "float" / "enum"
FIELD_DEFS: List[Tuple[str, str, str, str, Dict[str, Any]]] = [
    # ---- 加载时 ----
    ("ctx", "CTX", "load", "int",
     {"min": 2048, "max": 1048576, "default": 8192}),
    ("kv_cache", "KV Cache", "load", "enum",
     {"options": KV_CACHE_OPTIONS_FULL, "default": "q8_0"}),
    ("vram_extra_mb", "显存调整", "load", "int",
     {"min": -2048, "max": 4096, "default": 0, "suffix": " MB"}),
    ("mtp", "MTP", "load", "enum",
     {"options": MTP_OPTIONS_FULL, "default": 0}),
    # ---- 采样 ----
    ("temperature", "温度", "sampling", "float",
     {"min": 0.0, "max": 2.0, "default": 0.7}),
    ("repeat_penalty", "重复惩罚", "sampling", "float",
     {"min": 1.0, "max": 1.5, "default": 1.0}),
    ("presence_penalty", "存在惩罚", "sampling", "float",
     {"min": 0.0, "max": 2.0, "default": 0.0}),
    ("top_k", "Top-K", "sampling", "int",
     {"min": 0, "max": 100, "default": 40}),
    ("top_p", "Top-P", "sampling", "float",
     {"min": 0.0, "max": 1.0, "default": 0.95}),
    ("min_p", "Min-P", "sampling", "float",
     {"min": 0.0, "max": 1.0, "default": 0.05}),
]

GROUP_LABELS = {
    "load": "加载参数",
    "sampling": "采样参数",
}


class ModelParamsEditor:

    def __init__(
        self,
        model_settings: Any,
        get_font: Callable[[int], Any],
        scale: float,
        width: int,
        height: int,
        font_sizes: Optional[Dict[str, int]] = None,
        request_update: Optional[Callable[..., None]] = None,
        mark_dirty: Optional[Callable[[], None]] = None,
        add_message: Optional[Callable[[str, str], None]] = None,
    ):
        self.model_settings = model_settings
        self.get_font = get_font
        self.scale = scale
        self.width = width
        self.height = height
        self.font_sizes = font_sizes or {}

        self.request_update_callback = request_update
        self.mark_dirty_callback = mark_dirty
        self.add_message = add_message

        self.visible = False
        self.focus: Optional[Tuple[str, str]] = None  # (kind, key)，kind = "input"|"enum"
        self.message = ""
        self.error = ""

        # 运行时值缓存
        self._input_boxes: Dict[str, InputBox] = {}
        self._enum_values: Dict[str, Any] = {}

        # 布局缓存
        self._panel_rect: Optional[Tuple[int, int, int, int]] = None
        self._title_rect: Optional[Tuple[int, int, int, int]] = None
        self._content_rect: Optional[Tuple[int, int, int, int]] = None
        self._group_rects: Dict[str, Tuple[int, int, int, int]] = {}
        self._row_rects: List[Dict[str, Any]] = []
        self._save_rect: Optional[Tuple[int, int, int, int]] = None
        self._cancel_rect: Optional[Tuple[int, int, int, int]] = None
        self._scrollbar_rect: Optional[Tuple[int, int, int, int]] = None
        self._buttons_y = 0

        # 字体度量
        self._input_font_size = 20
        self._input_pad = 8
        self._ascent = 16
        self._descent = 5
        self._line_h = 24

        self._layout_key: Optional[Tuple] = None
        self._content_h = 0
        self._scroll_y = 0
        self._max_scroll_y = 0
        self._scroll_drag_active = False

    # ============================================================
    # 基础
    # ============================================================
    def _request(self, urgent: bool = True) -> None:
        try:
            if self.request_update_callback is not None:
                try:
                    self.request_update_callback(urgent=urgent)
                except TypeError:
                    self.request_update_callback()
        except Exception:
            pass
        try:
            if self.mark_dirty_callback is not None:
                self.mark_dirty_callback()
        except Exception:
            pass

    @staticmethod
    def _rect_contains(rect, x, y):
        if not rect:
            return False
        rx, ry, rw, rh = rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    @staticmethod
    def _rect_xyxy(rect):
        x, y, w, h = rect
        return x, y, x + w, y + h

    def _text_width(self, font, text):
        if not text:
            return 0.0
        try:
            if hasattr(font, "getlength"):
                return font.getlength(text)
            return font.getsize(text)[0]
        except Exception:
            return 0.0

    def _measure_font(self, font):
        try:
            bbox = font.getbbox("中Ag")
            ascent = max(0, -bbox[1])
            descent = max(0, bbox[3] - bbox[1] - ascent)
            if ascent + descent > 0:
                return ascent, descent
        except Exception:
            pass
        return int(16 * self.scale), int(5 * self.scale)

    def _focused_box(self) -> Optional[InputBox]:
        if not self.focus:
            return None
        kind, key = self.focus
        if kind != "input":
            return None
        return self._input_boxes.get(key)

    # ============================================================
    # 打开 / 关闭 / 加载 / 保存
    # ============================================================
    def open(self) -> None:
        if self.visible:
            return
        self._load_from_settings()
        self._layout_key = None
        self._scroll_y = 0
        self._sync_layout(self.width, self.height, self.scale)
        self.message = ""
        self.error = ""
        self.focus = None
        self.visible = True
        self._request(True)

    def close(self) -> None:
        if not self.visible:
            return
        self.visible = False
        self._scroll_drag_active = False
        self._request(True)

    def _load_from_settings(self) -> None:
        data = getattr(self.model_settings, "data", {}) or {}
        self._input_boxes.clear()
        self._enum_values.clear()

        for key, _label, _group, kind, spec in FIELD_DEFS:
            if kind == "enum":
                value = data.get(key, spec["default"])
                options = spec["options"]
                if value not in options:
                    pass
                self._enum_values[key] = value
            else:
                value = data.get(key, spec["default"])
                self._input_boxes[key] = self._make_input_box(value)

    def _make_input_box(self, value: Any) -> InputBox:
        box = InputBox(
            width=100,
            font_getter=self.get_font,
            line_spacing=max(1, int(2 * self.scale)),
            padding=self._input_pad,
            visible_lines=1,
            font_size=self._input_font_size,
        )
        box.set_text(self._format_value_for_input(value))
        box.set_cursor(len(box.get_text()))
        return box

    @staticmethod
    def _format_value_for_input(value: Any) -> str:
        if isinstance(value, float):
            if abs(value - round(value)) < 1e-9:
                return str(int(round(value)))
            return f"{value:g}"
        return str(value)

    def save(self) -> None:
        if not self.visible:
            return

        ms = self.model_settings
        if ms is None or not hasattr(ms, "data"):
            self.error = "模型设置模块不可用"
            self._request(True)
            return

        new_values: Dict[str, Any] = {}
        errors: List[str] = []

        # 先解析 InputBox
        for key, label, _group, kind, spec in FIELD_DEFS:
            if kind == "enum":
                new_values[key] = self._enum_values.get(key, spec["default"])
                continue

            box = self._input_boxes.get(key)
            if box is None:
                errors.append(f"{label}: 输入框缺失")
                continue

            raw = box.get_text().strip()
            if raw == "":
                errors.append(f"{label}: 不能为空")
                continue

            try:
                if kind == "int":
                    fv = float(raw)
                    if abs(fv - round(fv)) > 1e-9:
                        errors.append(f"{label}: 必须是整数")
                        continue
                    v = int(round(fv))                    
                else:
                    v = float(raw)
            except Exception:
                errors.append(f"{label}: 不是有效数值 ({raw})")
                continue

            vmin = spec.get("min")
            vmax = spec.get("max")
            if vmin is not None and v < vmin:
                errors.append(f"{label}: 不能小于 {vmin}（当前 {v}）")
                continue
            if vmax is not None and v > vmax:
                errors.append(f"{label}: 不能大于 {vmax}（当前 {v}）")
                continue

            new_values[key] = v

        if errors:
            self.error = "；".join(errors)
            self._request(True)
            return

        # 全量写回 ModelSettings.data
        for key, value in new_values.items():
            ms.data[key] = value

        try:
            ms.save()
        except Exception as e:
            self.error = f"保存失败：{e}"
            self._request(True)
            return

        self.visible = False
        self.error = ""
        self.message = "高级设置已保存，重新加载模型后生效。"
        self._scroll_drag_active = False

        if self.add_message is not None:
            try:
                self.add_message("shizuku", "高级设置已保存，重新加载模型后生效。")
            except Exception:
                pass

        self._request(True)

    # ============================================================
    # 布局
    # ============================================================
    def _sync_layout(self, width: int, height: int, scale: float) -> None:
        self.width = width
        self.height = height
        self.scale = scale

        panel_w = int(min(width * 0.94, max(480, 820 * scale)))
        panel_w = max(panel_w, int(320 * scale))
        panel_w = min(panel_w, max(280, width - int(24 * scale)))

        panel_h = int(min(height * 0.92, max(480, 700 * scale)))
        panel_h = max(panel_h, int(340 * scale))
        panel_h = min(panel_h, max(280, height - int(24 * scale)))

        panel_x = (width - panel_w) // 2
        panel_y = (height - panel_h) // 2

        pad = max(8, int(18 * scale))
        title_h = max(24, int(34 * scale))
        btn_h = max(28, int(38 * scale))

        inner_x = panel_x + pad
        inner_w = panel_w - pad * 2

        label_w = max(60, int(110 * scale))
        gap = max(4, int(10 * scale))

        sb_w = max(4, int(8 * scale))
        sb_gap = max(4, int(8 * scale))

        input_x = inner_x + label_w + gap
        input_w = inner_w - label_w - gap - sb_w - sb_gap
        
        if input_w < 80:
            input_w = max(80, input_w)

        buttons_y = panel_y + panel_h - pad - btn_h
        self._buttons_y = buttons_y

        message_h = int(22 * scale)

        self._panel_rect = (panel_x, panel_y, panel_w, panel_h)
        self._title_rect = (panel_x, panel_y + pad, panel_w, title_h)

        content_y = panel_y + pad + title_h + int(8 * scale)
        content_h = buttons_y - content_y - message_h - int(4 * scale)
        if content_h < 20:
            content_h = max(20, buttons_y - content_y)
        self._content_rect = (inner_x, content_y, inner_w, content_h)

        # 行高
        row_h = max(24, int(34 * scale))
        row_gap = max(3, int(6 * scale))
        group_title_h = max(20, int(26 * scale))
        group_gap = max(6, int(12 * scale))

        # 字体度量
        base_font = int(self.font_sizes.get("message", max(16, int(18 * scale))))
        max_from_h = max(11, int(row_h * 0.6))
        input_font_size = min(base_font, max_from_h)
        input_pad = max(3, int(6 * scale))
        font = self.get_font(input_font_size)
        ascent, descent = self._measure_font(font)
        line_spacing = max(1, int(2 * scale))

        self._input_font_size = input_font_size
        self._input_pad = input_pad
        self._ascent = ascent
        self._descent = descent
        self._line_h = ascent + descent + line_spacing

        # 先算总内容高度
        groups_seen: List[str] = []
        total_h = 0
        for _key, _label, group, _kind, _spec in FIELD_DEFS:
            if group not in groups_seen:
                groups_seen.append(group)
                if len(groups_seen) > 1:
                    total_h += group_gap
                total_h += group_title_h
        total_h += len(FIELD_DEFS) * row_h + (len(FIELD_DEFS) - 1) * row_gap
        self._content_h = total_h
        self._max_scroll_y = max(0, total_h - content_h)
        self._scroll_y = max(0, min(self._scroll_y, self._max_scroll_y))

        # 计算每行的绝对 y（未滚动前）
        self._group_rects = {}
        self._row_rects = []

        y_abs = 0
        groups_seen = []
        for key, label, group, kind, spec in FIELD_DEFS:
            if group not in groups_seen:
                groups_seen.append(group)
                if len(groups_seen) > 1:
                    y_abs += group_gap
                self._group_rects[group] = (inner_x, content_y + y_abs, inner_w, group_title_h)
                y_abs += group_title_h

            label_rect = (inner_x, content_y + y_abs, label_w, row_h)
            field_x = input_x
            field_w = input_w
            field_rect = (field_x, content_y + y_abs, field_w, row_h)

            self._row_rects.append({
                "key": key,
                "label": label,
                "group": group,
                "kind": kind,
                "spec": spec,
                "label_rect": label_rect,
                "field_rect": field_rect,
                "y_abs": y_abs,
                "row_h": row_h,
            })
            y_abs += row_h + row_gap

        # 滚动条区域（右侧细条）
        self._scrollbar_rect = (
            inner_x + inner_w - sb_w,
            content_y,
            sb_w,
            content_h,
        ) if self._max_scroll_y > 0 else None

        # 底部按钮
        cancel_w = max(60, int(88 * scale))
        save_w = max(60, int(88 * scale))
        spacing = max(6, int(12 * scale))
        cancel_x = panel_x + panel_w - pad - cancel_w
        save_x = cancel_x - spacing - save_w
        self._cancel_rect = (cancel_x, buttons_y, cancel_w, btn_h)
        self._save_rect = (save_x, buttons_y, save_w, btn_h)

        # 重建 InputBox（宽度变化时）
        layout_key = (width, height, scale, input_font_size, input_w, row_h)
        if layout_key != self._layout_key:
            self._rebuild_input_boxes(input_w - input_pad * 2, input_pad,
                                      input_font_size, line_spacing)
            self._layout_key = layout_key

    def _rebuild_input_boxes(self, box_w, padding, font_size, line_spacing) -> None:
        box_w = max(10, box_w)
        for key, _label, _group, kind, _spec in FIELD_DEFS:
            if kind == "enum":
                continue
            old_box = self._input_boxes.get(key)
            old_text = old_box.get_text() if old_box else ""

            new_box = InputBox(
                width=box_w,
                font_getter=self.get_font,
                line_spacing=line_spacing,
                padding=padding,
                visible_lines=1,
                font_size=font_size,
            )
            new_box.set_text(old_text)
            new_box.set_cursor(len(old_text))
            self._input_boxes[key] = new_box

    # ============================================================
    # 渲染
    # ============================================================
    def render(self, img: Image.Image, draw: ImageDraw.ImageDraw, ctx: Any):
        if not self.visible:
            return None

        self._sync_layout(ctx.width, ctx.height, ctx.scale)

        # 遮罩
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 60))
        img.paste(overlay, (0, 0), overlay)

        px, py, pw, ph = self._panel_rect
        radius = max(6, int(12 * self.scale))
        draw.rounded_rectangle(
            self._rect_xyxy(self._panel_rect),
            radius=radius,
            fill=(24, 32, 38, 240),
            outline=(225, 225, 230, 130),
            width=max(1, int(2 * self.scale)),
        )

        # 标题
        title_font = self.get_font(
            int(self.font_sizes.get("title", max(18, int(24 * self.scale))))
        )
        self._draw_center_text(draw, self._title_rect, DEFAULT_TITLE, title_font,
                               (250, 250, 250, 255))

        # 内容区裁剪
        self._draw_content(draw)

        # 按钮
        self._draw_button(draw, self._save_rect, "保存", is_primary=True)
        self._draw_button(draw, self._cancel_rect, "取消", is_primary=False)

        # 提示 / 错误
        msg = self.error or self.message
        if msg:
            msg_font = self.get_font(max(12, int(self._input_font_size * 0.80)))
            color = (255, 120, 120, 255) if self.error else (190, 255, 190, 255)
            try:
                bbox = msg_font.getbbox(msg)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = self._text_width(msg_font, msg)
            msg_x = px + (pw - int(tw)) // 2
            msg_y = self._buttons_y - int(22 * self.scale)
            draw.text((msg_x, msg_y), msg, font=msg_font, fill=color)

        return None

    def _draw_content(self, draw: ImageDraw.ImageDraw) -> None:
        cx, cy, cw, ch = self._content_rect
        clip_top = cy
        clip_bottom = cy + ch

        # 分组标题
        group_title_font = self.get_font(
            max(11, int(self._input_font_size * 0.9))
        )
        for group, rect in self._group_rects.items():
            gx, gy, gw, gh = rect
            draw_y = gy - self._scroll_y
            if draw_y + gh < clip_top or draw_y > clip_bottom:
                continue
            label = GROUP_LABELS.get(group, group)
            draw.text(
                (gx, draw_y + max(0, (gh - (self._ascent + self._descent)) // 2)),
                label,
                font=group_title_font,
                fill=(180, 200, 230, 240),
            )

        # 每一行
        label_font = self.get_font(self._input_font_size)
        for row in self._row_rects:
            y_abs = row["y_abs"]
            draw_y = self._content_rect[1] + y_abs - self._scroll_y
            row_h = row["row_h"]
            if draw_y + row_h < clip_top or draw_y > clip_bottom:
                continue

            key = row["key"]
            label = row["label"]
            kind = row["kind"]
            spec = row["spec"]

            # 标签
            lx = row["label_rect"][0]
            lw = row["label_rect"][2]
            text_w = self._text_width(label_font, label)
            draw.text(
                (lx + lw - text_w, draw_y + max(0, (row_h - (self._ascent + self._descent)) // 2)),
                label,
                font=label_font,
                fill=(210, 210, 210, 230),
            )

            # 字段
            fx, _, fw, _ = row["field_rect"]
            if kind == "enum":
                self._draw_enum_button(draw, (fx, draw_y, fw, row_h), key, spec)
            else:
                box = self._input_boxes.get(key)
                active = (
                    self.focus is not None
                    and self.focus[0] == "input"
                    and self.focus[1] == key
                )
                self._draw_input(draw, box, (fx, draw_y, fw, row_h), active)

        # 滚动条
        if self._scrollbar_rect is not None and self._max_scroll_y > 0:
            sx, sy, sw, sh = self._scrollbar_rect
            draw.rounded_rectangle(
                (sx, sy, sx + sw, sy + sh),
                radius=sw // 2,
                fill=(60, 70, 80, 180),
            )
            ratio = self._content_rect[3] / self._content_h
            thumb_h = max(16, int(sh * ratio))
            thumb_y = sy + int((sh - thumb_h) * (self._scroll_y / self._max_scroll_y))
            draw.rounded_rectangle(
                (sx, thumb_y, sx + sw, thumb_y + thumb_h),
                radius=sw // 2,
                fill=(160, 180, 210, 220),
            )

    def _draw_enum_button(self, draw, rect, key, spec):
        x, y, w, h = rect
        radius = max(4, int(8 * self.scale))
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=radius,
            fill=(240, 235, 220, 245),
            outline=(150, 130, 100, 200),
            width=max(1, int(1 * self.scale)),
        )
        font = self.get_font(self._input_font_size)
        value = self._enum_values.get(key, spec["default"])
        label = str(value)
        text_h = self._ascent + self._descent
        text_top = y + max(0, (h - text_h) // 2)
        draw.text(
            (x + self._input_pad, text_top),
            label,
            font=font,
            fill=(40, 40, 30, 255),
        )
        arrow = "[ 切换 ]"
        try:
            arrow_w = self._text_width(font, arrow)
        except Exception:
            arrow_w = 10
        ax = x + w - self._input_pad - arrow_w
        draw.text((ax, text_top), arrow, font=font, fill=(120, 120, 100, 220))

    def _draw_input(self, draw, box, rect, active):
        if box is None or rect is None:
            return
        x, y, w, h = rect
        radius = max(4, int(8 * self.scale))
        border_color = (120, 180, 255, 235) if active else (130, 120, 100, 200)
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=radius,
            fill=(248, 244, 232, 245),
            outline=border_color,
            width=max(1, int(2 * self.scale) if active else int(1 * self.scale)),
        )

        font = box._font
        pad = box.padding
        text_x = x + pad
        line_h = self._line_h
        text_h = self._ascent + self._descent

        start = box.scroll_offset
        end = min(len(box.lines), start + box.visible_lines)
        visible_lines = end - start
        total_text_h = visible_lines * line_h
        y_center_offset = max(0, (h - pad * 2 - total_text_h) // 2)
        text_y = y + pad + y_center_offset

        selection = box.get_selection()
        if selection is not None:
            sel_start, sel_end = selection
            if sel_start > sel_end:
                sel_start, sel_end = sel_end, sel_start
        else:
            sel_start = sel_end = None

        for i in range(start, end):
            row_top = text_y + (i - start) * line_h
            if row_top + line_h > y + h - pad:
                break

            text_top = row_top + max(0, (line_h - text_h) // 2)
            line = box.lines[i]
            line_abs_start = box.get_line_abs_start(i)
            line_abs_end = line_abs_start + len(line)

            segments = []
            if sel_start is not None and sel_end is not None and sel_start < sel_end:
                seg1_end = max(line_abs_start, min(line_abs_end, sel_start))
                if seg1_end > line_abs_start:
                    segments.append((line_abs_start, seg1_end, False))
                seg2_start = max(line_abs_start, sel_start)
                seg2_end = min(line_abs_end, sel_end)
                if seg2_end > seg2_start:
                    segments.append((seg2_start, seg2_end, True))
                seg3_start = max(line_abs_start, sel_end)
                if seg3_start < line_abs_end:
                    segments.append((seg3_start, line_abs_end, False))
            else:
                segments.append((line_abs_start, line_abs_end, False))

            x_offset = text_x
            y_baseline = text_top + self._ascent

            for seg_start, seg_end, is_selected in segments:
                sub_text = line[seg_start - line_abs_start: seg_end - line_abs_start]
                if not sub_text:
                    continue
                w_text = self._text_width(font, sub_text)

                if is_selected:
                    draw.rectangle(
                        (x_offset, row_top, x_offset + w_text, row_top + line_h),
                        fill=(30, 100, 255, 160),
                    )
                    text_color = (255, 255, 255, 255)
                else:
                    text_color = (40, 40, 30, 255)

                draw.text((x_offset, y_baseline), sub_text, font=font, fill=text_color)
                x_offset += w_text

            if active and i == box.cursor_line:
                prefix = line[:box.cursor_col]
                cursor_x = text_x + self._text_width(box._font, prefix)
                cursor_y = text_top + int(6 * self.scale)
                cursor_h = max(6, text_h - int(2 * self.scale))
                if box.get_cursor_visible():
                    cursor_w = max(1, int(2 * self.scale))
                    draw.rectangle(
                        (cursor_x, cursor_y, cursor_x + cursor_w, cursor_y + cursor_h),
                        fill=(30, 30, 30, 255),
                    )

    def _draw_button(self, draw, rect, text, is_primary):
        if not rect:
            return
        x, y, w, h = rect
        radius = max(4, int(8 * self.scale))
        if is_primary:
            fill = (70, 110, 190, 235)
            outline = (200, 220, 255, 180)
        else:
            fill = (90, 90, 110, 235)
            outline = (220, 220, 230, 140)
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=radius,
            fill=fill,
            outline=outline,
            width=max(1, int(1 * self.scale)),
        )
        font = self.get_font(max(14, int(18 * self.scale)))
        self._draw_center_text(draw, rect, text, font, (255, 255, 255, 255))

    def _draw_center_text(self, draw, rect, text, font, fill):
        x, y, w, h = rect
        cx = x + w // 2
        cy = y + h // 2
        try:
            draw.text((cx, cy), text, font=font, fill=fill, anchor="mm")
            return
        except Exception:
            pass
        try:
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = x + (w - tw) // 2 - bbox[0]
            ty = y + (h - th) // 2 - bbox[1]
        except Exception:
            tw, th = font.getsize(text)
            tx = x + (w - tw) // 2
            ty = y + (h - th) // 2
        draw.text((tx, ty), text, font=font, fill=fill)

    # ============================================================
    # 命中测试
    # ============================================================
    def hit_test(self, x: int, y: int, ctx: Any = None, is_click: bool = False) -> Dict[str, Any]:
        if not self.visible:
            return {"area": "empty"}

        if self._rect_contains(self._save_rect, x, y):
            return {"area": "model_params_save"}
        if self._rect_contains(self._cancel_rect, x, y):
            return {"area": "model_params_cancel"}

        # 内容区
        if self._rect_contains(self._content_rect, x, y):
            if self._rect_contains(self._scrollbar_rect, x, y):
                return {"area": "model_params_scrollbar"}
            for row in self._row_rects:
                draw_y = self._content_rect[1] + row["y_abs"] - self._scroll_y
                field_rect = (
                    row["field_rect"][0],
                    draw_y,
                    row["field_rect"][2],
                    row["field_rect"][3],
                )
                if self._rect_contains(field_rect, x, y):
                    return {
                        "area": "model_params_field",
                        "key": row["key"],
                        "kind": row["kind"],
                    }
            return {"area": "model_params_content"}

        if self._rect_contains(self._panel_rect, x, y):
            return {"area": "model_params_panel"}

        return {"area": "model_params_modal"}

    # ============================================================
    # 鼠标
    # ============================================================
    def handle_mouse_button(self, button: int, action: int, mods: int, x: int, y: int) -> bool:
        if not self.visible:
            return False
        if glfw is None:
            return True
        if action != glfw.PRESS:
            return True
        if button != glfw.MOUSE_BUTTON_LEFT:
            return True

        if self._rect_contains(self._save_rect, x, y):
            self.save()
            return True
        if self._rect_contains(self._cancel_rect, x, y):
            self.close()
            return True

        if self._rect_contains(self._scrollbar_rect, x, y):
            self._scroll_drag_active = True
            self._scroll_to_y(y)
            return True

        if self._rect_contains(self._content_rect, x, y):
            for row in self._row_rects:
                draw_y = self._content_rect[1] + row["y_abs"] - self._scroll_y
                field_rect = (
                    row["field_rect"][0],
                    draw_y,
                    row["field_rect"][2],
                    row["field_rect"][3],
                )
                if self._rect_contains(field_rect, x, y):
                    key = row["key"]
                    kind = row["kind"]

                    if kind == "enum":
                        self._cycle_enum(key, row["spec"])
                        return True
                    else:
                        self.focus = ("input", key)
                        box = self._input_boxes.get(key)
                        if box is not None:
                            idx = self._get_index_by_pos(box, field_rect, x, y)
                            box.clear_selection()
                            box.set_cursor(idx)
                            box.reset_cursor_blink()
                        self._request(True)
                        return True
            self.focus = None
            self._request(True)
            return True

        return True

    def _scroll_to_y(self, y):
        sx, sy, sw, sh = self._scrollbar_rect
        ratio = max(0.0, min(1.0, (y - sy) / max(1, sh)))
        self._scroll_y = int(ratio * self._max_scroll_y)
        self._request(True)

    def _cycle_enum(self, key, spec):
        options = spec["options"]
        current = self._enum_values.get(key, spec["default"])
        cycle = list(options)
        if current not in cycle:
            cycle.append(current)
        if current in cycle:
            next_value = cycle[(cycle.index(current) + 1) % len(cycle)]
        else:
            next_value = options[0]
        self._enum_values[key] = next_value
        self._request(True)

    def _get_index_by_pos(self, box, rect, x, y):
        if not box.lines:
            return 0
        pad = box.padding
        rel_y = y - (rect[1] + pad)
        if rel_y < 0:
            clicked_line = box.scroll_offset
        else:
            clicked_line = box.scroll_offset + int(rel_y // self._line_h)
        clicked_line = max(0, min(clicked_line, len(box.lines) - 1))
        target_x = x - (rect[0] + pad)
        line_text = box.lines[clicked_line]
        line_start_abs = box.get_line_abs_start(clicked_line)
        char_idx = 0
        if target_x > 0:
            for i in range(len(line_text)):
                prefix = line_text[:i + 1]
                if self._text_width(box._font, prefix) > target_x:
                    break
                char_idx = i + 1
        abs_idx = line_start_abs + char_idx
        try:
            total = len(box.text)
        except Exception:
            total = abs_idx
        return max(0, min(abs_idx, total))

    def handle_scroll(self, x: int, y: int, x_offset: float, y_offset: float) -> bool:
        if not self.visible:
            return False
        if not self._rect_contains(self._content_rect, x, y):
            return True
        if self._max_scroll_y <= 0:
            return True
        step = max(20, int(40 * self.scale))
        self._scroll_y -= int(y_offset * step)
        self._scroll_y = max(0, min(self._scroll_y, self._max_scroll_y))
        self._request(True)
        return True

    # ============================================================
    # 键盘
    # ============================================================
    def handle_key(self, key: int, scancode: int, action: int, mods: int) -> bool:
        if not self.visible:
            return False
        if glfw is None:
            return True
        if action == glfw.RELEASE:
            return False

        mods = mods or 0
        ctrl = bool(mods & glfw.MOD_CONTROL)

        if key == glfw.KEY_ESCAPE:
            self.close()
            return True
        if ctrl and key == glfw.KEY_S:
            self.save()
            return True
        if key in (glfw.KEY_ENTER, getattr(glfw, "KEY_KP_ENTER", -1)) and not ctrl:
            self.save()
            return True

        box = self._focused_box()
        if box is None:
            if key == glfw.KEY_UP:
                self.handle_scroll(0, 0, 0, 1)
                return True
            if key == glfw.KEY_DOWN:
                self.handle_scroll(0, 0, 0, -1)
                return True
            return True

        # 输入框快捷键（复制/粘贴/剪切/全选）
        if ctrl:
            if key == glfw.KEY_A:
                box.select_all()
                self._request(True)
                return True
            if key == glfw.KEY_C:
                from services import ClipboardService
                text = box.copy_selected()
                if text:
                    ClipboardService.set_text(text)
                return True
            if key == glfw.KEY_X:
                from services import ClipboardService
                text = box.cut_selected()
                if text:
                    ClipboardService.set_text(text)
                self._request(True)
                return True
            if key == glfw.KEY_V:
                from services import ClipboardService
                text = ClipboardService.get_text()
                if text:
                    box.paste(text)
                self._request(True)
                return True

        if key == glfw.KEY_BACKSPACE:
            box.delete_backward()
            self._request(True)
            return True
        if key == glfw.KEY_DELETE:
            box.delete_forward()
            self._request(True)
            return True
        if key == glfw.KEY_LEFT:
            box.move_cursor_left()
            self._request(True)
            return True
        if key == glfw.KEY_RIGHT:
            box.move_cursor_right()
            self._request(True)
            return True
        if key == glfw.KEY_HOME:
            box.set_cursor(box.get_line_abs_start(box.cursor_line))
            self._request(True)
            return True
        if key == glfw.KEY_END:
            box.set_cursor(box.get_line_abs_start(box.cursor_line) + len(box.lines[box.cursor_line]))
            self._request(True)
            return True

        return True

    def handle_char(self, text: str) -> bool:
        if not self.visible or not text:
            return False
        box = self._focused_box()
        if box is None:
            return False
        text = text.replace("\r", "").replace("\n", "")
        if not text:
            return False
        # 允许数字、小数点、负号
        filtered = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
        if not filtered:
            return False
        box.insert_text(filtered)
        self._request(True)
        return True

    def update_cursor_blink(self, current_time: float) -> bool:
        if not self.visible:
            return False
        box = self._focused_box()
        if box is None:
            return False
        before = box.get_cursor_visible()
        box.update_cursor_blink(current_time)
        after = box.get_cursor_visible()
        if before != after:
            self._request(True)
            return True
        return False
