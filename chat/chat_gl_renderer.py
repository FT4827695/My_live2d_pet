# -*- coding: utf-8 -*-
import ctypes
import threading

import glfw
import numpy as np
import OpenGL.GL as gl


class ChatGLRenderer:

    def __init__(
        self,
        window,
        width,
        height,
        cursor_width=2,
        clear_color=(0.0, 0.0, 0.0, 0.0),
        cursor_color=(0.0, 0.0, 0.0, 1.0)
    ):
        self.window = window
        self.cursor_width = cursor_width
        self.clear_color = clear_color
        self.cursor_color = cursor_color

        # 主纹理
        self.static_texture = None
        self._texture_allocated = False
        self._texture_size = None

        # 主 shader / quad
        self.shader_prog = None
        self.vao = None
        self.vbo = None
        self.u_texture_loc = None

        # 光标 shader / quad
        self.cursor_shader_prog = None
        self.cursor_rect_vao = None
        self.cursor_rect_vbo = None
        self.cursor_color_loc = None
        self.cursor_rect_loc = None

        self._destroyed = False
        self._lock = threading.Lock()

        prev_ctx, changed = self._enter_gl()
        try:
            self._init_renderer()
            self._init_cursor_shader()
            self._init_cursor_rect()
        except Exception:
            self.cleanup()
            raise
        finally:
            self._exit_gl(prev_ctx, changed)

    # ================= GL 上下文辅助 =================

    def _enter_gl(self):
        prev_ctx = glfw.get_current_context()
        changed = prev_ctx != self.window
        if changed:
            glfw.make_context_current(self.window)
        return prev_ctx, changed

    def _exit_gl(self, prev_ctx, changed):
        if changed:
            glfw.make_context_current(prev_ctx)

    # ================= 数据准备 =================

    @staticmethod
    def _prepare_pixels(data):
        if isinstance(data, (bytes, bytearray)):
            return np.frombuffer(data, dtype=np.uint8)

        if isinstance(data, memoryview):
            if data.itemsize == 1:
                return np.frombuffer(data, dtype=np.uint8)
            data = np.asarray(data)
        else:
            data = np.asarray(data)

        if data.dtype != np.uint8:
            data = data.astype(np.uint8)

        if not data.flags["C_CONTIGUOUS"]:
            data = np.ascontiguousarray(data)

        return data

    # ================= 初始化 =================

    def _compile_shader(self, source, shader_type):
        shader = gl.glCreateShader(shader_type)
        if shader == 0:
            raise RuntimeError("Failed to create shader object")

        try:
            gl.glShaderSource(shader, source)
            gl.glCompileShader(shader)

            if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                log = gl.glGetShaderInfoLog(shader)
                raise RuntimeError(f"Shader compilation failed: {log}")

            return shader
        except Exception:
            gl.glDeleteShader(shader)
            raise

    def _init_renderer(self):
        vs_src = """#version 120
attribute vec2 a_position;
attribute vec2 a_texcoord;
varying vec2 v_texcoord;

void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texcoord = a_texcoord;
}
"""

        fs_src = """#version 120
uniform sampler2D u_texture;
varying vec2 v_texcoord;

void main() {
    gl_FragColor = texture2D(u_texture, v_texcoord);
}
"""

        vs = self._compile_shader(vs_src, gl.GL_VERTEX_SHADER)
        fs = self._compile_shader(fs_src, gl.GL_FRAGMENT_SHADER)

        prog = gl.glCreateProgram()
        if prog == 0:
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            raise RuntimeError("Failed to create program object")

        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        gl.glLinkProgram(prog)

        if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
            log = gl.glGetProgramInfoLog(prog)
            gl.glDeleteProgram(prog)
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            raise RuntimeError(f"Program linking failed: {log}")

        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)

        self.shader_prog = prog
        self.u_texture_loc = gl.glGetUniformLocation(prog, "u_texture")

        # 全屏 quad，使用 TRIANGLE_FAN（4 顶点）
        vertices = np.array(
            [
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                 1.0,  1.0, 1.0, 1.0,
                -1.0,  1.0, 0.0, 1.0,
            ],
            dtype=np.float32
        )

        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)

        if self.vao == 0 or self.vbo == 0:
            gl.glDeleteProgram(prog)
            self.shader_prog = None

            if self.vao:
                gl.glDeleteVertexArrays(1, [self.vao])
                self.vao = None

            if self.vbo:
                gl.glDeleteBuffers(1, [self.vbo])
                self.vbo = None

            raise RuntimeError("Failed to generate VAO/VBO")

        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            vertices.nbytes,
            vertices,
            gl.GL_STATIC_DRAW
        )

        pos_loc = gl.glGetAttribLocation(prog, "a_position")
        tex_loc = gl.glGetAttribLocation(prog, "a_texcoord")

        gl.glEnableVertexAttribArray(pos_loc)
        gl.glEnableVertexAttribArray(tex_loc)

        gl.glVertexAttribPointer(
            pos_loc,
            2,
            gl.GL_FLOAT,
            False,
            16,
            ctypes.c_void_p(0)
        )

        gl.glVertexAttribPointer(
            tex_loc,
            2,
            gl.GL_FLOAT,
            False,
            16,
            ctypes.c_void_p(8)
        )

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    def _init_cursor_shader(self):
        vs_src = """#version 120
attribute vec2 a_position;
uniform vec4 u_rect;

void main() {
    vec2 pos = u_rect.xy + a_position * u_rect.zw;
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

        fs_src = """#version 120
