# chat/search_api_editor.py
import os
import copy
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import glfw
except Exception:
    glfw = None

from PIL import Image, ImageDraw

from .input_box import InputBox
from services import ClipboardService

try:
    from internet_settings import InternetSettings
except Exception:
    InternetSettings = None


DEFAULT_TITLE = "搜索 API 设置"
API_COUNT = 3

PROTOCOL_PRESETS = [
    {
        "id": "tavily",
        "label": "Tavily（内置）",
        "protocol": "tavily",
        "request": {},
        "response": {},
        "default_base": "https://api.tavily.com",
        "slots": ["api_1"],
    },
    {
        "id": "tavily_compat",
        "label": "Tavily 兼容 HTTP",
        "protocol": "generic_http",
        "request": {
            "method": "POST",
            "url": "{api_base}",
            "auth": {"type": "bearer"},
            "json": {"query": "{query}", "max_results": "{max_results}"},
        },
        "response": {
            "results_path": "results",
            "title": "title",
            "url": "url",
            "snippet": ["content", "snippet"],
        },
        "default_base": "https://api.tavily.com",
        "slots": ["api_2", "api_3"],
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "protocol": "generic_http",
        "request": {
            "method": "POST",
            "url": "{api_base}",
            "auth": {"type": "bearer"},
            "json": {"query": "{query}", "count": "{max_results}"},
        },
        "response": {
            "results_path": "results",
            "title": "title",
            "url": "url",
            "snippet": ["snippet", "summary", "content"],
        },
        "default_base": "https://api.deepseek.com/v1/tools/search",
        "slots": ["api_1", "api_2", "api_3"],
    },
    {
        "id": "baidu_web_summary",
        "label": "百度 web_summary",
        "protocol": "generic_http",
        "request": {
            "method": "POST",
            "url": "{api_base}",
            "auth": {"type": "bearer"},
            "json": {
                "messages": [{"role": "user", "content": "{query}"}],
                "resource_type_filter": {"web": [{"top_k": "{max_results}"}]},
            },
        },
        "response": {
            "results_path": ["references", "search_results", "data"],
            "title": "title",
            "url": "url",
            "snippet": ["content", "snippet", "summary"],
        },
        "default_base": "https://qianfan.baidubce.com/v2/ai_search/web_summary",
        "slots": ["api_1", "api_2", "api_3"],
    },
    {
        "id": "custom",
        "label": "自定义 HTTP",
        "protocol": "generic_http",
        "request": {
            "method": "POST",
            "url": "{api_base}",
            "auth": {"type": "bearer"},
            "json": {"query": "{query}", "count": "{max_results}"}
        },
        "response": {
            "results_path": "results",
            "title": "title",
            "url": "url",
            "snippet": ["snippet", "content", "summary"]
        },
        "default_base": "",
        "slots": ["api_1", "api_2", "api_3"],
    },
]

PROTOCOL_BY_ID = {p["id"]: p for p in PROTOCOL_PRESETS}

