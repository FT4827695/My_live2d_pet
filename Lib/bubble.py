# -*- coding: utf-8 -*-

import os
import time
import ctypes

import numpy as np
import glfw
import OpenGL.GL as gl

from PIL import Image, ImageDraw, ImageFont


class BubbleRenderer:
    # ===== 4K 基准参数 =====
    BASE_WINDOW_WIDTH = 440.0

    BASE_MAX_WIDTH = 500.0
    BASE_PADDING = 15.0
    BASE_FONT_SIZE = 30.0

    BASE_X_OFFSET = 30.0
    BASE_Y_OFFSET = 20.0

    BASE_TAIL_SIZE = 15.0
    BASE_CORNER_RADIUS = 20.0
    BASE_LINE_SPACING = 4.0

    def __init__(self):
        self.texture = None
        self.width = 0
        self.height = 0
        self.display_until = 0.0
        self.position = (0, 0)

        self.shader_prog = None
        self.vbo = None
        self.vao = None

        self._font_cache = {}

    def init(self):
        self.shader_prog = self._create_shader_program()

        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)

        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            4 * 4 * ctypes.sizeof(ctypes.c_float),
            None,
            gl.GL_DYNAMIC_DRAW
        )

        pos_loc = gl.glGetAttribLocation(self.shader_prog, "a_position")
        tex_loc = gl.glGetAttribLocation(self.shader_prog, "a_texcoord")

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

    def _compile_shader(self, source, shader_type):
        shader = gl.glCreateShader(shader_type)
        gl.glShaderSource(shader, source)
        gl.glCompileShader(shader)

        if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
            raise RuntimeError(
                f"Shader compile error: {gl.glGetShaderInfoLog(shader)}"
            )

        return shader

    def _create_shader_program(self):
        vs_src = """
        #version 120
        attribute vec2 a_position;
        attribute vec2 a_texcoord;
        varying vec2 v_texcoord;
        void main() {
            gl_Position = vec4(a_position, 0.0, 1.0);
            v_texcoord = a_texcoord;
        }
        """

        fs_src = """
        #version 120
        uniform sampler2D u_texture;
        varying vec2 v_texcoord;
        void main() {
            gl_FragColor = texture2D(u_texture, v_texcoord);
        }
        """

        vs = self._compile_shader(vs_src, gl.GL_VERTEX_SHADER)
        fs = self._compile_shader(fs_src, gl.GL_FRAGMENT_SHADER)

        prog = gl.glCreateProgram()

        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        gl.glLinkProgram(prog)

        if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
            raise RuntimeError(
                f"Program link error: {gl.glGetProgramInfoLog(prog)}"
            )

        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)

        return prog

    def create_texture(
        self,
        text,
        font_paths,
        max_width=600,
        padding=35,
        tail_size=15,
        bg_color=(255, 182, 193, 255),
        text_color=(0, 0, 0, 255),
        font_size=18,
        corner_radius=20,
        vertical_shift=16,
        line_spacing=4
    ):
        def get_font(size):
            cache_key = size

            if cache_key in self._font_cache:
                return self._font_cache[cache_key]

            for path in font_paths:
                if os.path.exists(path):
                    try:
                        font = ImageFont.truetype(path, size)
                        self._font_cache[cache_key] = font
                        return font
                    except Exception:
                        pass

            font = ImageFont.load_default()
            self._font_cache[cache_key] = font

            return font

        font = get_font(font_size)

        if not text:
            text = " "

        lines = []
        line = ""

        for ch in text:
            test_line = line + ch

            if font.getlength(test_line) > max_width:
                lines.append(line)
                line = ch
            else:
                line = test_line

        if line:
            lines.append(line)

        if not lines:
            lines = [text]

        ascent, descent = font.getmetrics()
        standard_line_height = ascent + descent

        max_line_width = 0

        for ln in lines:
            max_line_width = max(max_line_width, font.getlength(ln))

        total_text_h = (
            standard_line_height * len(lines)
            + line_spacing * (len(lines) - 1)
        )

        bubble_w = int(max_line_width + padding * 2)
        bubble_h = int(total_text_h + padding * 2)

        # 防止圆角过大导致绘制异常
        if corner_radius > 0:
            max_radius = max(1, min(bubble_w // 2, bubble_h // 2))
            corner_radius = min(corner_radius, max_radius)

        total_w = bubble_w + tail_size
        total_h = bubble_h + tail_size

        img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if corner_radius > 0:
            draw.rounded_rectangle(
                [0, 0, bubble_w, bubble_h],
                radius=corner_radius,
                fill=bg_color,
                corners=(True, True, True, True)
            )
        else:
            draw.rectangle(
                [0, 0, bubble_w, bubble_h],
                fill=bg_color
            )

        # ===== 气泡尾巴横向锚点 =====
        if self.BASE_TAIL_SIZE > 0:
            tail_scale = max(0.2, float(tail_size) / float(self.BASE_TAIL_SIZE))
        else:
            tail_scale = 1.0

        tail_left = bubble_w - int(50 * tail_scale)
        tail_right = bubble_w - int(20 * tail_scale)
        tail_tip = bubble_w - int(35 * tail_scale)

        min_tail_width = max(2, int(8 * tail_scale))

        if (
            tail_left < 0 or
            tail_right > bubble_w or
            tail_right - tail_left < min_tail_width
        ):
            tail_left = max(0, bubble_w // 3)
            tail_right = min(bubble_w, (bubble_w * 2) // 3)

            if tail_right <= tail_left:
                tail_left = max(0, bubble_w // 4)
                tail_right = min(bubble_w, (bubble_w * 3) // 4)

            tail_tip = (tail_left + tail_right) // 2

        tail_tip = max(tail_left, min(tail_tip, tail_right))

        tail_points = [
            (tail_left, bubble_h),
            (tail_right, bubble_h),
            (tail_tip, bubble_h + tail_size)
        ]

        draw.polygon(tail_points, fill=bg_color)

        start_y = (bubble_h - total_text_h) // 2 + vertical_shift
        y_offset = start_y

        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font)
            x_text = (bubble_w - (bbox[2] - bbox[0])) // 2

            draw.text((x_text, y_offset), ln, font=font, fill=text_color)

            y_offset += standard_line_height + line_spacing

        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        img_data = np.array(img, dtype=np.uint8)

        tex_id = gl.glGenTextures(1)

        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexImage2D(
            gl.GL_TEXTURE_2D,
            0,
            gl.GL_RGBA,
            total_w,
            total_h,
            0,
            gl.GL_RGBA,
            gl.GL_UNSIGNED_BYTE,
            img_data
        )

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

        gl.glUseProgram(0)
        gl.glBindVertexArray(0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        return tex_id, total_w, total_h

    def show(
        self,
        text,
        win_width,
        duration=None,
        font_paths=None,
        max_width=None,
        padding=None,
        font_size=None,
        vertical_shift=None,
        x_offset=None,
        y_offset=None
    ):
        if text is None:
            text = ""

        if font_paths is None:
            font_paths = []

        if win_width is None or win_width <= 0:
            win_width = self.BASE_WINDOW_WIDTH

        # ===== 根据当前窗口宽度反推缩放档位 =====
        if self.BASE_WINDOW_WIDTH > 0:
            scale = float(win_width) / float(self.BASE_WINDOW_WIDTH)
        else:
            scale = 1.0

        if scale <= 0:
            scale = 1.0

        # 简单保护，避免异常值
        scale = max(0.35, min(2.0, scale))

        # ===== 默认气泡位置缩放 =====
        if x_offset is None:
            x_offset = max(1, int(round(self.BASE_X_OFFSET * scale)))

        if y_offset is None:
            y_offset = max(1, int(round(self.BASE_Y_OFFSET * scale)))

        # ===== 气泡内部视觉参数缩放 =====
        if padding is None:
            padding = max(6, int(round(self.BASE_PADDING * scale)))

        tail_size = max(4, int(round(self.BASE_TAIL_SIZE * scale)))
        corner_radius = max(6, int(round(self.BASE_CORNER_RADIUS * scale)))
        line_spacing = max(1, int(round(self.BASE_LINE_SPACING * scale)))

        if font_size is None:
            font_size = max(12, int(round(self.BASE_FONT_SIZE * scale)))

        if vertical_shift is None:
            vertical_shift = 0

        # ===== 气泡最大文本宽度缩放 =====
        if max_width is None:
            margin = max(5, int(round(10 * scale)))

            available = int(
                win_width
                - x_offset
                - tail_size
                - padding * 2
                - margin
            )

            available = max(10, available)
            scaled_max_width = max(10, int(round(self.BASE_MAX_WIDTH * scale)))

            max_width = max(10, min(scaled_max_width, available))

        if duration is None:
            duration = min(5.0, 2.0 + len(text) * 0.2)

        if self.texture is not None:
            gl.glDeleteTextures([self.texture])

        tex_id, w, h = self.create_texture(
            text,
            font_paths=font_paths,
            max_width=max_width,
            padding=padding,
            tail_size=tail_size,
            font_size=font_size,
            corner_radius=corner_radius,
            vertical_shift=vertical_shift,
            line_spacing=line_spacing
        )

        if tex_id is None:
            return

        self.texture = tex_id
        self.width, self.height = w, h
        self.display_until = time.time() + duration

        # ===== 位置保护，避免小窗口下气泡明显出界 =====
        margin = max(2, int(round(5 * scale)))

        if w > max(1, win_width - margin * 2):
            x = margin
        else:
            x = max(margin, min(int(x_offset), win_width - w - margin))

        y = max(margin, int(y_offset))

        self.position = (x, y)

    def draw(self, win_width, win_height):
        if win_width <= 0 or win_height <= 0 or self.texture is None:
            return

        if time.time() > self.display_until:
            gl.glDeleteTextures([self.texture])
            self.texture = None
            return

        if self.shader_prog is None:
            self.init()

        x, y = self.position
        w, h = self.width, self.height

        left = (x / win_width) * 2.0 - 1.0
        right = ((x + w) / win_width) * 2.0 - 1.0
        bottom = ((win_height - (y + h)) / win_height) * 2.0 - 1.0
        top = ((win_height - y) / win_height) * 2.0 - 1.0

        vertices = np.array(
            [
                left, bottom, 0.0, 0.0,
                right, bottom, 1.0, 0.0,
                right, top, 1.0, 1.0,
                left, top, 0.0, 1.0
            ],
            dtype=np.float32
        )

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

        gl.glPushAttrib(
            gl.GL_ENABLE_BIT | gl.GL_POLYGON_BIT | gl.GL_COLOR_BUFFER_BIT
        )
        gl.glPushClientAttrib(gl.GL_CLIENT_VERTEX_ARRAY_BIT)

        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        gl.glUseProgram(self.shader_prog)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)

        gl.glUniform1i(
            gl.glGetUniformLocation(self.shader_prog, "u_texture"),
            0
        )

        gl.glBindVertexArray(self.vao)
        gl.glDrawArrays(gl.GL_QUADS, 0, 4)

        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

        gl.glPopClientAttrib()
        gl.glPopAttrib()

    def cleanup(self):
        if self.texture is not None:
            gl.glDeleteTextures([self.texture])
            self.texture = None

        if self.vbo is not None:
            gl.glDeleteBuffers(1, [self.vbo])
            self.vbo = None

        if self.vao is not None:
            gl.glDeleteVertexArrays(1, [self.vao])
            self.vao = None

        if self.shader_prog is not None:
            gl.glDeleteProgram(self.shader_prog)
            self.shader_prog = None