uniform vec4 u_color;

void main() {
    gl_FragColor = u_color;
}
"""

        vs = self._compile_shader(vs_src, gl.GL_VERTEX_SHADER)
        fs = self._compile_shader(fs_src, gl.GL_FRAGMENT_SHADER)

        prog = gl.glCreateProgram()
        if prog == 0:
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            raise RuntimeError("Failed to create cursor program")

        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        gl.glBindAttribLocation(prog, 0, "a_position")
        gl.glLinkProgram(prog)

        if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
            log = gl.glGetProgramInfoLog(prog)
            gl.glDeleteProgram(prog)
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            raise RuntimeError(f"Cursor program linking failed: {log}")

        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)

        self.cursor_shader_prog = prog
        self.cursor_color_loc = gl.glGetUniformLocation(prog, "u_color")
        self.cursor_rect_loc = gl.glGetUniformLocation(prog, "u_rect")

    def _init_cursor_rect(self):
        vertices = np.array(
            [
                0.0, 0.0,
                1.0, 0.0,
                1.0, 1.0,
                0.0, 1.0,
            ],
            dtype=np.float32
        )

        self.cursor_rect_vao = gl.glGenVertexArrays(1)
        self.cursor_rect_vbo = gl.glGenBuffers(1)

        if self.cursor_rect_vao == 0 or self.cursor_rect_vbo == 0:
            if self.cursor_rect_vao:
                gl.glDeleteVertexArrays(1, [self.cursor_rect_vao])
                self.cursor_rect_vao = None

            if self.cursor_rect_vbo:
                gl.glDeleteBuffers(1, [self.cursor_rect_vbo])
                self.cursor_rect_vbo = None

            raise RuntimeError("Failed to generate cursor VAO/VBO")

        gl.glBindVertexArray(self.cursor_rect_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.cursor_rect_vbo)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            vertices.nbytes,
            vertices,
            gl.GL_STATIC_DRAW
        )

        gl.glVertexAttribPointer(
            0,
            2,
            gl.GL_FLOAT,
            False,
            0,
            ctypes.c_void_p(0)
        )
        gl.glEnableVertexAttribArray(0)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    # ================= 纹理上传 =================

    def upload_texture(
        self,
        data,
        width=None,
        height=None,
        dirty_rect=None,
        data_width=None,
        data_height=None
    ):
        # 上传 / 更新聊天窗口 UI 纹理。
        if getattr(self, "_destroyed", False):
            return False

        if data is None:
            return False

        # 确定目标纹理尺寸
        if width is None or height is None:
            fb_width, fb_height = glfw.get_framebuffer_size(self.window)
            if fb_width <= 0 or fb_height <= 0:
                return False

            width = int(fb_width) if width is None else int(width)
            height = int(fb_height) if height is None else int(height)
        else:
            width = int(width)
            height = int(height)

        if width <= 0 or height <= 0:
            return False

        if (data_width is None) != (data_height is None):
            return False

        data = self._prepare_pixels(data)

        # 解析 dirty_rect
        dirty = None
        dirty_nbytes = 0

        if dirty_rect is not None:
            try:
                dx, dy, dw, dh = dirty_rect
                dx = int(dx)
                dy = int(dy)
                dw = int(dw)
                dh = int(dh)
            except Exception:
                return False

            if dw <= 0 or dh <= 0:
                return False

            if dx < 0 or dy < 0 or dx + dw > width or dy + dh > height:
                return False

            dirty = (dx, dy, dw, dh)
            dirty_nbytes = dw * dh * 4

        full_nbytes = width * height * 4

        # 推断 data_width / data_height
        if data_width is None or data_height is None:
            if dirty is None:
                if data.nbytes != full_nbytes:
                    return False
                data_width = width
                data_height = height
            else:
                dx, dy, dw, dh = dirty

                if data.nbytes == full_nbytes:
                    data_width = width
                    data_height = height
                elif data.nbytes == dirty_nbytes:
                    data_width = dw
                    data_height = dh
                else:
                    return False
        else:
            data_width = int(data_width)
            data_height = int(data_height)

            if data_width <= 0 or data_height <= 0:
                return False

            if data.nbytes != data_width * data_height * 4:
                return False

            if dirty is None:
                if data_height != height or data_width < width:
                    return False
            else:
                dx, dy, dw, dh = dirty

                full_like = (data_height == height and data_width >= width)
                cropped_like = (data_height == dh and data_width >= dw)

                if not full_like and not cropped_like:
                    return False

        with self._lock:
            if self._destroyed:
                return False

            prev_ctx, changed = self._enter_gl()

            old_alignment = gl.glGetIntegerv(gl.GL_UNPACK_ALIGNMENT)
            old_row_length = gl.glGetIntegerv(gl.GL_UNPACK_ROW_LENGTH)
            old_skip_pixels = gl.glGetIntegerv(gl.GL_UNPACK_SKIP_PIXELS)
            old_skip_rows = gl.glGetIntegerv(gl.GL_UNPACK_SKIP_ROWS)

            success = False

            try:
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
                gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
                gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)
                gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, 0)

                if self.static_texture is None:
                    self.static_texture = gl.glGenTextures(1)
                    if self.static_texture == 0:
                        self.static_texture = None
                        return False

                gl.glBindTexture(gl.GL_TEXTURE_2D, self.static_texture)

                tex_size = (width, height)
                need_alloc = (not self._texture_allocated) or (self._texture_size != tex_size)

                data_is_full = (data_height == height and data_width >= width)

                if need_alloc:
                    if data_is_full:
                        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, data_width)
                        initial_data = data
                    else:
                        initial_data = None

                    gl.glTexImage2D(
                        gl.GL_TEXTURE_2D,
                        0,
                        gl.GL_RGBA,
                        width,
                        height,
                        0,
                        gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE,
                        initial_data
                    )

                    self._texture_allocated = True
                    self._texture_size = tex_size

                    gl.glTexParameteri(
                        gl.GL_TEXTURE_2D,
                        gl.GL_TEXTURE_MIN_FILTER,
                        gl.GL_LINEAR
                    )
                    gl.glTexParameteri(
                        gl.GL_TEXTURE_2D,
                        gl.GL_TEXTURE_MAG_FILTER,
                        gl.GL_LINEAR
                    )
                    gl.glTexParameteri(
                        gl.GL_TEXTURE_2D,
                        gl.GL_TEXTURE_WRAP_S,
                        gl.GL_CLAMP_TO_EDGE
                    )
                    gl.glTexParameteri(
                        gl.GL_TEXTURE_2D,
                        gl.GL_TEXTURE_WRAP_T,
                        gl.GL_CLAMP_TO_EDGE
                    )

                    if dirty is None or data_is_full:
                        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
                        success = True
                        return success

                # 全量更新
                if dirty is None:
                    gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, data_width)
                    gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)
                    gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, 0)

                    gl.glTexSubImage2D(
                        gl.GL_TEXTURE_2D,
                        0,
                        0,
                        0,
                        width,
                        height,
                        gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE,
                        data
                    )

                # 局部更新
                else:
                    dx, dy, dw, dh = dirty

                    if data_is_full:
                        # 从完整 data 中跳过脏区域
                        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, data_width)
                        gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, dx)
                        gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, dy)
                    else:
                        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, data_width)
                        gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)
                        gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, 0)

                    gl.glTexSubImage2D(
                        gl.GL_TEXTURE_2D,
                        0,
                        dx,
                        dy,
                        dw,
                        dh,
                        gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE,
                        data
                    )

                gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
                success = True

            except Exception:
                success = False

            finally:
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, old_alignment)
                gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, old_row_length)
                gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, old_skip_pixels)
                gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, old_skip_rows)

                self._exit_gl(prev_ctx, changed)

            return success

    # ================= 绘制 =================

    def draw(self, show_cursor=False, cursor_rect=None):
        # 绘制聊天窗口。
        if getattr(self, "_destroyed", False):
            return False

        if self.static_texture is None:
            return False

        with self._lock:
            if self._destroyed:
                return False

            prev_ctx, changed = self._enter_gl()

            try:
                fb_width, fb_height = glfw.get_framebuffer_size(self.window)

                if fb_width > 0 and fb_height > 0:
                    gl.glViewport(0, 0, fb_width, fb_height)

                # 保存当前混合状态
                depth_test = gl.glIsEnabled(gl.GL_DEPTH_TEST)
                cull_face = gl.glIsEnabled(gl.GL_CULL_FACE)
                blend = gl.glIsEnabled(gl.GL_BLEND)

                blend_src_rgb = gl.glGetIntegerv(gl.GL_BLEND_SRC_RGB)
                blend_dst_rgb = gl.glGetIntegerv(gl.GL_BLEND_DST_RGB)
                blend_src_alpha = gl.glGetIntegerv(gl.GL_BLEND_SRC_ALPHA)
                blend_dst_alpha = gl.glGetIntegerv(gl.GL_BLEND_DST_ALPHA)

                # 设置绘制状态
                gl.glDisable(gl.GL_DEPTH_TEST)
                gl.glDisable(gl.GL_CULL_FACE)
                gl.glEnable(gl.GL_BLEND)

                gl.glBlendFuncSeparate(
                    gl.GL_SRC_ALPHA,
                    gl.GL_ONE_MINUS_SRC_ALPHA,
                    gl.GL_ONE,
                    gl.GL_ONE_MINUS_SRC_ALPHA
                )

                gl.glClearColor(*self.clear_color)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                # 绘制整窗纹理
                gl.glUseProgram(self.shader_prog)
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glBindTexture(gl.GL_TEXTURE_2D, self.static_texture)

                if self.u_texture_loc is not None and self.u_texture_loc >= 0:
                    gl.glUniform1i(self.u_texture_loc, 0)

                gl.glBindVertexArray(self.vao)
                gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
                gl.glBindVertexArray(0)

                gl.glUseProgram(0)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

                # 绘制输入框光标
                if show_cursor and cursor_rect is not None:
                    self._draw_cursor(cursor_rect, fb_width, fb_height)

                # 解绑纹理和程序
                gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
                gl.glUseProgram(0)

                # 恢复状态
                if depth_test:
                    gl.glEnable(gl.GL_DEPTH_TEST)
                else:
                    gl.glDisable(gl.GL_DEPTH_TEST)

                if cull_face:
                    gl.glEnable(gl.GL_CULL_FACE)
                else:
                    gl.glDisable(gl.GL_CULL_FACE)

                if blend:
                    gl.glEnable(gl.GL_BLEND)
                    gl.glBlendFuncSeparate(
                        blend_src_rgb,
                        blend_dst_rgb,
                        blend_src_alpha,
                        blend_dst_alpha
                    )
                else:
                    gl.glDisable(gl.GL_BLEND)

                success = True

            except Exception:
                success = False

            finally:
                self._exit_gl(prev_ctx, changed)

            return success

    def _draw_cursor(self, cursor_rect, fb_width, fb_height):
        if self.cursor_shader_prog is None:
            return

        if fb_width <= 0 or fb_height <= 0:
            return

        x, y, w, h = cursor_rect

        if w <= 0 or h <= 0:
            return

        x = float(x)
        y = float(y)
        w = float(w)
        h = float(h)

        left = (x / fb_width) * 2.0 - 1.0
        bottom = 1.0 - ((y + h) / fb_height) * 2.0
        w_ndc = (w / fb_width) * 2.0
        h_ndc = (h / fb_height) * 2.0

        gl.glUseProgram(self.cursor_shader_prog)

        if self.cursor_rect_loc is not None and self.cursor_rect_loc >= 0:
            gl.glUniform4f(self.cursor_rect_loc, left, bottom, w_ndc, h_ndc)

        if self.cursor_color_loc is not None and self.cursor_color_loc >= 0:
            gl.glUniform4f(self.cursor_color_loc, *self.cursor_color)

        gl.glBindVertexArray(self.cursor_rect_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glBindVertexArray(0)

    # ================= 清理 =================

    def cleanup(self):
        with self._lock:
            if self._destroyed:
                return

            prev_ctx, changed = self._enter_gl()

            try:
                for attr, delete_func in [
                    ('static_texture', lambda x: gl.glDeleteTextures([x])),
                    ('vbo', lambda x: gl.glDeleteBuffers(1, [x])),
                    ('vao', lambda x: gl.glDeleteVertexArrays(1, [x])),
                    ('shader_prog', gl.glDeleteProgram),
                    ('cursor_rect_vbo', lambda x: gl.glDeleteBuffers(1, [x])),
                    ('cursor_rect_vao', lambda x: gl.glDeleteVertexArrays(1, [x])),
                    ('cursor_shader_prog', gl.glDeleteProgram),
                ]:
                    val = getattr(self, attr, None)
                    if val is not None:
                        try:
                            delete_func(val)
                        except Exception:
                            pass
                        setattr(self, attr, None)
                    
                self._texture_allocated = False
                self._texture_size = None
            finally:
                self._destroyed = True
                self._exit_gl(prev_ctx, changed)
