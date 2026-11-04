"""
window_manager.py
窗口生命周期管理
"""

import glfw
import win32gui
import win32con

class WindowManager:

    def __init__(self, main_window_handle, width, height, scale):
        self.main_window = main_window_handle
        self.width = width
        self.height = height
        self.scale = scale

        self.window = None        # GLFW window handle
        self.x = 0
        self.y = 0
        self.active = False       # 窗口是否可见
        self.input_focus = False  # 输入框是否获得焦点

        # ---- 回调（由主窗口设置） ----
        self.on_focus_lost = None     # callable()  窗口失去焦点
        self.on_cursor_left = None    # callable()  光标离开窗口区域
        self.on_close_request = None  # callable()  窗口关闭请求

    # ==================== 创建 / 销毁 ====================

    def create(self):
        self.x, self.y = self.calc_position()

        glfw.window_hint(glfw.DECORATED, False)
        glfw.window_hint(glfw.RESIZABLE, False)
        glfw.window_hint(glfw.FLOATING, True)
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, True)

        self.window = glfw.create_window(
            self.width, self.height, "Chat", None, None
        )
        if not self.window:
            raise RuntimeError("Failed to create chat window")

        glfw.set_window_pos(self.window, self.x, self.y)
        glfw.hide_window(self.window)
        glfw.make_context_current(self.window)

        # Win32: 工具窗口样式（不在任务栏显示）
        try:
            hwnd = glfw.get_win32_window(self.window)
            if hwnd:
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                ex_style |= win32con.WS_EX_TOOLWINDOW
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
        except Exception as e:
            print(f"[Win32] 设置窗口样式失败: {e}")

        # 窗口级回调（输入级回调由主窗口自行注册）
        glfw.set_window_focus_callback(self.window, self._focus_callback)
        glfw.set_cursor_enter_callback(self.window, self._cursor_enter_callback)
        glfw.set_window_close_callback(self.window, self._close_callback)
        glfw.set_window_attrib(self.window, glfw.FLOATING, True)
        glfw.set_input_mode(self.window, glfw.CURSOR, glfw.CURSOR_NORMAL)

    def destroy(self):
        if self.window:
            try:
                glfw.hide_window(self.window)
            except Exception:
                pass
            try:
                hwnd = glfw.get_win32_window(self.window)
                if hwnd:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            
            glfw.make_context_current(self.window)
            glfw.destroy_window(self.window)
            self.window = None
        self.active = False
        self.input_focus = False

    # ==================== 位置 ====================

    def calc_position(self):
        wx, wy = glfw.get_window_pos(self.main_window)
        ww, wh = glfw.get_window_size(self.main_window)

        try:
            import win32api
            screen_w = win32api.GetSystemMetrics(0)
            screen_h = win32api.GetSystemMetrics(1)
        except Exception:
            monitor = glfw.get_primary_monitor()
            mode = glfw.get_video_mode(monitor)
            screen_w, screen_h = mode.size.width, mode.size.height

        center_x = wx + ww // 2
        if center_x < screen_w // 2:
            x = wx + ww + 5
            if x + self.width > screen_w:
                x = wx - self.width - 5
        else:
            x = wx - self.width - 5
            if x < 0:
                x = wx + ww + 5

        y = wy + wh - self.height
        y = max(0, min(y, screen_h - self.height))
        return x, y

    def update_position(self):
        if not self.active:
            return
        new_x, new_y = self.calc_position()
        if (new_x, new_y) != (self.x, self.y):
            self.x, self.y = new_x, new_y
            glfw.set_window_pos(self.window, self.x, self.y)

    def get_rect(self):
        if self.window:
            try:
                x, y = glfw.get_window_pos(self.window)
                w, h = glfw.get_window_size(self.window)
                return int(x), int(y), int(w), int(h)
            except Exception:
                pass

        return int(self.x), int(self.y), int(self.width), int(self.height)

    def set_rect(self, width, height, x=None, y=None):
        self.width = int(width)
        self.height = int(height)

        if x is not None and y is not None:
            self.x = int(x)
            self.y = int(y)

        if not self.window:
            return

        # ---------- 优先 Win32 原子更新 ----------
        try:
            hwnd = glfw.get_win32_window(self.window)
            if hwnd and x is not None and y is not None:
                flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE

                # 如果存在，则加上，减少窗口位置变化消息
                flags |= getattr(win32con, "SWP_NOOWNERZORDER", 0)
                flags |= getattr(win32con, "SWP_NOSENDCHANGING", 0)

                win32gui.SetWindowPos(
                    hwnd,
                    0,
                    self.x,
                    self.y,
                    self.width,
                    self.height,
                    flags
                )
                return
        except Exception as e:
            print(f"[WindowManager] Win32 set_rect 失败: {e}")

        # ---------- 兜底：GLFW 分步更新 ----------
        try:
            glfw.set_window_size(self.window, self.width, self.height)
        except Exception as e:
            print(f"[WindowManager] set_rect 设置尺寸失败: {e}")

        if x is not None and y is not None:
            try:
                glfw.set_window_pos(self.window, self.x, self.y)
            except Exception as e:
                print(f"[WindowManager] set_rect 设置位置失败: {e}")
            
    def resize(self, width, height, x=None, y=None):
        self.width = int(width)
        self.height = int(height)

        if self.window:
            try:
                glfw.set_window_size(self.window, self.width, self.height)
            except Exception as e:
                print(f"[WindowManager] resize 失败: {e}")

            if x is not None and y is not None:
                self.x = int(x)
                self.y = int(y)
                try:
                    glfw.set_window_pos(self.window, self.x, self.y)
                except Exception as e:
                    print(f"[WindowManager] resize 定位失败: {e}")
                
    # ==================== 显示 / 隐藏 ====================

    def show(self):
        self.x, self.y = self.calc_position()
        glfw.set_window_pos(self.window, self.x, self.y)

        # 若窗口被最小化则恢复
        try:
            hwnd = glfw.get_win32_window(self.window)
            if hwnd and win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass

        glfw.show_window(self.window)
        self.active = True

    def hide(self):
        glfw.hide_window(self.window)
        self.active = False
        self.input_focus = False

    # ==================== 焦点 ====================

    def grab_focus(self):
        self.input_focus = True
        try:
            hwnd = glfw.get_win32_window(self.window)
            if not hwnd:
                return False
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if not win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(hwnd)
            try:
                win32gui.SetFocus(hwnd)
            except Exception:
                pass
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
            )
            glfw.focus_window(self.window)
            return True
        except Exception as e:
            print(f"[焦点] 获取失败: {e}")
            return False

    def get_hwnd(self):
        if self.window:
            return glfw.get_win32_window(self.window)
        return None

    # ==================== 内部回调 ====================

    def _focus_callback(self, win, focused):
        if not focused:
            self.input_focus = False
            if self.on_focus_lost:
                self.on_focus_lost()

    def _cursor_enter_callback(self, win, entered):
        if not entered:
            if self.on_cursor_left:
                self.on_cursor_left()

    def _close_callback(self, win):
        if self.on_close_request:
            self.on_close_request()
