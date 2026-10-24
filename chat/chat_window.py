import glfw

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

        from display_config import compute_chat_metrics
        self.scale, self.width, self.height, self.font_sizes = compute_chat_metrics(display_scale)

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
        self.cursor_width = int(2 * self.scale)
        self.gl_renderer = None
        self.render_pipeline = None
        self.input_handler = None

    def _ensure_window_and_gl(self):
        if self._window_created:
            return

        # 1. 创建窗口 (内部会设置 self.win_mgr.window 并 make_context_current)
        self.win_mgr.create()

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
        )
        self.input_handler.register_callbacks()

        # 人设编辑弹窗事件路由
        self.input_handler.character_editor = getattr(self, "character_editor", None)
        
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
            self.add_message("shizuku", "干嘛~")
            self._first_show = False
            self._sync_layout()

        self._window_created = True
        glfw.make_context_current(self.main_window)

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
            selected_messages=self.selected_messages,
            recording_status_text=self.voice_ctrl.recording_status_text,
            context_menu=self.menu_manager.context_menu,
            settings_menu=self.menu_manager.settings_menu,
            hovered_menu_item=self.menu_manager.hovered_menu_item,
            settings_menu_b=self.menu_manager.settings_menu_b,
            msg_top=self.msg_top,
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
        )

    def add_message(self, sender, text):
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

        self.chat_ctrl.process_updates()
        self._sync_layout()
        if self.scroll_manager.auto_scroll:
            self.scroll_manager.scroll_to_bottom()

        self.render_pipeline.schedule()
        if self.render_pipeline.has_pending_data:
            self.mark_dirty()
        self.input_handler.update_auto_scroll()

    def draw(self):
        if not self.active or not self._window_created:
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
        self._ensure_window_and_gl()  # 按需重建 Context

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

        # 窗口显示（位置计算 + show_window + active=True）
        self.win_mgr.show()

        # 焦点 + 业务
        self._grab_focus()

        # 显示时尝试同步一次布局缓存
        self._sync_layout()
        self.scroll_manager.scroll_to_bottom()

        # 渲染
        self._request_ui_update()
        self.mark_dirty()

    def hide(self):
        # 业务清理
        self.voice_ctrl.set_active(False)
        self.voice_ctrl.stop_and_release()
        self.input_box.clear_selection()

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
        if not self._window_created:
            return
        self.win_mgr.update_position()

    def update_cursor_blink(self, current_time):
        if not self._window_created or not self.active:
            return

        editor = getattr(self, "character_editor", None)
        if editor is not None and getattr(editor, "visible", False):
            editor.update_cursor_blink(current_time)
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

    def cleanup(self):
        if self._window_created:
            self.hide()

        self.voice_ctrl.cleanup()
