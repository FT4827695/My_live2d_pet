# chat/character_editor.py
import os
import json
import shutil
import time
from typing import Any, Callable, Dict, Optional, Tuple

try:
    import glfw
except Exception:
    glfw = None

from PIL import Image, ImageDraw

from .input_box import InputBox
from services import ClipboardService

try:
    import prompt
except Exception:
    prompt = None


DEFAULT_NAME = "雫"
DEFAULT_DESCRIPTION = "你是桌面助手，一个可爱、有一点点傲娇的二次元少女角色。"


# ============================================================
# 路径和缓存工具
# ============================================================

def _get_character_file_path() -> str:
    try:
        if prompt is not None:
            if hasattr(prompt, "get_character_file_path"):
                return prompt.get_character_file_path()
            if hasattr(prompt, "_CHARACTER_FILE"):
                return prompt._CHARACTER_FILE
    except Exception:
        pass

    # 兜底
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "character.json")


def _clear_character_cache() -> None:
    try:
        if prompt is None:
            return
        if hasattr(prompt, "clear_character_cache"):
            prompt.clear_character_cache()
        elif hasattr(prompt, "_load_character_data"):
            prompt._load_character_data.cache_clear()
    except Exception:
        pass


class CharacterEditor:

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
        refresh_ui: Optional[Callable[[], None]] = None,
    ):
        self.get_font = get_font
        self.scale = scale
        self.width = width
        self.height = height
        self.font_sizes = font_sizes or {}

        self.request_update_callback = request_update
        self.mark_dirty_callback = mark_dirty
        self.add_message = add_message
        self.refresh_ui = refresh_ui

        self.visible = False
        self.focus = "name"          # "name" 或 "desc"
        self.message = ""
        self.error = ""

        self.name_box: Optional[InputBox] = None
        self.desc_box: Optional[InputBox] = None

        # 布局矩形
        self._panel_rect: Optional[Tuple[int, int, int, int]] = None
        self._title_rect: Optional[Tuple[int, int, int, int]] = None
        self._name_label_rect: Optional[Tuple[int, int, int, int]] = None
        self._name_rect: Optional[Tuple[int, int, int, int]] = None
        self._desc_label_rect: Optional[Tuple[int, int, int, int]] = None
        self._desc_rect: Optional[Tuple[int, int, int, int]] = None
        self._save_rect: Optional[Tuple[int, int, int, int]] = None
        self._cancel_rect: Optional[Tuple[int, int, int, int]] = None
        self._buttons_y = 0

        # 输入框度量
        self._input_font_size = 20
        self._input_pad = 8
        self._ascent = 16
        self._descent = 5
        self._line_h = 24

        # 布局缓存 key
        self._layout_key: Optional[Tuple] = None

        # 背景图缓存
        self._bg_img: Optional[Image.Image] = None
        self._bg_key: Optional[Tuple] = None
        self._bg_path_cache: Optional[str] = None

    # ============================================================
    # 基础工具
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

    def _focused_box(self) -> Optional[InputBox]:
        if self.focus == "name":
            return self.name_box
        return self.desc_box

    def _reset_cursor_blink(self) -> None:
        if self.name_box is not None:
            self.name_box.reset_cursor_blink()
        if self.desc_box is not None:
            self.desc_box.reset_cursor_blink()

    @staticmethod
    def _rect_contains(rect: Optional[Tuple[int, int, int, int]], x: int, y: int) -> bool:
        if not rect:
            return False
        rx, ry, rw, rh = rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    @staticmethod
    def _rect_xyxy(rect: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x, y, w, h = rect
        return x, y, x + w, y + h

    def _text_width(self, font: Any, text: str) -> float:
        if not text:
            return 0.0
        try:
            if hasattr(font, "getlength"):
                return font.getlength(text)
            return font.getsize(text)[0]
        except Exception:
            return 0.0

    def _measure_font(self, font: Any) -> Tuple[int, int]:
        try:
            bbox = font.getbbox("中Ag")
            ascent = max(0, -bbox[1])
            descent = max(0, bbox[3] - bbox[1] - ascent)
            if ascent + descent > 0:
                return ascent, descent
        except Exception:
            pass
        return int(16 * self.scale), int(5 * self.scale)

    # ============================================================
    # 打开 / 关闭 / 保存
    # ============================================================

    def open(self) -> None:
        if self.visible:
            return

        self._bg_path_cache = None
        self._bg_img = None
        self._bg_key = None

        data = self._load_data()

        # 同步布局，创建/更新输入框
        self._sync_layout(self.width, self.height, self.scale)

        if self.name_box is not None:
            self.name_box.set_text(data.get("name", DEFAULT_NAME))
        if self.desc_box is not None:
            self.desc_box.set_text(data.get("description", DEFAULT_DESCRIPTION))

        self.focus = "name"
        self.message = ""
        self.error = ""
        self.visible = True
        self._reset_cursor_blink()
        self._request(True)

    def close(self) -> None:
        if not self.visible:
            return
        self.visible = False
        self._request(True)

    def save(self) -> None:
        if not self.visible:
            return

        try:
            name = ""
            desc = ""

            if self.name_box is not None:
                name = self.name_box.get_text().strip()
            if self.desc_box is not None:
                desc = self.desc_box.get_text().strip()

            if not name:
                name = DEFAULT_NAME
            if not desc:
                desc = DEFAULT_DESCRIPTION

            data = {
                "name": name,
                "character_description": desc,
            }

            path = _get_character_file_path()
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            # 清除 prompt 缓存
            _clear_character_cache()

            # 刷新 UI 角色名和静态缓存
            if self.refresh_ui is not None:
                try:
                    self.refresh_ui()
                except Exception as e:
                    print(f"[CharacterEditor] refresh_ui 失败: {e}")

            self.visible = False
            self.error = ""
            self.message = "人设已保存"

            if self.add_message is not None:
                try:
                    self.add_message("shizuku", "人设已保存，重新加载模型后生效。")
                except Exception:
                    pass

            self._request(True)

        except Exception as e:
            self.error = f"保存失败：{e}"
            self._request(True)

    # ============================================================
    # 数据读取
    # ============================================================

    def _load_data(self) -> Dict[str, str]:
        default = {
            "name": DEFAULT_NAME,
            "description": DEFAULT_DESCRIPTION,
        }

        path = _get_character_file_path()
        try:
            if not os.path.exists(path):
                return default

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            name = str(data.get("name", default["name"])).strip()
            desc = str(data.get("character_description", default["description"])).strip()

            if not name:
                name = default["name"]
            if not desc:
                desc = default["description"]

            return {
                "name": name,
                "description": desc,
            }

        except Exception as e:
            print(f"[CharacterEditor] 读取 character.json 失败: {e}")
            return default

    # ============================================================
    # 布局
    # ============================================================

    def _sync_layout(self, width: int, height: int, scale: float) -> None:
        self.width = width
        self.height = height
        self.scale = scale

        # 面板尺寸
        panel_w = int(min(width * 0.90, max(360, 860 * scale)))
        panel_w = max(panel_w, int(320 * scale))
        panel_w = min(panel_w, max(280, width - int(24 * scale)))

        panel_h = int(min(height * 0.90, max(320, 720 * scale)))
        panel_h = max(panel_h, int(280 * scale))
        panel_h = min(panel_h, max(240, height - int(24 * scale)))

        panel_x = (width - panel_w) // 2
        panel_y = (height - panel_h) // 2

        pad = max(8, int(18 * scale))
        title_h = max(24, int(36 * scale))
        label_h = max(16, int(24 * scale))
        name_h = max(28, int(44 * scale))
        btn_h = max(28, int(40 * scale))

        x = panel_x + pad
        w = max(50, panel_w - pad * 2)
        y = panel_y + pad

        self._panel_rect = (panel_x, panel_y, panel_w, panel_h)

        self._title_rect = (panel_x, y, panel_w, title_h)
        y += title_h + int(8 * scale)

        self._name_label_rect = (x, y, w, label_h)
        y += label_h + int(4 * scale)

        self._name_rect = (x, y, w, name_h)
        y += name_h + int(12 * scale)

        self._desc_label_rect = (x, y, w, label_h)
        y += label_h + int(4 * scale)

        buttons_y = panel_y + panel_h - pad - btn_h
        self._buttons_y = buttons_y

        desc_h = max(int(80 * scale), buttons_y - int(10 * scale) - y)
        self._desc_rect = (x, y, w, desc_h)

        cancel_w = max(60, int(88 * scale))
        save_w = max(60, int(88 * scale))
        spacing = max(6, int(12 * scale))

        cancel_x = panel_x + panel_w - pad - cancel_w
        save_x = cancel_x - spacing - save_w

        self._cancel_rect = (cancel_x, buttons_y, cancel_w, btn_h)
        self._save_rect = (save_x, buttons_y, save_w, btn_h)

        # 输入框字体和行高
        input_font_size = int(self.font_sizes.get("message", max(16, int(20 * scale))))
        input_pad = max(4, int(8 * scale))
        input_w = max(10, w - input_pad * 2)

        font = self.get_font(input_font_size)
        ascent, descent = self._measure_font(font)
        line_spacing = max(2, int(4 * scale))
        line_h = ascent + descent + line_spacing

        desc_visible = max(3, int((desc_h - input_pad * 2) // line_h))

        self._input_font_size = input_font_size
        self._input_pad = input_pad
        self._ascent = ascent
        self._descent = descent
        self._line_h = line_h

        layout_key = (
            width,
            height,
            scale,
            input_font_size,
            input_w,
            desc_visible,
        )

        if layout_key != self._layout_key:
            old_name = self.name_box.get_text() if self.name_box is not None else ""
            old_desc = self.desc_box.get_text() if self.desc_box is not None else ""
            old_focus = self.focus

            self.name_box = InputBox(
                width=input_w,
                font_getter=self.get_font,
                line_spacing=line_spacing,
                padding=input_pad,
                visible_lines=1,
                font_size=input_font_size,
            )

            self.desc_box = InputBox(
                width=input_w,
                font_getter=self.get_font,
                line_spacing=line_spacing,
                padding=input_pad,
                visible_lines=desc_visible,
                font_size=input_font_size,
            )

            self.name_box.set_text(old_name)
            self.desc_box.set_text(old_desc)
            self.focus = old_focus if old_focus in ("name", "desc") else "name"

            self._layout_key = layout_key

    # ============================================================
    # 光标闪烁
    # ============================================================

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
    
    # ============================================================
    # 渲染
    # ============================================================

    def render(self, img: Image.Image, draw: ImageDraw.ImageDraw, ctx: Any) -> Optional[Tuple[int, int, int, int]]:
        if not self.visible:
            return None

        self._sync_layout(ctx.width, ctx.height, ctx.scale)

        # 半透明遮罩
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 60))
        img.paste(overlay, (0, 0), overlay)

        px, py, pw, ph = self._panel_rect
        radius = max(6, int(12 * self.scale))

        # 背景图
        bg = self._get_background_image(pw, ph)
        if bg is not None:
            try:
                overlay = Image.new("RGBA", (pw, ph), (15, 20, 25, 60))
                
                panel_img = Image.alpha_composite(bg, overlay)
                
                mask = Image.new("L", (pw, ph), 0)
                md = ImageDraw.Draw(mask)
                md.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=radius, fill=255)
                
                img.paste(panel_img, (px, py), mask)
                
                draw.rounded_rectangle(
                    self._rect_xyxy(self._panel_rect),
                    radius=radius,
                    outline=(235, 235, 235, 130),
                    width=max(1, int(2 * self.scale)),
                )
            except Exception as e:
                print(f"[CharacterEditor] 背景图渲染失败: {e}")
                draw.rounded_rectangle(
                    self._rect_xyxy(self._panel_rect),
                    radius=radius,
                    fill=(24, 32, 38, 235),
                )
        else:
            draw.rounded_rectangle(
                self._rect_xyxy(self._panel_rect),
                radius=radius,
                fill=(24, 32, 38, 235),
                outline=(225, 225, 230, 130),
                width=max(1, int(2 * self.scale)),
            )
            
        # 标题
        title_font = self.get_font(int(self.font_sizes.get("title", max(18, int(26 * self.scale)))))
        self._draw_center_text(draw, self._title_rect, "模型人设", title_font, (250, 250, 250, 255))

        # 标签
        label_font = self.get_font(max(12, int(self._input_font_size * 0.85)))
        draw.text(
            (self._name_label_rect[0], self._name_label_rect[1]),
            "名字",
            font=label_font,
            fill=(235, 235, 235, 235),
        )
        draw.text(
            (self._desc_label_rect[0], self._desc_label_rect[1]),
            "人设",
            font=label_font,
            fill=(235, 235, 235, 235),
        )

        # 输入框
        name_cursor = self._draw_input(draw, self.name_box, self._name_rect, self.focus == "name")
        desc_cursor = self._draw_input(draw, self.desc_box, self._desc_rect, self.focus == "desc")

        # 按钮
        self._draw_button(draw, self._save_rect, "保存")
        self._draw_button(draw, self._cancel_rect, "取消")

        # 提示/错误
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

    def _draw_center_text(
        self,
        draw: ImageDraw.ImageDraw,
        rect: Tuple[int, int, int, int],
        text: str,
        font: Any,
        fill: Tuple[int, int, int, int],
    ) -> None:
        x, y, w, h = rect
        cx = x + w // 2
        cy = y + h // 2

        try:
            draw.text(
                (cx, cy),
                text,
                font=font,
                fill=fill,
                anchor="mm",
            )
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
        
    def _draw_button(
        self,
        draw: ImageDraw.ImageDraw,
        rect: Optional[Tuple[int, int, int, int]],
        text: str,
    ) -> None:
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

    def _draw_input(
        self,
        draw: ImageDraw.ImageDraw,
        box: Optional[InputBox],
        rect: Optional[Tuple[int, int, int, int]],
        active: bool,
    ) -> Optional[Tuple[int, int, int, int]]:
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
        text_y = y + pad

        start = box.scroll_offset
        end = min(len(box.lines), start + box.visible_lines)

        selection = box.get_selection()
        if selection is not None:
            sel_start, sel_end = selection
            if sel_start > sel_end:
                sel_start, sel_end = sel_end, sel_start
        else:
            sel_start = sel_end = None

        cursor_rect: Optional[Tuple[int, int, int, int]] = None

        for i in range(start, end):
            row_top = text_y + (i - start) * self._line_h
            if row_top + self._line_h > y + h - pad:
                break

            text_h = self._ascent + self._descent
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

                draw.text((x_offset, y_baseline), sub_text, font=font, fill=text_color)
                x_offset += w_text

            # 光标
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
    # 背景图
    # ============================================================

    def _find_background_path(self) -> Optional[str]:
        names = [
            "character_editor_bg.png",
            "character_editor_bg.jpg",
            "character_editor_bg.jpeg",
            "character_editor_bg.webp",
            "character_editor_bg.gif",
            "editor_bg.png",
            "editor_bg.jpg",
            "editor_bg.jpeg",
            "editor_bg.webp",
            "editor_bg.gif",
        ]

        dirs = []

        try:
            dirs.append(os.path.dirname(_get_character_file_path()))
        except Exception:
            pass

        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            dirs.append(base_dir)
            dirs.append(os.path.dirname(base_dir))
            dirs.append(os.path.dirname(os.path.dirname(base_dir)))
        except Exception:
            pass

        try:
            import sys
            if getattr(sys, "frozen", False):
                dirs.append(os.path.dirname(sys.executable))
        except Exception:
            pass

        try:
            dirs.append(os.getcwd())
        except Exception:
            pass

        seen = set()

        for d in dirs:
            if not d or d in seen:
                continue
            seen.add(d)

            for name in names:
                p = os.path.join(d, name)
                if os.path.exists(p):
                    return p

        return None

    def _get_background_image(self, panel_w: int, panel_h: int) -> Optional[Image.Image]:
        if panel_w <= 0 or panel_h <= 0:
            return None

        if self._bg_path_cache is None:
            self._bg_path_cache = self._find_background_path()
        
        path = self._bg_path_cache
        if not path:
            return None

        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0

        key = (path, panel_w, panel_h, mtime)

        if self._bg_key == key and self._bg_img is not None:
            return self._bg_img

        try:
            img = Image.open(path).convert("RGBA")
            if img.size != (panel_w, panel_h):
                try:
                    resample = Image.Resampling.LANCZOS
                except Exception:
                    resample = Image.LANCZOS
                img = img.resize((panel_w, panel_h), resample)

            self._bg_img = img
            self._bg_key = key
            return img

        except Exception as e:
            print(f"[CharacterEditor] 背景图加载失败: {e}")
            self._bg_img = None
            self._bg_key = key
            return None
        
    # ============================================================
    # 命中测试
    # ============================================================

    def hit_test(self, x: int, y: int, ctx: Any = None, is_click: bool = False) -> Dict[str, Any]:
        if not self.visible:
            return {"area": "empty"}

        if self._rect_contains(self._save_rect, x, y):
            return {"area": "character_editor_save"}

        if self._rect_contains(self._cancel_rect, x, y):
            return {"area": "character_editor_cancel"}

        if self._rect_contains(self._name_rect, x, y):
            return {"area": "character_editor_name_input"}

        if self._rect_contains(self._desc_rect, x, y):
            return {"area": "character_editor_desc_input"}

        if self._rect_contains(self._panel_rect, x, y):
            return {"area": "character_editor_panel"}

        return {"area": "character_editor_modal"}

    # ============================================================
    # 鼠标事件
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

        if self._rect_contains(self._name_rect, x, y):
            self.focus = "name"
            if self.name_box is not None:
                idx = self._get_index_by_pos(self.name_box, self._name_rect, x, y)
                self.name_box.clear_selection()
                self.name_box.set_cursor(idx)
                self.name_box.reset_cursor_blink()
            self._request(True)
            return True

        if self._rect_contains(self._desc_rect, x, y):
            self.focus = "desc"
            if self.desc_box is not None:
                idx = self._get_index_by_pos(self.desc_box, self._desc_rect, x, y)
                self.desc_box.clear_selection()
                self.desc_box.set_cursor(idx)
                self.desc_box.reset_cursor_blink()
            self._request(True)
            return True

        return True

    def handle_scroll(self, x: int, y: int, x_offset: float, y_offset: float) -> bool:
        if not self.visible:
            return False

        # 人设框内滚动
        if self._rect_contains(self._desc_rect, x, y) and self.desc_box is not None:
            delta = -int(y_offset)
            if delta == 0:
                delta = -1 if y_offset > 0 else 1
            self.desc_box.scroll(delta)
            self._request(True)
            return True

        if self._rect_contains(self._panel_rect, x, y):
            return True

        return True

    def _get_index_by_pos(
        self,
        box: InputBox,
        rect: Tuple[int, int, int, int],
        x: int,
        y: int,
    ) -> int:
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
        return max(0, min(abs_idx, len(box.text)))

    # ============================================================
    # 键盘事件
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

        # Esc：取消
        if key == glfw.KEY_ESCAPE:
            self.close()
            return True

        # Ctrl + S：保存
        if ctrl and key == glfw.KEY_S:
            self.save()
            return True

        # Tab：切换焦点
        if key == glfw.KEY_TAB:
            self.focus = "desc" if self.focus == "name" else "name"
            self._reset_cursor_blink()
            self._request(True)
            return True

        box = self._focused_box()
        if box is None:
            return True

        # Ctrl + A / C / X / V
        if ctrl:
            if key == glfw.KEY_A:
                box.select_all()
                self._request(True)
                return True

            if key == glfw.KEY_C:
                text = box.copy_selected()
                if text:
                    ClipboardService.set_text(text)
                return True

            if key == glfw.KEY_X:
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

        # Enter：名字框切换焦点；人设框换行
        if key in (glfw.KEY_ENTER, getattr(glfw, "KEY_KP_ENTER", -1)):
            if self.focus == "name":
                self.focus = "desc"
                if self.desc_box is not None:
                    self.desc_box.reset_cursor_blink()
            else:
                box.insert_text("\n")
            self._request(True)
            return True

        # 常规编辑键
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

        if key == glfw.KEY_UP:
            if self.focus == "desc":
                box.move_cursor_up()
            self._request(True)
            return True

        if key == glfw.KEY_DOWN:
            if self.focus == "desc":
                box.move_cursor_down()
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

        # 按键拦截
        return True

    def handle_char(self, text: str) -> bool:
        if not self.visible or not text:
            return False

        box = self._focused_box()
        if box is None:
            return False

        if self.focus == "name":
            text = text.replace("\n", "").replace("\r", "")

        if not text:
            return False

        box.insert_text(text)
        self._request(True)
        return True
