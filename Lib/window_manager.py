# -*- coding: utf-8 -*-

import os
import sys
import time
import ctypes


def _ensure_plugin_path():
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    plugin_dir = os.path.join(base_dir, "data")

    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)


_ensure_plugin_path()

import glfw
import OpenGL.GL as gl
import win32gui
import win32con
import gl_direct
import opt_rotation
import live2d.v2 as live2d


class WindowManager:
    """
    只负责 GLFW / OpenGL / Win32 窗口初始化。
    """

    def __init__(self):
        self.window = None
        self._title = "Live2D Pet"
        self._hwnd = None
        self._old_wnd_proc = None
        self._wnd_proc_cb = None
        self._callbacks = {}
        self._glfw_initialized = False

    def ensure_glfw_initialized(self):
        if self._glfw_initialized:
            return True
        try:
            if not glfw.init():
                print("[错误] GLFW 初始化失败")
                return False
        except Exception as e:
            print(f"[错误] GLFW 初始化异常: {e}")
            return False

        self._glfw_initialized = True
        return True
    
    def init_window(
        self,
        width,
        height,
        title,
        on_mouse_move=None,
        on_mouse_button=None,
        on_key=None,
        on_scroll=None,
        on_char=None,
        on_window_resize=None,
    ):
        self._title = title

        if not self.ensure_glfw_initialized():
            return False
        
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, True)
        glfw.window_hint(glfw.FLOATING, True)
        glfw.window_hint(glfw.DECORATED, False)
        glfw.window_hint(glfw.RESIZABLE, False)
        glfw.window_hint(glfw.SAMPLES, 4)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 6)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_COMPAT_PROFILE)

        self.window = glfw.create_window(
            width,
            height,
            title,
            None,
            None
        )

        if not self.window:
            glfw.terminate()
            self._glfw_initialized = False
            return False

        time.sleep(0.1)

        self._setup_win32_window()

        glfw.make_context_current(self.window)
        glfw.swap_interval(1)

        self._init_gl_backend()
        self._init_live2d()

        self._register_callbacks(
            on_mouse_move=on_mouse_move,
            on_mouse_button=on_mouse_button,
            on_key=on_key,
            on_scroll=on_scroll,
            on_char=on_char,
            on_window_resize=on_window_resize,
        )

        return True

    def get_window_size(self):
        if not self.window:
            return 0, 0

        try:
            return glfw.get_window_size(self.window)
        except Exception:
            return 0, 0

    def destroy(self):
        try:
            if self.window:
                glfw.destroy_window(self.window)
        except Exception:
            pass

        self.window = None
        self._hwnd = None
        self._callbacks.clear()
        self._old_wnd_proc = None
        self._wnd_proc_cb = None

        if self._glfw_initialized:
            try:
                glfw.terminate()
            except Exception:
                pass
            self._glfw_initialized = False

    # ==================== Win32 ====================

    def _setup_win32_window(self):
        hwnd = win32gui.FindWindow(None, self._title)
        if not hwnd:
            return

        self._hwnd = hwnd

        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex_style |= win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(
            win32con.WS_MINIMIZEBOX |
            win32con.WS_MAXIMIZEBOX |
            win32con.WS_CAPTION
        )
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE |
            win32con.SWP_NOSIZE |
            win32con.SWP_FRAMECHANGED
        )

        self._wnd_proc_cb = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p
        )(self._wnd_proc_hook)

        self._old_wnd_proc = win32gui.SetWindowLong(
            hwnd,
            win32con.GWL_WNDPROC,
            self._wnd_proc_cb
        )

    def _wnd_proc_hook(self, hwnd, msg, wparam, lparam):
        if msg == 0x0046 and lparam:  # WM_WINDOWPOSCHANGING
            class WINDOWPOS(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("hwndInsertAfter", ctypes.c_void_p),
                    ("x", ctypes.c_int),
                    ("y", ctypes.c_int),
                    ("cx", ctypes.c_int),
                    ("cy", ctypes.c_int),
                    ("flags", ctypes.c_uint)
                ]

            pos = WINDOWPOS.from_address(lparam)

            if pos.flags & (0x20000 | 0x0080):
                pos.flags &= ~(0x20000 | 0x0080)
                return 0

        if self._old_wnd_proc:
            return win32gui.CallWindowProc(
                self._old_wnd_proc,
                hwnd,
                msg,
                wparam,
                lparam
            )

        return 0

    # ==================== GL / Live2D ====================

    def _init_gl_backend(self):
        if gl_direct.init():
            gl_direct.patch_wrapper()
            gl_direct.patch_modules()
        else:
            print("[GL] ctypes 直调不可用，使用 PyOpenGL 回退")

        opt_rotation.apply()

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glUseProgram(0)
        gl.glEnable(gl.GL_MULTISAMPLE)

    def _init_live2d(self):
        live2d.init()
        live2d.glInit()

        if hasattr(live2d, "setDebugMode"):
            live2d.setDebugMode(False)

    # ==================== callbacks ====================

    def _register_callbacks(
        self,
        on_mouse_move,
        on_mouse_button,
        on_key,
        on_scroll,
        on_char,
        on_window_resize,
    ):
        self._callbacks = {
            "on_mouse_move": on_mouse_move,
            "on_mouse_button": on_mouse_button,
            "on_key": on_key,
            "on_scroll": on_scroll,
            "on_char": on_char,
            "on_window_resize": on_window_resize,
        }

        if on_mouse_move is not None:
            glfw.set_cursor_pos_callback(self.window, on_mouse_move)

        if on_mouse_button is not None:
            glfw.set_mouse_button_callback(self.window, on_mouse_button)

        if on_key is not None:
            glfw.set_key_callback(self.window, on_key)

        if on_scroll is not None:
            glfw.set_scroll_callback(self.window, on_scroll)

        if on_char is not None:
            glfw.set_char_callback(self.window, on_char)

        if on_window_resize is not None:
            glfw.set_window_size_callback(self.window, on_window_resize)
