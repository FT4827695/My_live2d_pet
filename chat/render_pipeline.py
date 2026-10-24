"""
render_pipeline.py
渲染管线：后台渲染调度、纹理上传、OpenGL 绘制、脏标记管理。
"""
import time
import numpy as np
import glfw
import concurrent.futures

from .ime_helper import set_ime_position


class RenderPipeline:

    def __init__(self, gl_renderer, ui_renderer, width, height, cursor_width,
                 build_context, input_box, get_input_focus, get_hwnd):
        """
        参数:
            gl_renderer:      ChatGLRenderer 实例
            ui_renderer:      ChatUIRenderer 实例
            width, height:    窗口尺寸（像素）
            cursor_width:     光标宽度（像素）
            build_context:    callable() -> RenderContext
            input_box:        InputBox 实例（用于光标可见性判断）
            get_input_focus:  callable() -> bool（输入框是否有焦点）
            get_hwnd:         callable() -> hwnd（用于 IME 位置）
        """
        self._gl_renderer = gl_renderer
        self._ui_renderer = ui_renderer
        self._width = width
        self._height = height
        self._cursor_width = cursor_width
        self._build_context = build_context
        self._input_box = input_box
        self._get_input_focus = get_input_focus
        self._get_hwnd = get_hwnd

        # 脏标记
        self._dirty = True

        # 渲染调度状态
        self._urgent_update = False
        self._last_tex_update_time = 0.0
        self._update_interval = 0.10
        self._pending_tex_update = False
        self._pending_image_data = None
        self._current_cursor_rect = None

        # 后台渲染线程池（单线程，保证渲染顺序）
        self._render_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._render_future = None

    # ==================== 脏标记 ====================

    def mark_dirty(self):
        self._dirty = True

    def clear_dirty(self):
        self._dirty = False

    def is_dirty(self):
        return self._dirty

    # ==================== 请求更新 ====================

    def request_update(self, urgent=False):
        if urgent:
            self._urgent_update = True
        self._pending_tex_update = True
        glfw.post_empty_event()

    # ==================== 调度 ====================

    def schedule(self):
        if not self._pending_tex_update:
            return

        now = time.time()

        if self._urgent_update:
            self._urgent_update = False
            if self._render_future is not None and not self._render_future.done():
                self._render_future.cancel()
            self._last_tex_update_time = now
            self._pending_tex_update = False
            self._render_future = self._render_executor.submit(self._render_background)
            return

        if now - self._last_tex_update_time < self._update_interval:
            return

        if self._render_future is not None and not self._render_future.done():
            return

        self._last_tex_update_time = now
        self._pending_tex_update = False
        self._render_future = self._render_executor.submit(self._render_background)

    # ==================== 后台渲染 ====================

    def _render_background(self):
        try:
            ctx = self._build_context()
            img, result = self._ui_renderer.render(ctx)
            self._current_cursor_rect = result.cursor_rect
            data = np.array(img, dtype=np.uint8)
            self._pending_image_data = data
            self.mark_dirty()
            glfw.post_empty_event()
        except Exception as e:
            print(f"[渲染后台] 失败: {e}")

    # ==================== 纹理上传 ====================

    def _upload_pending_texture(self):
        if self._pending_image_data is None:
            return

        self._gl_renderer.upload_texture(
            self._pending_image_data,
            self._width,
            self._height
        )
        self._pending_image_data = None
        self.mark_dirty()

        # IME 候选框位置跟随光标
        if self._current_cursor_rect:
            x, y, w, h = self._current_cursor_rect
            hwnd = self._get_hwnd()
            if hwnd:
                set_ime_position(hwnd, int(x), int(y))

    # ==================== 绘制 ====================

    def draw(self):
        self._upload_pending_texture()

        if self._gl_renderer.static_texture is None:
            self.clear_dirty()
            return False

        if not self._dirty:
            return False

        # 计算光标矩形
        cursor_rect = None
        if (self._get_input_focus()
                and self._input_box.get_cursor_visible()
                and self._current_cursor_rect is not None):
            x, y, _, h = self._current_cursor_rect
            cursor_rect = (x, y, self._cursor_width, h)

        rendered = self._gl_renderer.draw(
            show_cursor=cursor_rect is not None,
            cursor_rect=cursor_rect
        )
        self.clear_dirty()
        return rendered

    # ==================== 只读属性 ====================

    @property
    def current_cursor_rect(self):
        return self._current_cursor_rect

    @property
    def has_pending_data(self):
        return self._pending_image_data is not None

    @property
    def gl_renderer(self):
        return self._gl_renderer

    # ==================== 清理 ====================

    def cleanup(self):
        if self._render_executor:
            self._render_executor.shutdown(wait=False)
            self._render_executor = None

    def cleanup_gl(self):
        if self._gl_renderer is not None:
            self._gl_renderer.cleanup()
            self._gl_renderer = None
