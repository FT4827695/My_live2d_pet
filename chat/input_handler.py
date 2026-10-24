"""
input_handler.py
输入事件处理：鼠标、键盘、滚轮回调，文本选择拖拽，自动滚动。
"""
import time
import glfw
from services import ClipboardService


class InputHandler:
    DOUBLE_CLICK_INTERVAL = 0.45
    DOUBLE_CLICK_RADIUS = 5

    def __init__(self, window, ui, build_context,
                 input_box, scroll_manager, menu_manager,
                 chat_ctrl, voice_ctrl,
                 selected_messages,
                 request_update, mark_dirty,
                 get_active, get_input_focus, set_input_focus,
                 grab_focus, hide_window,
                 scale, width, height,
                 input_h, input_y_offset, send_btn_width,
                 input_visible_lines, msg_top, msg_bottom_margin):
        # 核心组件
        self._window = window
        self._ui = ui
        self._build_context = build_context
        self._input_box = input_box
        self._scroll_manager = scroll_manager
        self._menu_manager = menu_manager
        self._chat_ctrl = chat_ctrl
        self._voice_ctrl = voice_ctrl
        self._selected_messages = selected_messages
        # 回调
        self._request_update = request_update
        self._mark_dirty = mark_dirty
        self._get_active = get_active
        self._get_input_focus = get_input_focus
        self._set_input_focus = set_input_focus
        self._grab_focus = grab_focus
        self._hide_window = hide_window
        # 尺寸常量
        self._scale = scale
        self._width = width
        self._height = height
        self._input_h = input_h
        self._input_y_offset = input_y_offset
        self._send_btn_width = send_btn_width
        self._input_visible_lines = input_visible_lines
        self._msg_top = msg_top
        self._msg_bottom_margin = msg_bottom_margin
        # 预计算输入框区域
        self._input_top = self._height - self._input_h - self._input_y_offset
        self._input_bottom = self._input_top + self._input_h
        self._input_x = int(12 * self._scale)
        self._input_w = (self._width - int(12 * self._scale) * 2
                         - self._send_btn_width - int(16 * self._scale))
        self._drag_margin = int(5 * self._scale)
        self._msg_bottom = self._height - self._input_h - self._msg_bottom_margin
        # 交互状态
        self._pressed_button = None
        self._selecting = False
        self._auto_scroll_dir = 0
        self._last_auto_scroll_time = 0.0
        self._last_click_time = 0.0
        self._last_click_pos = (0, 0)
        # 消息文本拖选状态
        self._msg_selecting = False
        self._msg_sel_anchor = None       # (msg_idx, line_in_msg, char_in_line)
        self._msg_sel_focus = None
        self._msg_drag_moved = False
        self._msg_press_pos = (0, 0)
        self._msg_auto_scroll_dir = 0
        self._last_msg_auto_scroll_time = 0.0
        self._msg_text_selection = None   # 规范化后的选区
        # 中键长按录音状态
        self._middle_press_active = False

        # 人设编辑弹窗（由 chat_window 注入）
        self.character_editor = None
        
    # ==================== 注册 ====================
    def register_callbacks(self):
        """一次性注册所有 GLFW 输入回调"""
        glfw.set_mouse_button_callback(self._window, self._mouse_callback)
        glfw.set_cursor_pos_callback(self._window, self._cursor_pos_callback)
        glfw.set_key_callback(self._window, self._key_callback)
        glfw.set_char_callback(self._window, self._char_callback)
        glfw.set_scroll_callback(self._window, self._scroll_callback)

    # ==================== 状态 ====================
    @property
    def selecting(self):
        return self._selecting

    @selecting.setter
    def selecting(self, value):
        self._selecting = value

    @property
    def msg_text_selection(self):
        return self._msg_text_selection

    @msg_text_selection.setter
    def msg_text_selection(self, value):
        self._msg_text_selection = value

    def clear_msg_selection(self):
        self._msg_text_selection = None
        self._msg_sel_anchor = None
        self._msg_sel_focus = None
        self._msg_selecting = False
        self._msg_drag_moved = False
        self._msg_auto_scroll_dir = 0

    @staticmethod
    def _normalize_selection(anchor, focus):
        if anchor <= focus:
            return (anchor, focus)
        return (focus, anchor)

    def reset_state(self):
        self._pressed_button = None
        self._selecting = False
        self._auto_scroll_dir = 0
        self._scroll_manager.dragging_scroll = False
        self._scroll_manager.can_start_drag_on_move = False
        # 消息文本拖选重置
        self._msg_selecting = False
        self._msg_sel_anchor = None
        self._msg_sel_focus = None
        self._msg_drag_moved = False
        self._msg_auto_scroll_dir = 0
        # 中键状态重置
        self._middle_press_active = False
        self._voice_ctrl.cancel_long_press()
        try:
            ctx = self._build_context()
            uploader = getattr(ctx, "doc_uploader", None)
            if uploader is not None:
                if uploader.hide_hover():
                    self._request_update()
                    self._mark_dirty()
        except Exception:
            pass

    # ==================== 鼠标 ====================
    def _mouse_callback(self, win, button, action, mods):
        if not self._get_active():
            return

        x, y = glfw.get_cursor_pos(win)
        rx, ry = int(x), int(y)

        editor = getattr(self, "character_editor", None)
        if editor is not None and editor.visible:
            editor.handle_mouse_button(button, action, mods, rx, ry)
            self._request_update()
            self._mark_dirty()
            return

        # 左键释放：通用清理
        if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.RELEASE:
            self._on_left_release_cleanup()
        # 中键处理
        if button == glfw.MOUSE_BUTTON_MIDDLE:
            if action == glfw.PRESS:
                self._handle_middle_press()
            elif action == glfw.RELEASE:
                self._handle_middle_release()
            return
        # 命中测试
        ctx = self._build_context()
        hit = self._ui.hit_test(rx, ry, ctx, is_click=True)
        area = hit.get('area')
        if button == glfw.MOUSE_BUTTON_LEFT:
            if action == glfw.PRESS:
                self._handle_left_press(area, hit, rx, ry)
            elif action == glfw.RELEASE:
                self._handle_left_release()
        elif button == glfw.MOUSE_BUTTON_RIGHT and action == glfw.PRESS:
            self._handle_right_press(area, rx, ry)

    def _on_left_release_cleanup(self):
        was_dragging = self._scroll_manager.dragging_scroll
        # ---- 消息文本拖选释放 ----
        if self._msg_selecting:
            self._msg_selecting = False
            self._msg_auto_scroll_dir = 0
            if not self._msg_drag_moved:
                # 没有移动 → 点击 → 切换整条选中
                msg_idx = self._msg_sel_anchor[0] if self._msg_sel_anchor else None
                if msg_idx is not None:
                    if msg_idx in self._selected_messages:
                        self._selected_messages.remove(msg_idx)
                    else:
                        self._selected_messages.add(msg_idx)
                self._msg_text_selection = None
            # 有移动 → 拖选 → 选区已在 drag 中确定，保留
            self._msg_sel_anchor = None
            self._msg_sel_focus = None
            self._request_update()
            self._mark_dirty()
        # ---- 输入框选择清理 ----
        if (self._input_box.is_selection_active()
                and self._input_box.selection_start == self._input_box.selection_end):
            self._input_box.clear_selection()
        self._selecting = False
        self._scroll_manager.dragging_scroll = False
        self._scroll_manager.can_start_drag_on_move = False
        self._auto_scroll_dir = 0
        if was_dragging:
            self._request_update()
            self._mark_dirty()

    def _handle_left_press(self, area, hit, rx, ry):
        sm = self._scroll_manager
        mm = self._menu_manager
        # ---- 设置按钮（左键 → 菜单A） ----
        if area == 'settings_btn':
            # 如果菜单B已打开，只关闭菜单B，不打开菜单A
            if mm.settings_menu_b.get('visible'):
                mm.hide_settings_menu_b()
                self._request_update()
                self._mark_dirty()
                mm.clear_hover()
                return
            # 否则切换菜单A
            if mm.settings_menu.get('visible'):
                mm.hide_settings_menu()
            else:
                mm.show_settings_menu(rx, ry)
            self._request_update()
            self._mark_dirty()
            mm.clear_hover()
            return
        # ---- 菜单B（已打开时，点击空白关闭） ----
        if mm.settings_menu_b.get('visible'):
            if area == 'settings_menu_b' and hit.get('item_id') is not None:
                mm.handle_settings_menu_b_command(hit['item_id'])
                mm.clear_hover()
                self._request_update()
                self._mark_dirty()
            else:
                mm.hide_settings_menu_b()
                mm.clear_hover()
                self._request_update()
                self._mark_dirty()
            return
        # ---- 设置菜单A（已打开时） ----
        if area == 'settings_menu' and hit.get('item_id') is not None:
            mm.handle_settings_command(hit['item_id'])
            mm.hide_settings_menu()
            mm.clear_hover()
            self._request_update()
            self._mark_dirty()
            return
        # ---- 右键菜单（已打开时） ----
        if mm.context_menu.get('visible'):
            if area == 'context_menu' and hit.get('item_id') is not None:
                mm.handle_context_menu_command(hit['item_id'])
                mm.hide_context_menu()
                mm.clear_hover()
                self._request_update()
                self._mark_dirty()
            else:
                mm.hide_context_menu()
                mm.clear_hover()
                self._request_update()
                self._mark_dirty()
            return
        # ---- 设置菜单A（已打开时，点击其他区域关闭） ----
        if mm.settings_menu.get('visible'):
            mm.hide_settings_menu()
            mm.clear_hover()
            self._request_update()
            self._mark_dirty()
            return
        # ---- 关闭按钮 ----
        if area == 'close_btn':
            self._hide_window()
        # ---- 文档清除 ----
        elif area == 'doc_clear':
            self._pressed_button = 'clear'
            self._chat_ctrl.clear_file()
            self._request_update()
            self._mark_dirty()
        # ---- 文档上传（长按录音 / 短按选文件） ----
        elif area == 'doc_upload':
            self._pressed_button = 'upload'
            self._voice_ctrl.start_long_press(delay=1.0)  # 改为1秒
        # ---- 滚动条 ----
        elif area == 'scrollbar_thumb':
            sm.start_drag(ry)
        elif area == 'scrollbar_track':
            if sm.click_track(ry):
                self._request_update()
                self._mark_dirty()
        # ---- 输入框 ----
        elif area == 'input_box':
            self._selected_messages.clear()
            self.clear_msg_selection()
            self._grab_focus()
            abs_idx = hit['char_index']
            # 双击检测：两次按下时间接近且位置接近
            now = time.time()
            is_double = (
                now - self._last_click_time < self.DOUBLE_CLICK_INTERVAL
                and abs(rx - self._last_click_pos[0]) <= self.DOUBLE_CLICK_RADIUS
                and abs(ry - self._last_click_pos[1]) <= self.DOUBLE_CLICK_RADIUS
            )
            self._last_click_time = now
            self._last_click_pos = (rx, ry)
            if is_double:
                self._input_box.select_all()
                self._selecting = False
            else:
                self._selecting = True
                self._input_box.set_cursor(abs_idx)
                self._input_box.selection_start = abs_idx
                self._input_box.selection_end = abs_idx
            self._request_update()
            self._mark_dirty()
        # ---- 发送按钮 ----
        elif area == 'send_btn':
            self._chat_ctrl.send_message()
        # ---- 消息列表（准备拖选 / 点击选中） ----
        elif area == 'msg_list':
            msg_idx = hit.get('msg_index')
            if msg_idx is not None:
                self._input_box.clear_selection()
                self._msg_sel_anchor = (msg_idx, hit.get('line_in_msg', 0), hit.get('char_in_line', 0))
                self._msg_sel_focus = self._msg_sel_anchor
                self._msg_selecting = True
                self._msg_drag_moved = False
                self._msg_press_pos = (rx, ry)
                self._msg_text_selection = None
        # ---- 空白区域 ----
        elif area == 'empty':
            if self._get_input_focus():
                self._set_input_focus(False)
            self._input_box.clear_selection()
            self._selecting = False
            self._selected_messages.clear()
            self.clear_msg_selection()
            self._request_update()
            self._mark_dirty()

    def _handle_msg_selection_drag(self, area, hit, rx, ry):
        # 判定是否真正移动（区分点击和拖选）
        if not self._msg_drag_moved:
            dx = abs(rx - self._msg_press_pos[0])
            dy = abs(ry - self._msg_press_pos[1])
            if dx <= 4 and dy <= 4:
                return
            self._msg_drag_moved = True
            self._selected_messages.clear()
        # 更新焦点
        if area == 'msg_list' and hit.get('msg_index') is not None:
            self._msg_sel_focus = (
                hit['msg_index'],
                hit.get('line_in_msg', 0),
                hit.get('char_in_line', 0)
            )
        # 计算规范化选区
        if self._msg_sel_anchor and self._msg_sel_focus:
            self._msg_text_selection = self._normalize_selection(
                self._msg_sel_anchor, self._msg_sel_focus
            )
        # 边缘自动滚动方向
        margin = self._drag_margin
        if ry < self._msg_top + margin:
            self._msg_auto_scroll_dir = -1
        elif ry > self._msg_bottom - margin:
            self._msg_auto_scroll_dir = 1
        else:
            self._msg_auto_scroll_dir = 0
        self._request_update()
        self._mark_dirty()

    def _handle_left_release(self):
        if self._pressed_button == 'clear':
            self._pressed_button = None
        elif self._pressed_button == 'upload':
            self._pressed_button = None
            if not self._voice_ctrl.handle_release():
                self._chat_ctrl.start_file_selection()

    def _handle_right_press(self, area, rx, ry):
        mm = self._menu_manager
        # ---- 检查是否在齿轮按钮上（绕过 hit_test 的菜单优先级） ----
        margin = int(12 * self._scale)
        close_size = int(30 * self._scale)
        close_x = self._width - margin - close_size - int(5 * self._scale)
        close_y = margin + int(5 * self._scale)
        gear_size = int(30 * self._scale)
        gear_x = close_x - gear_size - int(5 * self._scale)
        gear_y = close_y
        if gear_x <= rx <= gear_x + gear_size and gear_y <= ry <= gear_y + gear_size:
            area = 'settings_btn'
        # ---- 设置按钮（右键 → 菜单B） ----
        if area == 'settings_btn':
            # 菜单A已打开，只关闭菜单A，不打开菜单B
            if mm.settings_menu.get('visible'):
                mm.hide_settings_menu()
                self._request_update()
                self._mark_dirty()
                mm.clear_hover()
                return
            # 否则切换菜单B
            if mm.settings_menu_b.get('visible'):
                mm.hide_settings_menu_b()
            else:
                mm.show_settings_menu_b(rx, ry)
            self._request_update()
            self._mark_dirty()
            mm.clear_hover()
            return
        # ---- 输入框右键菜单 ----
        if area == 'input_box':
            mm.show_context_menu(rx, ry)
            self._request_update()
            self._mark_dirty()

    def _handle_middle_press(self):
        # 检查快捷键是否启用
        if self._voice_ctrl.voice_shortcut != 'wheel':
            return
        self._middle_press_active = True
        self._voice_ctrl.start_long_press(delay=0.5)

    def _handle_middle_release(self):
        if self._middle_press_active:
            self._middle_press_active = False
            self._voice_ctrl.handle_release()

    # ==================== 光标移动 ====================
    def _cursor_pos_callback(self, win, x, y):
        rx, ry = int(x), int(y)

        editor = getattr(self, "character_editor", None)
        if editor is not None and editor.visible:
            return
        
        ctx = self._build_context()
        uploader = getattr(ctx, "doc_uploader", None)
        if uploader is not None:
            if uploader.update_hover(rx, ry):
                self._request_update()
                self._mark_dirty()
        hit = self._ui.hit_test(rx, ry, ctx, is_click=False,
                                need_msg_char=self._msg_selecting)
        area = hit.get('area')
        # 滚动条悬停
        self._scroll_manager.is_hovering_thumb = (area == 'scrollbar_thumb')
        # 菜单悬停
        self._update_menu_hover(area, hit)
        # 滚动条拖拽
        if self._scroll_manager.dragging_scroll:
            if self._scroll_manager.update_drag(ry):
                self._request_update()
                self._mark_dirty()
            return
        # ---- 消息文本拖选 ----
        if self._msg_selecting:
            self._handle_msg_selection_drag(area, hit, rx, ry)
            return
        # 输入框选择拖拽
        if self._selecting:
            self._handle_selection_drag(area, hit, rx, ry)
            return
        # 消息区域悬停 → 显示/隐藏滚动条
        in_area = self._msg_top <= ry <= self._msg_bottom
        if in_area != self._scroll_manager.cursor_in_msg_area:
            self._scroll_manager.update_scrollbar_visibility(cursor_in_area=in_area)
            self._request_update()
            self._mark_dirty()

    def _update_menu_hover(self, area, hit):
        mm = self._menu_manager
        new_hover = None
        if area == 'context_menu' and hit.get('item_id') is not None:
            new_hover = ('context', hit['item_id'])
        elif area == 'settings_menu' and hit.get('item_id') is not None:
            new_hover = ('settings', hit['item_id'])
        elif area == 'settings_menu_b' and hit.get('item_id') is not None:
            new_hover = ('settings_b', hit['item_id'])
        if new_hover != mm.hovered_menu_item:
            if new_hover:
                mm.set_hovered_item(new_hover[0], new_hover[1])
            else:
                mm.clear_hover()
            self._request_update()
            self._mark_dirty()

    def _handle_selection_drag(self, area, hit, rx, ry):
        m = self._drag_margin
        new_dir = 0
        if self._input_x - m <= rx <= self._input_x + self._input_w + m:
            if ry < self._input_top + m and self._input_box.scroll_offset > 0:
                new_dir = -1
            elif (ry > self._input_bottom - m
                  and self._input_box.scroll_offset < len(self._input_box.lines) - self._input_visible_lines):
                new_dir = 1
        self._auto_scroll_dir = new_dir
        if area == 'input_box':
            self._input_box.selection_end = hit['char_index']
            self._input_box.set_cursor(self._input_box.selection_end)
            self._request_update()
            self._mark_dirty()
        if new_dir != 0:
            self._request_update()
            self._mark_dirty()

    # ==================== 自动滚动（tick 中调用） ====================
    def update_auto_scroll(self):
        now = time.time()
        # ---- 输入框自动滚动 ----
        if self._selecting and self._auto_scroll_dir != 0:
            if now - self._last_auto_scroll_time >= 0.05:
                self._last_auto_scroll_time = now
                ib = self._input_box
                max_off = max(0, len(ib.lines) - self._input_visible_lines)
                new_offset = ib.scroll_offset + self._auto_scroll_dir
                if not (0 <= new_offset <= max_off):
                    self._auto_scroll_dir = 0
                else:
                    ib.scroll_offset = new_offset
                    try:
                        mx, my = glfw.get_cursor_pos(self._window)
                        rx, ry = int(mx), int(my)
                        ctx = self._build_context()
                        hit = self._ui.hit_test(rx, ry, ctx)
                        if hit.get('area') == 'input_box':
                            ib.selection_end = hit['char_index']
                            ib.set_cursor(ib.selection_end)
                        else:
                            if self._auto_scroll_dir == -1:
                                first_line = ib.scroll_offset
                                abs_idx = sum(len(l) for l in ib.lines[:first_line])
                            else:
                                last_line = min(
                                    ib.scroll_offset + self._input_visible_lines - 1,
                                    len(ib.lines) - 1
                                )
                                abs_idx = sum(len(l) for l in ib.lines[:last_line + 1])
                            ib.selection_end = abs_idx
                            ib.set_cursor(abs_idx)
                    except Exception:
                        pass
                    self._request_update()
                    self._mark_dirty()
        # ---- 消息区域自动滚动 ----
        if self._msg_selecting and self._msg_auto_scroll_dir != 0:
            if now - self._last_msg_auto_scroll_time >= 0.05:
                self._last_msg_auto_scroll_time = now
                self._scroll_manager.scroll(self._msg_auto_scroll_dir)
                try:
                    mx, my = glfw.get_cursor_pos(self._window)
                    rx, ry = int(mx), int(my)
                    ctx = self._build_context()
                    hit = self._ui.hit_test(rx, ry, ctx, need_msg_char=True) 
                    if hit.get('area') == 'msg_list' and hit.get('msg_index') is not None:
                        self._msg_sel_focus = (
                            hit['msg_index'],
                            hit.get('line_in_msg', 0),
                            hit.get('char_in_line', 0)
                        )
                        if self._msg_sel_anchor and self._msg_sel_focus:
                            self._msg_text_selection = self._normalize_selection(
                                self._msg_sel_anchor, self._msg_sel_focus
                            )
                except Exception:
                    pass
                self._request_update()
                self._mark_dirty()

    # ==================== 键盘 ====================
    def _key_callback(self, win, key, scancode, action, mods):

        if not self._get_active() or action not in (glfw.PRESS, glfw.REPEAT):
            return

        # ==================== 人设编辑弹窗拦截 ====================
        editor = getattr(self, "character_editor", None)
        if editor is not None and editor.visible:
            editor.handle_key(key, scancode, action, mods)
            self._request_update()
            self._mark_dirty()
            return
        
        # ESC 关闭菜单或窗口
        if key == glfw.KEY_ESCAPE:
            mm = self._menu_manager
            # 优先关闭菜单B
            if mm.settings_menu_b.get('visible'):
                mm.hide_settings_menu_b()
                self._request_update()
                self._mark_dirty()
                return
            # 关闭菜单A
            if mm.settings_menu.get('visible'):
                mm.hide_settings_menu()
                self._request_update()
                self._mark_dirty()
                return
            # 关闭右键菜单
            if mm.context_menu.get('visible'):
                mm.hide_context_menu()
                self._request_update()
                self._mark_dirty()
                return
            self._hide_window()
            return
        # Ctrl+V 粘贴
        if key == glfw.KEY_V and (mods & glfw.MOD_CONTROL):
            if not self._get_input_focus():
                return
            text = ClipboardService.get_text()
            if text:
                self._input_box.paste(text)
                self._request_update()
                self._mark_dirty()
            return
        # Ctrl+C 复制
        if key == glfw.KEY_C and (mods & glfw.MOD_CONTROL):
            # 1. 输入框选区
            if self._get_input_focus() and self._input_box.is_selection_active():
                selected = self._input_box.copy_selected()
                if selected:
                    ClipboardService.set_text(selected)
                return
            # 2. 消息文本字符选区
            if self._msg_text_selection:
                text = self._chat_ctrl.copy_msg_text_range(self._msg_text_selection)
                if text:
                    ClipboardService.set_text(text)
                return
            # 3. 整条消息选中
            if self._chat_ctrl.copyselected_messages():
                return
            return
        # 回车发送
        if key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            if self._get_input_focus():
                self._chat_ctrl.send_message()
            return
        if not self._get_input_focus():
            return
        ib = self._input_box
        # 退格
        if key == glfw.KEY_BACKSPACE:
            if mods & glfw.MOD_CONTROL:
                ib.delete_word_backward()
            else:
                ib.delete_backward()
            self._request_update()
            self._mark_dirty()
            return
        # Delete
        if key == glfw.KEY_DELETE:
            ib.delete_forward()
            self._request_update()
            self._mark_dirty()
            return
        # 方向键
        if key == glfw.KEY_LEFT:
            ib.move_cursor_left()
            self._request_update()
            self._mark_dirty()
            return
        if key == glfw.KEY_RIGHT:
            ib.move_cursor_right()
            self._request_update()
            self._mark_dirty()
            return
        if key == glfw.KEY_UP:
            ib.move_cursor_up()
            self._request_update()
            self._mark_dirty()
            return
        if key == glfw.KEY_DOWN:
            ib.move_cursor_down()
            self._request_update()
            self._mark_dirty()
            return

    # ==================== 字符输入 ====================
    def _char_callback(self, win, codepoint):
        if not self._get_active():
            return

        # ==================== 人设编辑弹窗拦截 ====================
        editor = getattr(self, "character_editor", None)
        if editor is not None and editor.visible:
            if codepoint >= 32:
                editor.handle_char(chr(codepoint))
                self._request_update()
                self._mark_dirty()
            return

        if not self._get_input_focus():
            return
        
        if codepoint < 32:
            return
        self._input_box.insert_text(chr(codepoint))
        self._request_update()
        self._mark_dirty()

    # ==================== 滚轮 ====================
    def _scroll_callback(self, win, xoff, yoff):
        if not self._get_active():
            return

        # ==================== 人设编辑弹窗拦截 ====================
        editor = getattr(self, "character_editor", None)
        if editor is not None and editor.visible:
            x, y = glfw.get_cursor_pos(win)
            rx, ry = int(x), int(y)
            editor.handle_scroll(rx, ry, xoff, yoff)
            self._request_update()
            self._mark_dirty()
            return

        if self._middle_press_active:
            self._middle_press_active = False
            self._voice_ctrl.cancel_long_press()
        x, y = glfw.get_cursor_pos(win)
        rx, ry = int(x), int(y)
        input_y = self._height - self._input_h - self._input_y_offset
        margin = int(10 * self._scale)
        if margin <= rx <= self._width - margin and input_y <= ry <= input_y + self._input_h:
            self._input_box.scroll(-int(round(yoff)))
        else:
            self._scroll_manager.scroll(-int(round(yoff)))
        self._request_update()
        self._mark_dirty()