class SearchApiEditor:

    def __init__(
        self,
        get_font: Callable[[int], Any],
        scale: float,
        width: int,
        height: int,
        font_sizes: Optional[Dict[str, int]] = None,
        request_update: Optional[Callable[..., None]] = None,
        mark_dirty: Optional[Callable[[], None]] = None,
        add_message: Optional[Callable[[str, str], None]] = None,
    ):
        self.get_font = get_font
        self.scale = scale
        self.width = width
        self.height = height
        self.font_sizes = font_sizes or {}

        self.request_update_callback = request_update
        self.mark_dirty_callback = mark_dirty
        self.add_message = add_message

        self.visible = False
        self.focus: Optional[Tuple[str, int]] = None  # ("link"|"key", 0..2)
        self.message = ""
        self.error = ""

        self.entries: List[Dict[str, Any]] = []
        self._panel_rect: Optional[Tuple[int, int, int, int]] = None
        self._title_rect: Optional[Tuple[int, int, int, int]] = None
        self._row_rects: List[Dict[str, Tuple[int, int, int, int]]] = []
        self._save_rect: Optional[Tuple[int, int, int, int]] = None
        self._cancel_rect: Optional[Tuple[int, int, int, int]] = None
        self._buttons_y = 0

        self._input_font_size = 20
        self._input_pad = 8
        self._ascent = 16
        self._descent = 5
        self._line_h = 24

        self._layout_key: Optional[Tuple] = None

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
        field, idx = self.focus
        if idx < 0 or idx >= len(self.entries):
            return None
        entry = self.entries[idx]
        if field == "link":
            return entry.get("link_box")
        if field == "key":
            return entry.get("key_box")
        return None

    def _is_password_field(self) -> bool:
        return self.focus is not None and self.focus[0] == "key"

    def _reset_cursor_blink(self):
        for entry in self.entries:
            for key in ("link_box", "key_box"):
                box = entry.get(key)
                if box is not None:
                    try:
                        box.reset_cursor_blink()
                    except Exception:
                        pass

    def _clear_key_boxes(self):
        for entry in self.entries:
            box = entry.get("key_box")
            if box is not None:
                try:
                    box.set_text("")
                except Exception:
                    pass

    def _validate_focus(self):
        if not self._row_rects:
            self.focus = None
            return
        n = len(self._row_rects)
        if not self.focus:
            self.focus = ("link", 0)
            return
        field, idx = self.focus
        if idx >= n:
            self.focus = ("link", n - 1)

    # ============================================================
    # 打开 / 关闭 / 保存
    # ============================================================
    def open(self) -> None:
        if self.visible:
            return
        self._load_entries()
        self._layout_key = None
        self._sync_layout(self.width, self.height, self.scale)
        self._validate_focus()
        self.message = ""
        self.error = ""
        self.visible = True
        self._reset_cursor_blink()
        self._request(True)

    def close(self) -> None:
        if not self.visible:
            return
        self.visible = False
        self._clear_key_boxes()
        self._request(True)

    def _make_empty_entry(self, i: int) -> Dict[str, Any]:
        api_id = f"api_{i + 1}"
        default_preset_id = "tavily" if api_id == "api_1" else "custom"
        preset = PROTOCOL_BY_ID.get(default_preset_id, PROTOCOL_BY_ID["custom"])
        return {
            "id": api_id,
            "display_name": f"API {i + 1}",
            "api_base": "",
            "api_key_text": "",
            "has_key": False,
            "enabled": False,
            "protocol": preset["protocol"],
            "request": copy.deepcopy(preset["request"]),
            "response": copy.deepcopy(preset["response"]),
            "preset_id": default_preset_id,
            "link_box": None,
            "key_box": None,
        }
    
    def _load_entries(self) -> None:
        self.entries = []
        if InternetSettings is None:
            for i in range(API_COUNT):
                self.entries.append(self._make_empty_entry(i))
            return
        try:
            ui_data = InternetSettings.get_instance().get_search_api_ui()
        except Exception as e:
            print(f"[SearchApiEditor] 读取配置失败: {e}")
            ui_data = []
        by_id: Dict[str, Dict[str, Any]] = {}
        for item in ui_data:
            if isinstance(item, dict) and item.get("id"):
                by_id[item["id"]] = item

        for i in range(API_COUNT):
            api_id = f"api_{i + 1}"
            item = by_id.get(api_id)
            if item is None:
                self.entries.append(self._make_empty_entry(i))
                continue

            full_cfg = None
            try:
                full_cfg = InternetSettings.get_instance().get_api_config(api_id)
            except Exception:
                pass

            protocol = ""
            request_cfg: Dict[str, Any] = {}
            response_cfg: Dict[str, Any] = {}
            if full_cfg:
                protocol = str(full_cfg.get("protocol", "")).strip().lower()
                if isinstance(full_cfg.get("request"), dict):
                    request_cfg = copy.deepcopy(full_cfg["request"])
                if isinstance(full_cfg.get("response"), dict):
                    response_cfg = copy.deepcopy(full_cfg["response"])

            preset_id = self._detect_preset(api_id, protocol, request_cfg, response_cfg)

            self.entries.append({
                "id": api_id,
                "display_name": str(item.get("display_name") or f"API {i + 1}"),
                "api_base": str(item.get("api_base", "")),
                "api_key_text": "",
                "has_key": bool(item.get("has_key", False)),
                "enabled": bool(item.get("enabled", False)),
                "protocol": protocol,
                "request": request_cfg,
                "response": response_cfg,
                "preset_id": preset_id,
                "link_box": None,
                "key_box": None,
            })

    @staticmethod
    def _signature(obj) -> str:
        try:
            return json.dumps(obj, sort_keys=True, ensure_ascii=False)
        except Exception:
            return ""

    def _detect_preset(self, api_id, protocol, request_cfg, response_cfg) -> str:
        available = [p for p in PROTOCOL_PRESETS if api_id in p.get("slots", [])]
        if not available:
            return "custom"

        if protocol in ("", "tavily"):
            for p in available:
                if p["protocol"] == "tavily":
                    return p["id"]
            for p in available:
                if p["id"] == "tavily_compat":
                    return p["id"]
            return "custom"

        if protocol == "generic_http":
            if not request_cfg and not response_cfg:
                return "custom"
            req_sig = self._signature(request_cfg)
            resp_sig = self._signature(response_cfg)
            for p in available:
                if p["protocol"] != "generic_http":
                    continue
                if p["id"] == "custom":
                    continue
                if (self._signature(p["request"]) == req_sig
                        and self._signature(p["response"]) == resp_sig):
                    return p["id"]
            return "custom"

        return "custom"

    def _cycle_protocol(self, i: int) -> None:
        if i < 0 or i >= len(self.entries):
            return
        entry = self.entries[i]
        api_id = entry.get("id", "")
        available = [p for p in PROTOCOL_PRESETS if api_id in p.get("slots", [])]
        if not available:
            return

        current_id = entry.get("preset_id") or "custom"
        ids = [p["id"] for p in available]
        if current_id not in ids:
            current_id = ids[0]
        next_idx = (ids.index(current_id) + 1) % len(ids)
        next_preset = available[next_idx]

        # 切换协议
        entry["preset_id"] = next_preset["id"]
        entry["protocol"] = next_preset["protocol"]
        entry["request"] = copy.deepcopy(next_preset["request"])
        entry["response"] = copy.deepcopy(next_preset["response"])

        new_base = next_preset.get("default_base", "")
        link_box = entry.get("link_box")
        if link_box:
            link_box.set_text(new_base)
        entry["api_base"] = new_base

        self._request(True)
        
    def save(self) -> None:
        if not self.visible:
            return
        if InternetSettings is None:
            self.error = "联网设置模块不可用"
            self._request(True)
            return
        try:
            payload = []
            for entry in self.entries:
                link_box = entry.get("link_box")
                key_box = entry.get("key_box")
                link_text = link_box.get_text().strip() if link_box else entry.get("api_base", "")
                key_text = key_box.get_text() if key_box else ""
                item = {
                    "id": entry["id"],
                    "api_base": link_text,
                    "enabled": bool(entry.get("enabled", False)),
                }
                if key_text != "":
                    item["api_key"] = key_text

                item = {
                    "id": entry["id"],
                    "api_base": link_text,
                    "enabled": bool(entry.get("enabled", False)),
                    "protocol": entry.get("protocol", ""),
                    "request": copy.deepcopy(entry.get("request") or {}),
                    "response": copy.deepcopy(entry.get("response") or {}),
                }
                
                payload.append(item)

            ok, err = InternetSettings.get_instance().save_search_apis(payload)
            if not ok:
                self.error = err or "保存失败"
                self._request(True)
                return

            self.visible = False
            self.error = ""
            self.message = "搜索 API 已保存"
            self._clear_key_boxes()
            if self.add_message is not None:
                try:
                    self.add_message("shizuku", "搜索 API 已保存，下一轮对话生效。")
                except Exception:
                    pass
            self._request(True)

        except Exception as e:
            self.error = f"保存失败：{e}"
            self._request(True)

    # ============================================================
    # 布局
    # ============================================================
    def _sync_layout(self, width: int, height: int, scale: float) -> None:
        self.width = width
        self.height = height
        self.scale = scale

        panel_w = int(min(width * 0.94, max(460, 820 * scale)))
        panel_w = max(panel_w, int(320 * scale))
        panel_w = min(panel_w, max(280, width - int(24 * scale)))

        panel_h = int(min(height * 0.92, max(460, 680 * scale)))
        panel_h = max(panel_h, int(340 * scale))
        panel_h = min(panel_h, max(280, height - int(24 * scale)))

        panel_x = (width - panel_w) // 2
        panel_y = (height - panel_h) // 2

        pad = max(8, int(18 * scale))
        title_h = max(24, int(34 * scale))
        btn_h = max(28, int(38 * scale))

        inner_x = panel_x + pad
        inner_w = panel_w - pad * 2

        label_w = max(40, int(56 * scale))
        gap = max(4, int(8 * scale))
        status_w = max(54, int(88 * scale))

        input_x = inner_x + label_w + gap
        input_w = inner_w - label_w - gap
        if input_w < 80:
            input_w = max(80, input_w)

        buttons_y = panel_y + panel_h - pad - btn_h
        self._buttons_y = buttons_y

        message_h = int(22 * scale)

        self._panel_rect = (panel_x, panel_y, panel_w, panel_h)
        self._title_rect = (panel_x, panel_y + pad, panel_w, title_h)

        y_start = panel_y + pad + title_h + int(8 * scale)
        available = buttons_y - y_start - message_h - int(4 * scale)

        header_h = int(26 * scale)
        line_input_h = int(34 * scale)
        line_gap = int(4 * scale)
        group_gap = int(12 * scale)

        min_header = max(16, int(18 * scale))
        min_input = max(20, int(22 * scale))
        min_line_gap = max(1, int(2 * scale))
        min_group_gap = max(3, int(4 * scale))

        def item_h_fn(h, ih, lg):
            return h + lg + ih + lg + ih + lg + ih
        
        def total_fn(h, ih, lg, gg):
            item = item_h_fn(h, ih, lg)
            return API_COUNT * item + (API_COUNT - 1) * gg

        total = total_fn(header_h, line_input_h, line_gap, group_gap)

        if total > available and available > 0:
            ratio = available / total
            header_h = max(min_header, int(header_h * ratio))
            line_input_h = max(min_input, int(line_input_h * ratio))
            line_gap = max(min_line_gap, int(line_gap * ratio))
            group_gap = max(min_group_gap, int(group_gap * ratio))
            total = total_fn(header_h, line_input_h, line_gap, group_gap)
            while total > available and group_gap > min_group_gap:
                group_gap -= 1
                total = total_fn(header_h, line_input_h, line_gap, group_gap)

        extra = available - total
        if extra > 0 and API_COUNT > 1:
            extra_per_gap = min(extra // (API_COUNT - 1), int(24 * scale))
            group_gap += extra_per_gap

        item_h = item_h_fn(header_h, line_input_h, line_gap)

        self._row_rects = []
        y = y_start
        for i in range(API_COUNT):
            if y + item_h > buttons_y - message_h:
                break

            header_rect = (inner_x, y, inner_w, header_h)
            header_label_rect = (inner_x, y, inner_w - status_w - gap, header_h)
            header_status_rect = (inner_x + inner_w - status_w, y, status_w, header_h)

            proto_y = y + header_h + line_gap
            proto_label_rect = (inner_x, proto_y, label_w, line_input_h)
            proto_rect = (input_x, proto_y, input_w, line_input_h)

            link_y = proto_y + line_input_h + line_gap
            link_label_rect = (inner_x, link_y, label_w, line_input_h)
            link_rect = (input_x, link_y, input_w, line_input_h)

            key_y = link_y + line_input_h + line_gap
            key_label_rect = (inner_x, key_y, label_w, line_input_h)
            key_rect = (input_x, key_y, input_w, line_input_h)

            self._row_rects.append({
                "header": header_rect,
                "header_label": header_label_rect,
                "header_status": header_status_rect,
                "protocol_label": proto_label_rect,
                "protocol": proto_rect,
                "link_label": link_label_rect,
                "link": link_rect,
                "key_label": key_label_rect,
                "key": key_rect,
            })
            y += item_h + group_gap
            
        cancel_w = max(60, int(88 * scale))
        save_w = max(60, int(88 * scale))
        spacing = max(6, int(12 * scale))
        cancel_x = panel_x + panel_w - pad - cancel_w
        save_x = cancel_x - spacing - save_w
        self._cancel_rect = (cancel_x, buttons_y, cancel_w, btn_h)
        self._save_rect = (save_x, buttons_y, save_w, btn_h)

        base_font = int(self.font_sizes.get("message", max(16, int(18 * scale))))
        max_from_h = max(11, int(line_input_h * 0.6))
        input_font_size = min(base_font, max_from_h)
        input_pad = max(3, int(6 * scale))
        font = self.get_font(input_font_size)
        ascent, descent = self._measure_font(font)
        line_spacing = max(1, int(2 * scale))
        line_h = ascent + descent + line_spacing

        self._input_font_size = input_font_size
        self._input_pad = input_pad
        self._ascent = ascent
        self._descent = descent
        self._line_h = line_h

        layout_key = (width, height, scale, input_font_size, input_w, line_input_h)
        if layout_key != self._layout_key:
            self._rebuild_input_boxes(input_w, input_pad, input_font_size, line_spacing)
            self._layout_key = layout_key
            
    def _rebuild_input_boxes(self, input_w, padding, font_size, line_spacing) -> None:
        box_w = max(10, input_w - padding * 2)
        for entry in self.entries:
            old_link_box = entry.get("link_box")
            old_key_box = entry.get("key_box")
            old_link = old_link_box.get_text() if old_link_box else entry.get("api_base", "")
            old_key = old_key_box.get_text() if old_key_box else ""

            entry["link_box"] = InputBox(
                width=box_w,
                font_getter=self.get_font,
                line_spacing=line_spacing,
                padding=padding,
                visible_lines=1,
                font_size=font_size,
            )
            entry["key_box"] = InputBox(
                width=box_w,
                font_getter=self.get_font,
                line_spacing=line_spacing,
                padding=padding,
                visible_lines=1,
                font_size=font_size,
            )
            entry["link_box"].set_text(old_link)
            entry["key_box"].set_text(old_key)
            
    # ============================================================
    # 渲染
    # ============================================================
    def render(self, img: Image.Image, draw: ImageDraw.ImageDraw, ctx: Any):
        if not self.visible:
            return None

        self._sync_layout(ctx.width, ctx.height, ctx.scale)
        self._validate_focus()

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

        title_font = self.get_font(
            int(self.font_sizes.get("title", max(18, int(24 * self.scale))))
        )
        self._draw_center_text(draw, self._title_rect, DEFAULT_TITLE, title_font, (250, 250, 250, 255))

        label_font = self.get_font(self._input_font_size)
        status_font = self.get_font(max(11, int(self._input_font_size * 0.75)))

        for i, row in enumerate(self._row_rects):
            entry = self.entries[i]
            self._draw_header(draw, row, entry, label_font, status_font)
            self._draw_field_label(draw, row["protocol_label"], "协议：", label_font)
            self._draw_protocol_button(draw, row["protocol"], entry)
            self._draw_field_label(draw, row["link_label"], "地址：", label_font)
            self._draw_input(
                draw, entry.get("link_box"), row["link"],
                self.focus == ("link", i), is_password=False,
            )
            self._draw_field_label(draw, row["key_label"], "密钥：", label_font)
            self._draw_input(
                draw, entry.get("key_box"), row["key"],
                self.focus == ("key", i), is_password=True,
            )
            
        self._draw_button(draw, self._save_rect, "保存")
        self._draw_button(draw, self._cancel_rect, "取消")

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

    def _draw_header(self, draw, row, entry, font, status_font):
        x, y, w, h = row["header_label"]
        text_h = self._ascent + self._descent
        text_top = y + max(0, (h - text_h) // 2)

        name = str(entry.get("display_name", ""))
        draw.text((x, text_top), name, font=font, fill=(240, 240, 240, 240))
        name_w = self._text_width(font, name)

        sep = " : "
        draw.text((x + name_w, text_top), sep, font=font, fill=(200, 200, 200, 220))
        sep_w = self._text_width(font, sep)

        if entry.get("enabled"):
            status_text = "【启用】"
            status_color = (120, 240, 140, 255)
        else:
            status_text = "【关闭】"
            status_color = (170, 170, 170, 210)
        draw.text(
            (x + name_w + sep_w, text_top),
            status_text, font=font, fill=status_color,
        )

        self._draw_status(draw, row["header_status"], entry, status_font)

    def _draw_field_label(self, draw, rect, text, font):
        x, y, w, h = rect
        text_h = self._ascent + self._descent
        text_top = y + max(0, (h - text_h) // 2)
        text_w = self._text_width(font, text)
        tx = x + w - text_w
        draw.text((tx, text_top), text, font=font, fill=(210, 210, 210, 230))
        
    def _draw_protocol_button(self, draw, rect, entry):
        if rect is None:
            return
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
        label = self._get_preset_label(entry)
        text_h = self._ascent + self._descent
        text_top = y + max(0, (h - text_h) // 2)
        draw.text(
            (x + self._input_pad, text_top),
            label, font=font, fill=(40, 40, 30, 255),
        )

        arrow = "[ 切换 ]"
        try:
            arrow_w = self._text_width(font, arrow)
        except Exception:
            arrow_w = 10
        ax = x + w - self._input_pad - arrow_w
        draw.text((ax, text_top), arrow, font=font, fill=(120, 120, 100, 220))

    def _get_preset_label(self, entry) -> str:
        preset_id = entry.get("preset_id", "custom")
        preset = PROTOCOL_BY_ID.get(preset_id)
        if preset:
            return preset["label"]
        return "自定义 HTTP"

    def _draw_status(self, draw, rect, entry, font):
        x, y, w, h = rect
        key_box = entry.get("key_box")
        key_text = key_box.get_text() if key_box else ""

        if key_text:
            text = "将更新"
            color = (255, 200, 100, 255)
        elif entry.get("has_key"):
            text = "已保存"
            color = (120, 220, 140, 255)
        else:
            text = "未配置"
            color = (160, 160, 160, 200)

        try:
            bbox = font.getbbox(text)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except Exception:
            text_w = self._text_width(font, text)
            text_h = int(self._ascent * 0.8)

        tx = x + w - text_w
        ty = y + max(0, (h - text_h) // 2) - int(2 * self.scale)
        draw.text((tx, ty), text, font=font, fill=color)
        
    def _draw_button(self, draw, rect, text):
        if not rect:
            return
        x, y, w, h = rect
        radius = max(4, int(8 * self.scale))
        if text == "保存":
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

    def _draw_input(self, draw, box, rect, active, is_password=False):
        if box is None or rect is None:
            return None

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

        start = box.scroll_offset
        end = min(len(box.lines), start + box.visible_lines)
        visible_lines = end - start
        total_text_h = visible_lines * self._line_h
        y_center_offset = max(0, (h - pad * 2 - total_text_h) // 2)
        text_y = y + pad + y_center_offset
        
        selection = box.get_selection()
        if selection is not None:
            sel_start, sel_end = selection
            if sel_start > sel_end:
                sel_start, sel_end = sel_end, sel_start
        else:
            sel_start = sel_end = None

        cursor_rect = None
        text_h = self._ascent + self._descent

        for i in range(start, end):
            row_top = text_y + (i - start) * self._line_h
            if row_top + self._line_h > y + h - pad:
                break

            text_top = row_top + max(0, (self._line_h - text_h) // 2)

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
                        (x_offset, row_top, x_offset + w_text, row_top + self._line_h),
                        fill=(30, 100, 255, 160),
                    )
                    text_color = (255, 255, 255, 255)
                else:
                    text_color = (40, 40, 30, 255)

                if is_password:
                    for ch_idx in range(len(sub_text)):
                        w_before = self._text_width(font, sub_text[:ch_idx])
                        w_after = self._text_width(font, sub_text[:ch_idx + 1])
                        cx = x_offset + (w_before + w_after) / 2
                        cy = row_top + self._line_h // 2
                        r = max(2, int(self._ascent * 0.14))
                        draw.ellipse(
                            (cx - r, cy - r, cx + r, cy + r),
                            fill=text_color,
                        )
                else:
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
                cursor_rect = (int(cursor_x), int(cursor_y), 0, int(cursor_h))

        return cursor_rect

    # ============================================================
    # 命中测试
    # ============================================================
    def hit_test(self, x: int, y: int, ctx: Any = None, is_click: bool = False) -> Dict[str, Any]:
        if not self.visible:
            return {"area": "empty"}

        if self._rect_contains(self._save_rect, x, y):
            return {"area": "search_api_editor_save"}
        if self._rect_contains(self._cancel_rect, x, y):
            return {"area": "search_api_editor_cancel"}

        for i, row in enumerate(self._row_rects):
            if self._rect_contains(row["header_label"], x, y):
                return {"area": "search_api_editor_toggle", "index": i}
            if self._rect_contains(row["protocol"], x, y):
                return {"area": "search_api_editor_protocol", "index": i}
            if self._rect_contains(row["link"], x, y):
                return {"area": "search_api_editor_link", "index": i}
            if self._rect_contains(row["key"], x, y):
                return {"area": "search_api_editor_key", "index": i}
            
        if self._rect_contains(self._panel_rect, x, y):
            return {"area": "search_api_editor_panel"}

        return {"area": "search_api_editor_modal"}
    
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

        for i, row in enumerate(self._row_rects):
            if self._rect_contains(row["header_label"], x, y):
                self.entries[i]["enabled"] = not self.entries[i].get("enabled", False)
                self._request(True)
                return True
            if self._rect_contains(row["link"], x, y):
                self.focus = ("link", i)
                box = self.entries[i].get("link_box")
                if box is not None:
                    idx = self._get_index_by_pos(box, row["link"], x, y)
                    box.clear_selection()
                    box.set_cursor(idx)
                    box.reset_cursor_blink()
                self._request(True)
                return True
            if self._rect_contains(row["key"], x, y):
                self.focus = ("key", i)
                box = self.entries[i].get("key_box")
                if box is not None:
                    idx = self._get_index_by_pos(box, row["key"], x, y)
                    box.clear_selection()
                    box.set_cursor(idx)
                    box.reset_cursor_blink()
                self._request(True)
                return True
            if self._rect_contains(row["header_label"], x, y):
                self.entries[i]["enabled"] = not self.entries[i].get("enabled", False)
                self._request(True)
                return True
            if self._rect_contains(row["protocol"], x, y):
                self._cycle_protocol(i)
                return True            
        return True

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
                prefix = line_text[: i + 1]
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
        return True

    # ============================================================
    # 键盘
    # ============================================================
    def _next_focus(self, reverse: bool = False):
        if not self._row_rects:
            return None
        n = len(self._row_rects)
        if self.focus is None:
            return ("link", 0)
        field, idx = self.focus
        if idx >= n:
            idx = n - 1
        if reverse:
            if field == "link":
                prev = idx - 1
                if prev < 0:
                    return ("key", n - 1)
                return ("key", prev)
            return ("link", idx)
        if field == "link":
            return ("key", idx)
        nxt = idx + 1
        if nxt >= n:
            return ("link", 0)
        return ("link", nxt)

    def handle_key(self, key: int, scancode: int, action: int, mods: int) -> bool:
        if not self.visible:
            return False
        if glfw is None:
            return True
        if action == glfw.RELEASE:
            return False

        mods = mods or 0
        ctrl = bool(mods & glfw.MOD_CONTROL)
        shift = bool(mods & glfw.MOD_SHIFT)

        if key == glfw.KEY_ESCAPE:
            self.close()
            return True

        if ctrl and key == glfw.KEY_S:
            self.save()
            return True

        if key == glfw.KEY_TAB:
            self.focus = self._next_focus(reverse=shift)
            self._reset_cursor_blink()
            self._request(True)
            return True

        if key in (glfw.KEY_ENTER, getattr(glfw, "KEY_KP_ENTER", -1)):
            if not ctrl:
                self.save()
                return True

        box = self._focused_box()
        if box is None:
            return True

        if ctrl:
            if key == glfw.KEY_A:
                box.select_all()
                self._request(True)
                return True
            if key == glfw.KEY_C:
                if not self._is_password_field():
                    text = box.copy_selected()
                    if text:
                        ClipboardService.set_text(text)
                return True
            if key == glfw.KEY_X:
                if not self._is_password_field():
                    text = box.cut_selected()
                    if text:
                        ClipboardService.set_text(text)
                    self._request(True)
                return True
            if key == glfw.KEY_V:
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
            line = box.cursor_line
            box.set_cursor(box.get_line_abs_start(line))
            self._request(True)
            return True
        if key == glfw.KEY_END:
            line = box.cursor_line
            box.set_cursor(box.get_line_abs_start(line) + len(box.lines[line]))
            self._request(True)
            return True
        if key == glfw.KEY_UP:
            self.focus = self._next_focus(reverse=True)
            self._reset_cursor_blink()
            self._request(True)
            return True
        if key == glfw.KEY_DOWN:
            self.focus = self._next_focus()
            self._reset_cursor_blink()
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
        box.insert_text(text)
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
