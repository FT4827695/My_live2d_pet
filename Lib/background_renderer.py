# background_renderer.py
# -*- coding: utf-8 -*-
"""
静态背景渲染器（PoC）
"""

import os
import time

import numpy as np
from OpenGL import GL as gl
from PIL import Image

from settings_store import get_settings, BASE_DIR

MAX_TEX_SIDE = 2048
DIM_SPEED = 6.0
RETRY_INTERVAL = 3.0

_VS = """
#version 330 core
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUV;
uniform vec2 uScreen;
out vec2 vUV;
void main(){
    vec2 ndc = vec2(aPos.x / uScreen.x * 2.0 - 1.0,
                    1.0 - aPos.y / uScreen.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    vUV = aUV;
}
"""

_FS = """
#version 330 core
in vec2 vUV;
uniform sampler2D uTex;
uniform float uAlpha;
out vec4 FragColor;
void main(){
    FragColor = texture(uTex, vUV) * uAlpha;
}
"""


def _resolve_path(name):
    cands = [
        os.path.join(BASE_DIR, name),
        os.path.join(BASE_DIR, "Image", name),
        os.path.join(BASE_DIR, "backgrounds", name),
        os.path.join(BASE_DIR, "Image", "backgrounds", name),
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return None

class BackgroundRenderer:
    def __init__(self, pet):
        self.pet = pet
        s = get_settings()
        self.enabled = s.get_bool("background.enabled", False)
        self._file = s.get("background.file", "background.png")
        self._opacity = s.get_float("background.opacity", 1.0)
        self._dim_on_chat = s.get_bool("background.dim_on_chat", True)
        self._dim_alpha = s.get_float("background.dim_alpha", 0.45)

        self._tex = 0
        self._tex_w = 0
        self._tex_h = 0
        self._program = 0
        self._vao = 0
        self._vbo = 0
        self._gl_ready = False

        self._alpha = 1.0
        self._last_tick = 0.0
        self._next_try = 0.0
        self._load_failed = False

    # ---------- 开关（按钮点击时调用） ----------
    def set_enabled(self, v):
        self.enabled = bool(v)
        if self.enabled:
            self._next_try = 0.0   # 允许立刻重试加载

    def init(self):
        self._last_tick = 0.0

    # ---------- GL ----------
    def _init_gl(self):
        if self._program:
            return True
        try:
            vs = gl.glCreateShader(gl.GL_VERTEX_SHADER)
            gl.glShaderSource(vs, _VS)
            gl.glCompileShader(vs)
            if not gl.glGetShaderiv(vs, gl.GL_COMPILE_STATUS):
                raise RuntimeError(gl.glGetShaderInfoLog(vs))
            fs = gl.glCreateShader(gl.GL_FRAGMENT_SHADER)
            gl.glShaderSource(fs, _FS)
            gl.glCompileShader(fs)
            if not gl.glGetShaderiv(fs, gl.GL_COMPILE_STATUS):
                raise RuntimeError(gl.glGetShaderInfoLog(fs))
            p = gl.glCreateProgram()
            gl.glAttachShader(p, vs)
            gl.glAttachShader(p, fs)
            gl.glLinkProgram(p)
            if not gl.glGetProgramiv(p, gl.GL_LINK_STATUS):
                raise RuntimeError(gl.glGetProgramInfoLog(p))
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            self._program = p

            self._vao = int(gl.glGenVertexArrays(1))
            self._vbo = int(gl.glGenBuffers(1))
            gl.glBindVertexArray(self._vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 16, None)
            gl.glEnableVertexAttribArray(1)
            gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, 16,
                                     gl.ctypes.c_void_p(8))
            gl.glBindVertexArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            return True
        except Exception as e:
            print(f"[Background] GL 初始化失败: {e}")
            self._program = 0
            return False

    def _load_texture(self):
        path = _resolve_path(self._file)
        if path is None:
            if not self._load_failed:
                print(f"[Background] 未找到背景图: {self._file}")
                self._load_failed = True
            return False
        try:
            img = Image.open(path).convert("RGBA")
            w, h = img.size
            side = max(w, h)
            if side > MAX_TEX_SIDE:
                k = MAX_TEX_SIDE / float(side)
                img = img.resize((max(1, int(w * k)), max(1, int(h * k))),
                                 Image.LANCZOS)
            self._tex_w, self._tex_h = img.size

            arr = np.asarray(img, dtype=np.float32) / 255.0
            arr[..., :3] *= arr[..., 3:4]      # 预乘
            data = (arr * 255.0).astype(np.uint8).tobytes()

            if self._tex:
                try:
                    gl.glDeleteTextures([self._tex])
                except Exception:
                    gl.glDeleteTextures(1, [self._tex])
            tid = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
                            self._tex_w, self._tex_h, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            self._tex = tid
            self._load_failed = False
            print(f"[Background] 已加载 {os.path.basename(path)} "
                  f"({self._tex_w}x{self._tex_h})")
            return True
        except Exception as e:
            print(f"[Background] 加载失败: {e}")
            self._load_failed = True
            return False

    # ---------- 每帧 ----------
    def tick_and_draw(self, now, win_w, win_h, chat_active=False):
        if not self.enabled:
            self._last_tick = now
            return
        if win_w <= 0 or win_h <= 0:
            return

        if not self._gl_ready:
            if not self._init_gl():
                return
            self._gl_ready = True

        if not self._tex:
            if now < self._next_try:
                return
            self._next_try = now + RETRY_INTERVAL
            if not self._load_texture():
                return

        # 平滑变淡
        if self._last_tick <= 0.0:
            dt = 0.0
        else:
            dt = max(0.0, min(0.1, now - self._last_tick))
        self._last_tick = now

        target = self._dim_alpha if (chat_active and self._dim_on_chat) else 1.0
        target *= self._opacity
        self._alpha += (target - self._alpha) * min(1.0, DIM_SPEED * dt)
        if self._alpha <= 0.01:
            return

        self._draw(float(win_w), float(win_h))

    def _draw(self, win_w, win_h):
        scale = max(win_w / self._tex_w, win_h / self._tex_h)
        disp_w = self._tex_w * scale
        disp_h = self._tex_h * scale
        hu = (win_w / disp_w) * 0.5
        hv = (win_h / disp_h) * 0.5
        u0, u1 = 0.5 - hu, 0.5 + hu
        v0, v1 = 0.5 - hv, 0.5 + hv

        quad = np.array([
            0.0,   0.0,   u0, v0,
            win_w, 0.0,   u1, v0,
            win_w, win_h, u1, v1,
            0.0,   0.0,   u0, v0,
            win_w, win_h, u1, v1,
            0.0,   win_h, u0, v1,
        ], dtype=np.float32)

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)  # 预乘
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(self._program)
        gl.glUniform2f(gl.glGetUniformLocation(self._program, "uScreen"), win_w, win_h)
        gl.glUniform1i(gl.glGetUniformLocation(self._program, "uTex"), 0)
        gl.glUniform1f(gl.glGetUniformLocation(self._program, "uAlpha"), float(self._alpha))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
        gl.glBindVertexArray(self._vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_DYNAMIC_DRAW)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glUseProgram(0)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    def destroy(self):
        if self._tex:
            try:
                gl.glDeleteTextures([self._tex])
            except Exception:
                try:
                    gl.glDeleteTextures(1, [self._tex])
                except Exception:
                    pass
            self._tex = 0
        self._gl_ready = False
