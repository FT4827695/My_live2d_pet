import glfw
import time
import math
import numpy as np
import threading

from .docs_uploader import DocsUploader
from .voice_input import VoiceController
from .viewmodels import ChatViewModel
from .ui import ChatUIRenderer, ChatUI, RenderContext, MSG_AREA_Y_PAD, compute_header_rects, refresh_character_name
from .chat_gl_renderer import ChatGLRenderer
from .font_loader import get_font
from .input_box import InputBox
from .scroll_manager import ScrollManager
from .menu_manager import MenuManager
from .window_manager import WindowManager
from .render_pipeline import RenderPipeline
from .chat_controller import ChatController
from .input_handler import InputHandler
from prompt import get_character_name
from .message_layout import MessageLayoutManager
from .character_editor import CharacterEditor
from .search_api_editor import SearchApiEditor
from settings_store import get_settings
from PIL import Image, ImageDraw

class IndependentChatWindow:
    # ---------- 窗口状态代理 ----------
    @property
    def window(self):
        return self.win_mgr.window

    @property
    def active(self):
        return self.win_mgr.active

    @active.setter
    def active(self, value):
        self.win_mgr.active = value

    @property
    def input_focus(self):
        return self.win_mgr.input_focus

    @input_focus.setter
    def input_focus(self, value):
        self.win_mgr.input_focus = value

    @property
    def selecting(self):
        return self.input_handler.selecting if self.input_handler else False

    @property
    def is_resize_press_active(self):
        return self._resize_press_active

    @property
    def is_resize_dragging(self):
        return self._resize_dragging

    @selecting.setter
    def selecting(self, value):
        if self.input_handler:
            self.input_handler.selecting = value

    def mark_dirty(self):
        if self.render_pipeline:
            self.render_pipeline.mark_dirty()

    def clear_dirty(self):
        if self.render_pipeline:
            self.render_pipeline.clear_dirty()

    def is_dirty(self):
        return self.render_pipeline.is_dirty() if self.render_pipeline else False

    def __init__(self, main_window_handle, pet_instance, display_scale=1.0):
        self.main_window = main_window_handle
        self.pet = pet_instance
        self._owner_thread = threading.current_thread()

        from display_config import compute_chat_metrics

        self._display_scale = display_scale
        self.scale, self.width, self.height, self.font_sizes = compute_chat_metrics(display_scale)

        # 默认聊天窗口尺寸
        self._default_chat_size = (self.width, self.height)

        # 自定义聊天窗口尺寸，初始化时应用
        self._load_saved_chat_size()

        self.win_mgr = WindowManager(
            self.main_window, self.width, self.height, self.scale
        )
        self.win_mgr.on_focus_lost = self._on_focus_lost
        self.win_mgr.on_cursor_left = self._on_cursor_left
        self.win_mgr.on_close_request = self.hide

        # 尺寸常量
        self.input_h = int(95 * self.scale)
        self.msg_bottom_margin = int(30 * self.scale)
        self.input_y_offset = int(12 * self.scale)
        self.input_line_spacing = int(8 * self.scale)
        self.text_margin = int(48 * self.scale)
        self.input_padding = int(10 * self.scale)
        self.send_btn_width = int(65 * self.scale)
        self.doc_btn_width = int(56 * self.scale)
        self.doc_btn_height = int(38 * self.scale)
        self.clear_btn_size = int(24 * self.scale)
        self.input_visible_lines = 3

        self.font = get_font(self.font_sizes['message'])

        self.doc_uploader = DocsUploader(
            (0, 0, self.doc_btn_width, self.doc_btn_height),
            clear_btn_size=self.clear_btn_size,
            font_provider=get_font,
            scale=self.scale,
            font_sizes=self.font_sizes
        )

        # ---------- 消息布局缓存 ----------
        self.layout_manager = MessageLayoutManager(self._get_text_width)
        self.layout_manager.set_geometry(
            self.width - self.text_margin,
            (self.scale, self.font_sizes.get("message")),
        )
        # 消息选择
        self.selected_messages = set()

        # 聊天窗口大小拖曳点状态
        self._resize_grip_side = "right"

        # 聊天窗口大小拖曳状态
        self._resize_press_active = False
        self._resize_dragging = False
        self._resize_drag_side = None
        self._resize_drag_start_pos = None
        self._resize_drag_start_size = None
        self._resize_target_size = None
        self._resize_drag_threshold = 4

        # 聊天窗口拖曳预览状态
        self._resize_preview_active = False
        self._resize_backup_rect = None
        self._resize_preview_last_size = None
        self._resize_preview_last_time = 0.0
        self._resize_preview_interval = 0.05

        # 拖曳点双击恢复默认状态
        self._last_grip_click_time = 0.0
        self._last_grip_click_side = None

        # 聊天窗口大小拖曳预览节流
        self._resize_preview_interval = 0.12
        self._last_resize_preview_time = 0.0
        self._last_resize_preview_target = None

        # 创建输入框
        input_width = self.width - self.input_padding * 2 - self.send_btn_width
        self.input_box = InputBox(
            width=input_width,
            font_getter=get_font,
            font_size=self.font_sizes['message'],
            line_spacing=self.input_line_spacing,
            padding=self.input_padding,
            visible_lines=self.input_visible_lines
        )

        # 计算消息区域顶部 y 坐标
        self.character_name = get_character_name()
        title = f"{self.character_name}的小黑板"
        title_font = get_font(self.font_sizes['title'])
        bbox = title_font.getbbox(title)
        th = bbox[3] - bbox[1]
        line_y = int(18 * self.scale) + th + int(12 * self.scale)
        self.msg_top = line_y + int(12 * self.scale)

        # 动态计算消息行高
        try:
            bbox = self.font.getbbox("中Ag")
            self.fixed_ascent = max(0, -bbox[1])
            self.fixed_descent = max(0, bbox[3] - bbox[1] - self.fixed_ascent)
            self.msg_line_h = self.fixed_ascent + self.fixed_descent + int(6 * self.scale)
        except Exception:
            self.fixed_ascent = 20
            self.fixed_descent = 6
            self.msg_line_h = int(32 * self.scale)

        self.scroll_manager = ScrollManager(
            get_messages_func=lambda: self.vm.messages,
            get_text_width_func=self._get_text_width,
            width=self.width,
            height=self.height,
            msg_top=self.msg_top,
            input_h=self.input_h,
            msg_bottom_margin=self.msg_bottom_margin,
            msg_line_h=self.msg_line_h,
            text_margin=self.text_margin,
            scale=self.scale,
            msg_y_pad=int(MSG_AREA_Y_PAD * self.scale),
        )

        self.scroll_manager.set_total_lines_provider(
            lambda: self.layout_manager.snapshot.total_lines
        )
            
        # MVVM
        self.vm = ChatViewModel(self.pet, wake_up_func=glfw.post_empty_event)

        # 创建菜单管理器
        self.menu_manager = MenuManager(
            pet_instance=self.pet,
            view_model=self.vm,
            input_box=self.input_box,
            add_message_callback=self.add_message,
            get_gear_rect_callback=lambda: self._calc_gear_rect(),
            scale=self.scale,
            width=self.width,
            height=self.height
        )

        # 注册清空对话回调（重置滚动）
        self.menu_manager.set_on_clear_chat(self._on_clear_chat)

        # 模型人设编辑弹窗
        self.character_editor = CharacterEditor(
            get_font=get_font,
            scale=self.scale,
            width=self.width,
            height=self.height,
            font_sizes=self.font_sizes,
            request_update=self._request_ui_update,
            mark_dirty=self.mark_dirty,
            add_message=self.add_message,
            refresh_ui=self._refresh_character_ui,
        )

        # 设置菜单点击“模型人设”时打开编辑弹窗
        self.menu_manager.on_open_character_editor = self.character_editor.open
        
        # 搜索 API 编辑弹窗
        self.search_api_editor = SearchApiEditor(
            get_font=get_font,
            scale=self.scale,
            width=self.width,
            height=self.height,
            font_sizes=self.font_sizes,
            request_update=self._request_ui_update,
            mark_dirty=self.mark_dirty,
            add_message=self.add_message,
        )
        self.menu_manager.on_open_search_api_editor = self.search_api_editor.open
        
        # 语音交互控制器
        self.voice_ctrl = VoiceController(model_settings=self.menu_manager.model_settings)

        # 聊天业务控制器
        self.chat_ctrl = ChatController(
            vm=self.vm,
            input_box=self.input_box,
            doc_uploader=self.doc_uploader,
            scroll_manager=self.scroll_manager,
            selected_messages=self.selected_messages,
            request_update=self._request_ui_update,
            mark_dirty=self.mark_dirty,
            get_active=lambda: self.active,
            get_clean_message_lines=self._get_clean_message_lines,
        )

        self.ui_renderer = ChatUIRenderer(get_font)
        self.ui = ChatUI()

        # ---- 延迟初始化标志 ----
        self._window_created = False
        self._first_show = True
        self._minimized_close_pending = False
        self.cursor_width = int(2 * self.scale)
        self.gl_renderer = None
        self.render_pipeline = None
        self.input_handler = None

    def _ensure_window_and_gl(self):
        if self._window_created:
            return

        self._minimized_close_pending = False

        # 1. 创建窗口 (内部会设置 self.win_mgr.window 并 make_context_current)
        self.win_mgr.create()

        try:
            if self.window:
                glfw.set_window_iconify_callback(
                    self.window,
                    self._on_window_iconified
                )
        except Exception as e:
            print(f"[ChatWindow] 注册最小化回调失败: {e}")
            
        # 2. 初始化 GL 渲染器
        self.gl_renderer = ChatGLRenderer(
            self.window,
            self.width,
            self.height,
            cursor_width=self.cursor_width,
            clear_color=(0.0, 0.0, 0.0, 1.0),
            cursor_color=(0.0, 0.0, 0.0, 1.0)
        )

        # 3. 初始化输入处理器并注册回调 (依赖 self.window)
        self.input_handler = InputHandler(
            window=self.window,
            ui=self.ui,
            build_context=self._build_render_context,
            input_box=self.input_box,
            scroll_manager=self.scroll_manager,
            menu_manager=self.menu_manager,
            chat_ctrl=self.chat_ctrl,
            voice_ctrl=self.voice_ctrl,
            selected_messages=self.selected_messages,
            request_update=self._request_ui_update,
            mark_dirty=self.mark_dirty,
            get_active=lambda: self.active,
            get_input_focus=lambda: self.input_focus,
            set_input_focus=lambda v: setattr(self, 'input_focus', v),
            grab_focus=self._grab_focus,
            hide_window=self.hide,
            scale=self.scale,
            width=self.width,
            height=self.height,
            input_h=self.input_h,
            input_y_offset=self.input_y_offset,
            send_btn_width=self.send_btn_width,
            input_visible_lines=self.input_visible_lines,
            msg_top=self.msg_top,
            msg_bottom_margin=self.msg_bottom_margin,
            resize_controller=self,
        )
        self.input_handler.register_callbacks()

        # 人设编辑弹窗事件路由
        self.input_handler.character_editor = getattr(self, "character_editor", None)
        
        self.input_handler.search_api_editor = getattr(self, "search_api_editor", None)
        
        # 4. 初始化渲染管线
        self.render_pipeline = RenderPipeline(
            gl_renderer=self.gl_renderer,
            ui_renderer=self.ui_renderer,
            width=self.width,
            height=self.height,
            cursor_width=self.cursor_width,
            build_context=self._build_render_context,
            input_box=self.input_box,
            get_input_focus=lambda: self.input_focus,
            get_hwnd=self.win_mgr.get_hwnd,
        )

        # 5. 绑定语音控制器回调
        self.voice_ctrl.on_ui_update = lambda urgent=False: self._request_ui_update(urgent=urgent)
        self.voice_ctrl.on_mark_dirty = self.mark_dirty
        self.voice_ctrl.on_add_message = self.add_message
        self.voice_ctrl.on_voice_text = self._inject_voice_text

        # 6. 首次打开的初始消息
        if self._first_show:
            self.add_message(get_character_name(), "干嘛~")
            self._first_show = False
            self._sync_layout()

        self._window_created = True
        glfw.make_context_current(self.main_window)

    def _get_screen_size(self):
        try:
            import win32api
            return int(win32api.GetSystemMetrics(0)), int(win32api.GetSystemMetrics(1))
        except Exception:
            pass

        try:
            monitor = glfw.get_primary_monitor()
            mode = glfw.get_video_mode(monitor)
            if hasattr(mode, "size"):
                return int(mode.size.width), int(mode.size.height)
            return int(mode.width), int(mode.height)
        except Exception:
            return 1920, 1080

    def _get_resize_drag_screen_pos(self, x=None, y=None):
        # 优先使用 Win32 绝对坐标
        try:
            import win32gui
            sx, sy = win32gui.GetCursorPos()
            return int(sx), int(sy)
        except Exception:
            pass

        # 兜底：GLFW 相对坐标 + 当前窗口位置
        try:
            win = getattr(self.win_mgr, "window", None)
            if win is not None and x is not None and y is not None:
                wx, wy = glfw.get_window_pos(win)
                return int(wx + x), int(wy + y)
        except Exception:
            pass

        return int(x or 0), int(y or 0)

    def _enter_resize_preview(self):
        if self._resize_preview_active:
            return

        if not self._window_created:
            return

        if self._resize_backup_rect is None:
            self._resize_backup_rect = self._get_window_rect()

        try:
            if self.render_pipeline is not None and hasattr(self.render_pipeline, "begin_external_preview"):
                self.render_pipeline.begin_external_preview()
        except Exception as e:
            print(f"[ResizePreview] 进入预览失败: {e}")

        self._resize_preview_active = True
        self._resize_preview_last_size = None
        self._resize_preview_last_time = 0.0

    def _stop_resize_preview(self, restore=False, clear_backup=True):
        if getattr(self, "_resize_preview_active", False):
            try:
                if self.render_pipeline is not None and hasattr(self.render_pipeline, "end_external_preview"):
                    self.render_pipeline.end_external_preview()
            except Exception as e:
                print(f"[ResizePreview] 退出预览失败: {e}")

            self._resize_preview_active = False

            if restore:
                backup = getattr(self, "_resize_backup_rect", None)
                if backup and self._window_created:
                    old_x, old_y, old_w, old_h = backup

                    try:
                        if hasattr(self.win_mgr, "set_rect"):
                            self.win_mgr.set_rect(old_w, old_h, old_x, old_y)
                        else:
                            self.win_mgr.width = old_w
                            self.win_mgr.height = old_h
                            self.win_mgr.x = old_x
                            self.win_mgr.y = old_y
                    except Exception as e:
                        print(f"[ResizePreview] 恢复窗口失败: {e}")

                    try:
                        if self.render_pipeline is not None and hasattr(self.render_pipeline, "set_size"):
                            self.render_pipeline.set_size(self.width, self.height)
                    except Exception:
                        pass
    
                    self._request_ui_update(urgent=True)
                    self.mark_dirty()

        if clear_backup:
            self._resize_backup_rect = None

        self._resize_preview_last_size = None
        self._resize_preview_last_time = 0.0

    def _compute_resize_preview_rect(self, target_w, target_h):
        backup = getattr(self, "_resize_backup_rect", None)
        if not backup:
            return None

        old_x, old_y, old_w, old_h = backup

        side = getattr(self, "_resize_drag_side", None)
        if side not in ("left", "right"):
            side = getattr(self, "_resize_grip_side", "right")

        target_w = int(target_w)
        target_h = int(target_h)

        if side == "left":
            new_x = old_x + old_w - target_w
        else:
            new_x = old_x

        new_y = old_y + old_h - target_h

        try:
            screen_w, screen_h = self._get_screen_size()
        except Exception:
            screen_w, screen_h = 1920, 1080
    
        max_x = max(0, screen_w - target_w)
        max_y = max(0, screen_h - target_h)

        new_x = max(0, min(int(new_x), max_x))
        new_y = max(0, min(int(new_y), max_y))

        return new_x, new_y, target_w, target_h

    def _render_resize_preview_image(self, w, h):
        w = max(2, int(w))
        h = max(2, int(h))

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        border_color = (255, 220, 160, 230)
        line_width = max(1, int(2 * self.scale))
        dash_len = max(4, int(8 * self.scale))
        gap_len = max(3, int(6 * self.scale))

        x0, y0 = 1, 1
        x1, y1 = w - 2, h - 2

        def draw_dashed_line(xa, ya, xb, yb):
            dist = math.hypot(xb - xa, yb - ya)
            if dist <= 0:
                return

            dx = (xb - xa) / dist
            dy = (yb - ya) / dist

            pos = 0.0
            drawing = True

            while pos < dist:
                seg_len = dash_len if drawing else gap_len
                end = min(dist, pos + seg_len)

                if drawing:
                     draw.line(
                        [
                            (xa + dx * pos, ya + dy * pos),
                            (xa + dx * end, ya + dy * end),
                        ],
                        fill=border_color,
                        width=line_width,
                    )

                pos = end
                drawing = not drawing

        # 上边
        draw_dashed_line(x0, y0, x1, y0)
        # 右边
        draw_dashed_line(x1, y0, x1, y1)
        # 下边
        draw_dashed_line(x1, y1, x0, y1)
        # 左边
        draw_dashed_line(x0, y1, x0, y0)

        # 中央文字
        try:
            font_size = max(14, int(self.font_sizes.get("status", 20)))
            font = get_font(font_size)

            text = "调整中"
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]

            if tw < w - 8 and th < h - 8:
                draw.text(
                    (
                        (w - tw) // 2 - bbox[0],
                        (h - th) // 2 - bbox[1],
                    ),
                    text,
                    font=font,
                    fill=(255, 240, 200, 220),
                )
        except Exception:
            pass

        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        return np.array(img, dtype=np.uint8)

    def _is_shift_pressed(self):
        try:
            win = getattr(self.win_mgr, "window", None)
            if win is None:
                return False

            left_shift = glfw.get_key(win, glfw.KEY_LEFT_SHIFT)
            right_shift = glfw.get_key(win, glfw.KEY_RIGHT_SHIFT)

            return (
                left_shift == glfw.PRESS
                or right_shift == glfw.PRESS
            )
        except Exception:
            return False

    def _compute_shift_locked_target(self, start_w, start_h, dx, dy):
        side = getattr(self, "_resize_drag_side", None)
        if side not in ("left", "right"):
            side = getattr(self, "_resize_grip_side", "right")

        start_w = max(1, int(start_w))
        start_h = max(1, int(start_h))

        # ---------- 计算期望缩放系数 ----------
        if side == "right":
            # 右上角拖曳：锚点为左下角
            anchor_x, anchor_y = 0, start_h
            start_x, start_y = start_w, 0
            current_x, current_y = start_w + int(dx), int(dy)
        else:
            # 左上角拖曳：锚点为右下角
            anchor_x, anchor_y = start_w, start_h
            start_x, start_y = 0, 0
            current_x, current_y = int(dx), int(dy)

        base_dist = math.hypot(start_x - anchor_x, start_y - anchor_y)
        current_dist = math.hypot(current_x - anchor_x, current_y - anchor_y)

        if base_dist <= 0:
            factor = 1.0
        else:
            factor = current_dist / base_dist

        # ---------- 最小缩放系数：不能小于默认安全尺寸 ----------
        default_w, default_h = getattr(self, "_default_chat_size", (start_w, start_h))

        min_factor = max(
            float(default_w) / float(start_w),
            float(default_h) / float(start_h),
        )

        # ---------- 最大缩放系数：不能超过屏幕边界 ----------
        max_factor = factor

        try:
            screen_w, screen_h = self._get_screen_size()
            margin = 8

            if self._window_created and getattr(self.win_mgr, "window", None) is not None:
                backup = getattr(self, "_resize_backup_rect", None)
                if backup:
                    x, y, _, _ = backup
                else:
                    x, y = glfw.get_window_pos(self.win_mgr.window)
    
                if side == "right":
                    # 右上角拖曳：向右扩展
                    max_w = max(start_w, screen_w - x - margin)
                else:
                    # 左上角拖曳：向左扩展
                    max_w = max(start_w, x + start_w - margin)

                # 向上扩展
                max_h = max(start_h, y + start_h - margin)

                max_factor = min(
                    float(max_w) / float(start_w),
                    float(max_h) / float(start_h),
                )
            else:
                max_factor = min(
                    float(screen_w - margin * 2) / float(start_w),
                    float(screen_h - margin * 2) / float(start_h),
                )

        except Exception:
            max_factor = max(min_factor, factor)

        # ---------- 钳制 ----------
        lower = min_factor
        upper = max(min_factor, max_factor)

        factor = max(lower, min(factor, upper))

        target_w = int(round(start_w * factor))
        target_h = int(round(start_h * factor))

        return target_w, target_h

    def _get_window_rect(self):
        try:
            if hasattr(self.win_mgr, "get_rect"):
                return self.win_mgr.get_rect()
        except Exception:
            pass

        return (
            getattr(self.win_mgr, "x", 0),
            getattr(self.win_mgr, "y", 0),
            self.width,
            self.height,
        )

    def _load_saved_chat_size(self):
        try:
            settings = get_settings()
            cw = int(settings.get("chat.custom_width", 0) or 0)
            ch = int(settings.get("chat.custom_height", 0) or 0)
            cs = float(settings.get("chat.custom_display_scale", 0.0) or 0.0)
        except Exception:
            return

        if cw <= 0 or ch <= 0:
            return

        if abs(cs - getattr(self, "_display_scale", 0.0)) > 1e-6:
            return

        default_w, default_h = getattr(self, "_default_chat_size", (self.width, self.height))

        try:
            screen_w, screen_h = self._get_screen_size()
        except Exception:
            screen_w, screen_h = 1920, 1080

        margin = 16

        max_w = max(self.width, screen_w - margin)
        max_h = max(self.height, screen_h - margin)

        self.width = max(self.width, min(cw, max_w))
        self.height = max(self.height, min(ch, max_h))        

    # ---------- 辅助方法 ----------
    def _calc_gear_rect(self):
        return compute_header_rects(self.width, self.scale)["gear"]
    
    def _get_text_width(self, text):
        return self.font.getlength(text) if hasattr(self.font, 'getlength') else self.font.getsize(text)[0]

    def _get_clean_message_lines(self, sender, text):

        from services import TextUtils

        lines = TextUtils.get_message_lines(
            sender,
            text,
            self.width - self.text_margin,
            self._get_text_width
        )

        clean_lines = []
        for line in lines:
            clean = (
                line.replace('\n', '')
                .replace('\r', '')
                .replace('\x0b', '')
                .replace('\x0c', '')
            )
            for ch in '\u2028\u2029\u0085':
                clean = clean.replace(ch, '')
            clean_lines.append(clean)

        return clean_lines

    def _is_resize_unlocked(self):
        try:
            return get_settings().get_bool("chat.resize_unlocked", False)
        except Exception:
            return False

    def _update_resize_grip_side(self):
        side = "right"

        try:
            chat_win = getattr(self.win_mgr, "window", None)
            pet_win = getattr(self, "main_window", None)
    
            if pet_win is None:
                pet_win = getattr(self.win_mgr, "main_window", None)

            if chat_win is None or pet_win is None:
                self._resize_grip_side = side
                return

            chat_x, chat_y = glfw.get_window_pos(chat_win)
            chat_w, chat_h = glfw.get_window_size(chat_win)

            pet_x, pet_y = glfw.get_window_pos(pet_win)
            pet_w, pet_h = glfw.get_window_size(pet_win)

            chat_center_x = chat_x + chat_w * 0.5
            pet_center_x = pet_x + pet_w * 0.5

            if chat_center_x >= pet_center_x:
                side = "right"
            else:
                side = "left"

        except Exception:
            side = "right"

        self._resize_grip_side = side


    def begin_resize_drag(self, side, x, y):
        if not self._is_resize_unlocked():
            return False

        if side not in ("left", "right"):
            return False

        if self._resize_press_active:
            return False

        try:
            if self.menu_manager.context_menu.get("visible"):
                return False
            if self.menu_manager.settings_menu.get("visible"):
                return False
            settings_menu_b = getattr(self.menu_manager, "settings_menu_b", None)
            if settings_menu_b is not None and settings_menu_b.get("visible"):
                return False
        except Exception:
            pass

        try:
            if getattr(self.character_editor, "visible", False):
                return False
            if getattr(self.search_api_editor, "visible", False):
                return False
        except Exception:
            pass

        self._resize_press_active = True
        self._resize_dragging = False
        self._resize_drag_side = side
        start_screen_x, start_screen_y = self._get_resize_drag_screen_pos(x, y)
        self._resize_drag_start_pos = (start_screen_x, start_screen_y)
        self._resize_drag_start_size = (self.width, self.height)
        self._resize_target_size = (self.width, self.height)

        # 拖曳预览备份
        self._resize_backup_rect = self._get_window_rect()
        self._resize_preview_active = False
        self._resize_preview_last_size = None
        self._resize_preview_last_time = 0.0

        return True

    def _tick_resize_preview(self):
        if not getattr(self, "_resize_dragging", False):
            return

        target = getattr(self, "_resize_target_size", None)
        if not target:
            return

        start = getattr(self, "_resize_drag_start_size", None)

        target_w = int(target[0])
        target_h = int(target[1])

        # 如果目标尺寸和拖曳起始尺寸一致，不进入预览
        if start is not None:
            start_w = int(start[0])
            start_h = int(start[1])

            if target_w == start_w and target_h == start_h:
                if self._resize_preview_active:
                    self._stop_resize_preview(restore=True, clear_backup=False)
                return

        now = time.time()

        if (target_w, target_h) == self._resize_preview_last_size:
            return
    
        if now - self._resize_preview_last_time < self._resize_preview_interval:
            return

        if not self._resize_preview_active:
            self._enter_resize_preview()

        rect = self._compute_resize_preview_rect(target_w, target_h)
        if rect is None:
            return

        new_x, new_y, new_w, new_h = rect

        try:
            if hasattr(self.win_mgr, "set_rect"):
                self.win_mgr.set_rect(new_w, new_h, new_x, new_y)
            else:
                self.win_mgr.width = new_w
                self.win_mgr.height = new_h
                self.win_mgr.x = new_x
                self.win_mgr.y = new_y
        except Exception as e:
            print(f"[ResizePreview] 设置预览窗口失败: {e}")
            return

        try:
            preview_image = self._render_resize_preview_image(new_w, new_h)

            if self.render_pipeline is not None and hasattr(self.render_pipeline, "submit_external_frame"):
                self.render_pipeline.submit_external_frame(preview_image, new_w, new_h)
        except Exception as e:
            print(f"[ResizePreview] 提交预览图失败: {e}")

        self._resize_preview_last_size = (new_w, new_h)
        self._resize_preview_last_time = now
    
    def move_resize_drag(self, x, y):
        if not self._resize_press_active:
            return False

        if self._resize_drag_start_pos is None:
            return False

        start_screen_x, start_screen_y = self._resize_drag_start_pos
        current_screen_x, current_screen_y = self._get_resize_drag_screen_pos(x, y)

        dx = current_screen_x - start_screen_x
        dy = current_screen_y - start_screen_y

        if not self._resize_dragging:
            if abs(dx) <= self._resize_drag_threshold and abs(dy) <= self._resize_drag_threshold:
                return False

            self._resize_dragging = True

            self._last_resize_preview_time = 0.0
            self._last_resize_preview_target = None
        
        start_w, start_h = self._resize_drag_start_size

        # ---------- 按住 Shift：锁定当前宽高比 ----------
        if self._is_shift_pressed():
            target_w, target_h = self._compute_shift_locked_target(
                start_w,
                start_h,
                dx,
                dy
            )

            self._resize_target_size = (int(target_w), int(target_h))
            return True

        # ---------- 默认：自由宽高 ----------
        if self._resize_drag_side == "right":
            target_w = start_w + dx
        else:
            target_w = start_w - dx

        target_h = start_h - dy

        self._resize_target_size = self._clamp_resize_target(target_w, target_h)
        return True

    def end_resize_drag(self, x=None, y=None):
        if not getattr(self, "_resize_press_active", False):
            return False

        was_dragging = getattr(self, "_resize_dragging", False)
        target = getattr(self, "_resize_target_size", None)
        start = getattr(self, "_resize_drag_start_size", None)
        side = getattr(self, "_resize_drag_side", None)

        if side not in ("left", "right"):
            side = getattr(self, "_resize_grip_side", "right")

        backup = getattr(self, "_resize_backup_rect", None)

       # 重置拖曳状态
        self._resize_press_active = False
        self._resize_dragging = False
        self._resize_drag_side = None
        self._resize_drag_start_pos = None
        self._resize_drag_start_size = None
        self._resize_target_size = None

        # 重置旧的预览节流状态
        try:
            self._last_resize_preview_time = 0.0
            self._last_resize_preview_target = None
        except Exception:
            pass

        now = time.time()

        # ---------- 没有真正拖曳：处理双击 ----------
        if not was_dragging:
            self._stop_resize_preview(restore=True, clear_backup=True)

            if (
                side
                and getattr(self, "_last_grip_click_side", None) == side
                and now - getattr(self, "_last_grip_click_time", 0.0) < 0.45
            ):
                self._last_grip_click_time = 0.0
                self._last_grip_click_side = None
                self._reset_resize_to_default(side)
                return True

            self._last_grip_click_time = now
            self._last_grip_click_side = side

            self._request_ui_update(urgent=True)
            self.mark_dirty()

            if self.render_pipeline:
                self.render_pipeline.schedule()

            return True

        # ---------- 发生过真正拖曳 ----------
        self._last_grip_click_time = 0.0
        self._last_grip_click_side = None

        # 停止预览，但保持当前预览窗口位置，不清空 backup
        self._stop_resize_preview(restore=False, clear_backup=False)

        applied = False

        if target and start:
            target_w = int(target[0])
            target_h = int(target[1])
            start_w = int(start[0])
            start_h = int(start[1])

            if (target_w, target_h) != (start_w, start_h):
                applied = self._apply_resize_target(
                    target_w,
                    target_h,
                    side,
                    save=True,
                    base_rect=backup,
                )

        if not applied:
            self._request_ui_update(urgent=True)
            self.mark_dirty()

            if self.render_pipeline:
                self.render_pipeline.schedule()

        self._resize_backup_rect = None

        return True

    def _reset_resize_to_default(self, side=None):
        default_w, default_h = getattr(self, "_default_chat_size", (self.width, self.height))

        if side not in ("left", "right"):
            side = getattr(self, "_resize_grip_side", "right")

        applied = self._apply_resize_target(int(default_w), int(default_h), side, save=True)

        if not applied:
            self._request_ui_update(urgent=True)
            self.mark_dirty()

            if self.render_pipeline:
                self.render_pipeline.schedule()
            
    def cancel_resize_drag(self):
        if not getattr(self, "_resize_press_active", False):
            return False

        self._resize_press_active = False
        self._resize_dragging = False
        self._resize_drag_side = None
        self._resize_drag_start_pos = None
        self._resize_drag_start_size = None
        self._resize_target_size = None

        try:
            self._last_resize_preview_time = 0.0
            self._last_resize_preview_target = None
        except Exception:
            pass

        self._stop_resize_preview(restore=True, clear_backup=True)

        self._request_ui_update(urgent=True)
        self.mark_dirty()

        if self.render_pipeline:
            self.render_pipeline.schedule()

        return True

    def _clamp_resize_target(self, w, h):
        start_w, start_h = getattr(self, "_resize_drag_start_size", None) or (self.width, self.height)
        min_w, min_h = getattr(self, "_default_chat_size", (self.width, self.height))

        w = max(min_w, int(w))
        h = max(min_h, int(h))

        backup = getattr(self, "_resize_backup_rect", None)

        try:
            screen_w, screen_h = self._get_screen_size()
            margin = 8

            if backup:
                old_x, old_y, old_w, old_h = backup

                side = getattr(self, "_resize_drag_side", None)
                if side not in ("left", "right"):
                    side = getattr(self, "_resize_grip_side", "right")

                if side == "left":
                    # 左上角：右边缘固定，向左扩展
                    max_w = old_x + old_w - margin
                else:
                    # 右上角：左边缘固定，向右扩展
                    max_w = screen_w - old_x - margin

                # 底边固定，向上扩展
                max_h = old_y + old_h - margin

                w = min(w, max(min_w, int(max_w)))
                h = min(h, max(min_h, int(max_h)))
            else:
                w = min(w, max(min_w, screen_w - margin * 2))
                h = min(h, max(min_h, screen_h - margin * 2))

        except Exception:
            pass

        return int(w), int(h)

    def _apply_resize_target(self, new_w, new_h, side, save=True, base_rect=None):
        new_w = int(new_w)
        new_h = int(new_h)

        if new_w <= 0 or new_h <= 0:
            return False

        old_w, old_h = self.width, self.height

        if new_w == old_w and new_h == old_h:
            return False

        if side not in ("left", "right"):
            side = getattr(self, "_resize_grip_side", "right")

        # ---------- 计算新的窗口位置 ----------
        new_x = None
        new_y = None

        if base_rect is not None:
            base_x, base_y, base_w, base_h = base_rect

            if side == "left":
                new_x = base_x + base_w - new_w
            else:
                new_x = base_x

            new_y = base_y + base_h - new_h

        elif self._window_created and getattr(self.win_mgr, "window", None) is not None:
            try:
                cur_x, cur_y = glfw.get_window_pos(self.win_mgr.window)
            except Exception:
                cur_x = getattr(self.win_mgr, "x", 0)
                cur_y = getattr(self.win_mgr, "y", 0)

            if side == "left":
                new_x = cur_x + old_w - new_w
            else:
                new_x = cur_x

            new_y = cur_y + old_h - new_h

        if new_x is not None and new_y is not None:
            try:
                screen_w, screen_h = self._get_screen_size()

                max_x = max(0, screen_w - new_w)
                max_y = max(0, screen_h - new_h)

                new_x = max(0, min(int(new_x), max_x))
                new_y = max(0, min(int(new_y), max_y))
            except Exception:
                pass

        # ---------- 更新正式尺寸 ----------
        self.width = new_w
        self.height = new_h

        # ---------- 更新 GLFW 窗口 ----------
        try:
            if hasattr(self.win_mgr, "set_rect"):
                self.win_mgr.set_rect(new_w, new_h, new_x, new_y)
            else:
                self.win_mgr.width = new_w
                self.win_mgr.height = new_h

                if new_x is not None and new_y is not None:
                    self.win_mgr.x = new_x
                    self.win_mgr.y = new_y

                if getattr(self.win_mgr, "window", None) is not None:
                    glfw.set_window_size(self.win_mgr.window, new_w, new_h)
                    glfw.set_window_pos(self.win_mgr.window, new_x, new_y)
        except Exception as e:
            print(f"[Resize] 窗口尺寸应用失败: {e}")

        # ---------- 更新渲染管线 ----------
        try:
            if self.render_pipeline is not None and hasattr(self.render_pipeline, "set_size"):
                self.render_pipeline.set_size(new_w, new_h)
        except Exception as e:
            print(f"[Resize] RenderPipeline 尺寸更新失败: {e}")

        # ---------- 更新输入框宽度 ----------
        try:
            new_input_width = new_w - self.input_padding * 2 - self.send_btn_width
            self.input_box.set_width(new_input_width)
        except Exception as e:
            print(f"[Resize] 输入框宽度更新失败: {e}")

        # ---------- 更新滚动区域 ----------
        try:
            self.scroll_manager.width = new_w
            self.scroll_manager.height = new_h
            self.scroll_manager.invalidate_cache()
            self.scroll_manager.scrollbar_rect = None
            self.scroll_manager.thumb_rect = None
        except Exception:
            pass

        # ---------- 重新折行消息 ----------
        try:
            self._sync_layout()
        except Exception:
            pass

        # ---------- 滚动位置钳制 ----------
        try:
            if self.scroll_manager.auto_scroll:
                self.scroll_manager.scroll_to_bottom()
            else:
                self.scroll_manager.scroll(0)
        except Exception:
            pass

        # ---------- 更新菜单管理器 ----------
        try:
            self.menu_manager.width = new_w
            self.menu_manager.height = new_h
        except Exception:
            pass

        # ---------- 更新编辑弹窗 ----------
        for editor_attr in ("character_editor", "search_api_editor"):
            editor = getattr(self, editor_attr, None)
            if editor is None:
                continue

            try:
                editor.width = new_w
                editor.height = new_h
            except Exception:
                pass

            try:
                if hasattr(editor, "_layout_key"):
                    editor._layout_key = None
            except Exception:
                pass

        # ---------- 更新输入处理器命中区域 ----------
        try:
            if self.input_handler is not None and hasattr(self.input_handler, "update_layout_metrics"):
                self.input_handler.update_layout_metrics(new_w, new_h)
        except Exception:
            pass

        # ---------- 强制静态背景重画 ----------
        try:
            self.ui_renderer._static_key = None
        except Exception:
            pass

        # ---------- 更新拖曳点方向 ----------
        try:
            self._update_resize_grip_side()
        except Exception:
            pass

        # ---------- 请求渲染 ----------
        self._request_ui_update(urgent=True)
        self.mark_dirty()

        if self.render_pipeline:
            self.render_pipeline.schedule()

       # ---------- 保存自定义尺寸 ----------
        if save:
            try:
                settings = get_settings()
                settings.set("chat.custom_width", new_w, save=False)
                settings.set("chat.custom_height", new_h, save=False)
                settings.set(
                    "chat.custom_display_scale",
                    float(getattr(self, "_display_scale", 0.0)),
                    save=False
                )
                settings.save()
            except Exception as e:
                print(f"[Resize] 保存自定义尺寸失败: {e}")

        return True

    def _sync_layout(self):
        if getattr(self, "layout_manager", None) is None:
            return None

        if getattr(self, "vm", None) is None:
            return None

        try:
            self.layout_manager.set_geometry(
                self.width - self.text_margin,
                (self.scale, self.font_sizes.get("message")),
            )
            return self.layout_manager.sync(self.vm.messages)
        except Exception as e:
            print(f"[布局缓存] 同步失败: {e}")
            return None
    
    def _build_render_context(self):

        layout_snapshot = None
        if getattr(self, "layout_manager", None) is not None:
            layout_snapshot = self.layout_manager.snapshot

        return RenderContext(
            width=self.width,
            height=self.height,
            scale=self.scale,
            font=self.font,
            font_sizes=self.font_sizes,
            input_box=self.input_box,
            scroll_manager=self.scroll_manager,
            vm=self.vm,
            doc_uploader=self.doc_uploader,
            selected_messages=set(self.selected_messages),
            recording_status_text=self.voice_ctrl.recording_status_text,
            context_menu=self.menu_manager.context_menu,
            settings_menu=self.menu_manager.settings_menu,
            hovered_menu_item=self.menu_manager.hovered_menu_item,
            settings_menu_b=self.menu_manager.settings_menu_b,
            # 聊天窗口大小拖曳点
            resize_unlocked=self._is_resize_unlocked(),
            resize_grip_side=self._resize_grip_side,
            # 聊天窗口大小拖曳预览
            resize_dragging=self._resize_dragging,
            resize_target_size=self._resize_target_size if self._resize_dragging else None,
            resize_start_size=self._resize_drag_start_size if self._resize_dragging else None,            msg_top=self.msg_top,
            input_h=self.input_h,
            msg_bottom_margin=self.msg_bottom_margin,
            input_y_offset=self.input_y_offset,
            input_line_spacing=self.input_line_spacing,
            text_margin=self.text_margin,
            send_btn_width=self.send_btn_width,
            msg_line_h=self.msg_line_h,
            input_visible_lines=self.input_visible_lines,
            fixed_ascent=self.fixed_ascent,
            fixed_descent=self.fixed_descent,
            selecting=self.selecting,
            msg_text_selection=self.input_handler.msg_text_selection if self.input_handler else None,
            layout_snapshot=self.layout_manager.snapshot,
            character_editor=getattr(self, "character_editor", None),
            search_api_editor=getattr(self, "search_api_editor", None),
        )

    def add_message(self, sender, text):
        if threading.current_thread() is not self._owner_thread:
            try:
                self.vm.update_queue.put(('add_message', sender, text))
                glfw.post_empty_event()
            except Exception:
                pass
            return
        self.chat_ctrl.add_message(sender, text)
    
    def _refresh_character_ui(self):
        try:
            refresh_character_name()
        except Exception as e:
            print(f"[ChatWindow] 刷新角色名失败: {e}")

        try:
            self.character_name = get_character_name()
        except Exception:
            self.character_name = "雫"

        try:
            self.ui_renderer._static_key = None
        except Exception:
            pass

        try:
            title = f"{self.character_name}的小黑板"
            title_font = get_font(self.font_sizes['title'])
            bbox = title_font.getbbox(title)
            th = bbox[3] - bbox[1]
            line_y = int(18 * self.scale) + th + int(12 * self.scale)
            self.msg_top = line_y + int(12 * self.scale)

            if hasattr(self, "scroll_manager"):
                self.scroll_manager.msg_top = self.msg_top
        except Exception:
            pass

        try:
            self._sync_layout()
        except Exception:
            pass

        self._request_ui_update()
        self.mark_dirty()
        
    # ---------- 事件回调 ----------
    def _request_ui_update(self, urgent=False):
        if self.render_pipeline:
            self.render_pipeline.request_update(urgent=urgent)

    def _inject_voice_text(self, text):
        self.vm.update_queue.put(('voice_text', text))
        glfw.post_empty_event()

    # ---------- 生命周期 ----------
    def tick(self):
        if not self.active or not self._window_created:
            return

        if self._minimized_close_pending:
            self.mark_dirty()
            return
    
        # ---------- 聊天窗口大小拖曳冻结 ----------
        if getattr(self, "_resize_press_active", False):
            if self.input_handler:
                self.input_handler.update_auto_scroll()

            if getattr(self, "_resize_press_active", False):
                if hasattr(self, "_tick_resize_preview"):
                    self._tick_resize_preview()

            return

        # ---------- 正常业务刷新 ----------
        self.chat_ctrl.process_updates()
        self._sync_layout()

        if self.scroll_manager.auto_scroll:
            self.scroll_manager.scroll_to_bottom()

        if self.render_pipeline:
            self.render_pipeline.schedule()

            if self.render_pipeline.has_pending_data:
                self.mark_dirty()

        if self.input_handler:
            self.input_handler.update_auto_scroll()
        
    def draw(self):
        if not self.active or not self._window_created:
            return False

        if self._minimized_close_pending:
            self._minimized_close_pending = False
            self.hide()
            return False

        rendered = self.render_pipeline.draw()
        if rendered:
            self.render_pipeline.clear_dirty()
        return rendered
    
    # ---------- 窗口管理 ----------
    def _grab_focus(self):
        self.input_box.clear_selection()
        self.selecting = False
        self.win_mgr.grab_focus()
        self._request_ui_update()
        self.mark_dirty()
        self.input_box.reset_cursor_blink()

    def show(self):
        self._minimized_close_pending = False
        self._ensure_window_and_gl()  # 按需重建 Context
        self._refresh_character_ui()

        # 业务重置
        self.voice_ctrl.set_active(True)
        self.input_box.clear_selection()

        if self.input_handler:
            self.input_handler.reset_state()

        self.selected_messages.clear()

        if self.input_handler:
            self.input_handler.clear_msg_selection()

        self.menu_manager.hide_context_menu()
        self.menu_manager.hide_settings_menu()

        if getattr(self, "character_editor", None) is not None:
            self.character_editor.close()
        if getattr(self, "search_api_editor", None) is not None:
            self.search_api_editor.close()
            
        # 窗口显示（位置计算 + show_window + active=True）
        self.win_mgr.show()
        self._update_resize_grip_side()

        # 焦点 + 业务
        self._grab_focus()

        # 显示时尝试同步一次布局缓存
        self._sync_layout()
        self.scroll_manager.scroll_to_bottom()

        # 渲染
        self._request_ui_update()
        self.mark_dirty()

    def hide(self):
        self._minimized_close_pending = False

        # 隐藏窗口时直接清理拖曳预览状态，不做恢复
        self._resize_press_active = False
        self._resize_dragging = False
        self._resize_preview_active = False
        self._resize_backup_rect = None
        self._resize_preview_last_size = None
        self._resize_preview_last_time = 0.0
    
        # 业务清理
        self.voice_ctrl.set_active(False)
        self.voice_ctrl.stop_and_release()
        self.input_box.clear_selection()

        if getattr(self, "character_editor", None) is not None:
            self.character_editor.close()
        if getattr(self, "search_api_editor", None) is not None:
            self.search_api_editor.close()
            
        if self.input_handler:
            self.input_handler.reset_state()

        if self._window_created:
            if self.render_pipeline:
                self.render_pipeline.cleanup()

            if self.window:
                glfw.make_context_current(self.window)

            if self.render_pipeline:
                self.render_pipeline.cleanup_gl()

            self.win_mgr.destroy()
            self._window_created = False

            glfw.make_context_current(self.main_window)

    def update_position(self):
        if getattr(self, "_resize_press_active", False):
            return

        if getattr(self, "_resize_preview_active", False):
            return
    
        if not self._window_created:
            return
        if self._minimized_close_pending:
            return
        self.win_mgr.update_position()
        self._update_resize_grip_side()
        
    def update_cursor_blink(self, current_time):
        if not self._window_created or not self.active:
            return
        if self._minimized_close_pending:
            return
        
        editor = getattr(self, "character_editor", None)
        if editor is not None and getattr(editor, "visible", False):
            editor.update_cursor_blink(current_time)
            return

        api_editor = getattr(self, "search_api_editor", None)
        if api_editor is not None and getattr(api_editor, "visible", False):
            api_editor.update_cursor_blink(current_time)
            return
        
        if not self.input_focus:
            return

        visible_before = self.input_box.get_cursor_visible()
        self.input_box.update_cursor_blink(current_time)

        if self.input_box.get_cursor_visible() != visible_before:
            self.mark_dirty()
            glfw.post_empty_event()
            
    # ---------- 清空对话回调（重置滚动） ----------
    def _on_clear_chat(self):
        self.chat_ctrl.clear_chat()

    # ---------- 窗口状态回调（由 WindowManager 触发） ----------
    def _on_focus_lost(self):
        if self.input_handler:
            self.input_handler.reset_state()

        self._request_ui_update()
        self.mark_dirty()

    def _on_cursor_left(self):
        if self.doc_uploader.hide_hover():
            self._request_ui_update()
            self.mark_dirty()

        if self.scroll_manager.scrollbar_visible:
            self.scroll_manager.scrollbar_visible = False
            self.scroll_manager.cursor_in_msg_area = False
            self._request_ui_update()
            self.mark_dirty()

    def _on_window_iconified(self, window, iconified):
        if not getattr(self, "_window_created", False):
            return

        if iconified:
            self._minimized_close_pending = True
            try:
                glfw.post_empty_event()
            except Exception:
                pass
            
    def cleanup(self):
        if self._window_created:
            self.hide()

        self.voice_ctrl.cleanup()
