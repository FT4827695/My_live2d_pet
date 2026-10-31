# setting_button.py
# -*- coding: utf-8 -*-
"""
设置按钮组（可扩展）
"""

import os
import math
import time

import numpy as np
from OpenGL import GL as gl
from PIL import Image, ImageDraw

from settings_store import get_settings

# ==================== 布局常量 ====================

BASE_RADIUS = 28.0        # 按钮半径
BASE_MARGIN = 10.0        # 右边距
BASE_TOP_GAP = 120.0       # 顶部间距
BASE_BUTTON_GAP = 10.0    # 按钮之间的间距
ZONE_PADDING = 28.0       # 显示区域外扩
ZONE_PADDING_Y = 160.0     # 显示区域外扩（垂直方向）

HOVER_SPEED = 12.0        # 平滑速度
BASE_VISIBLE = 0.45       # 区域内未悬停时的可见度
HIDDEN_EPS = 0.01

SS = 3                    # 图标超采样倍数

# ==================== 音频状态（内存缓存） ====================

_settings = get_settings()
_audio_muted = _settings.get_bool("audio.muted", False)
_mixer_synced = None


def is_audio_muted():
    return _audio_muted


def _apply_mixer_state(force=False):
    global _mixer_synced
    if not force and _mixer_synced == _audio_muted:
        return
    try:
        import pygame
        if pygame.mixer.get_init() is None:
            return
        pygame.mixer.set_volume(0.0 if _audio_muted else 1.0)
        _mixer_synced = _audio_muted
    except Exception:
        pass


def enforce_mixer_state():
    _apply_mixer_state(force=True)


def set_audio_muted(v):
    global _audio_muted
    new_state = bool(v)
    if new_state == _audio_muted:
        return
    _audio_muted = new_state
    _settings.set("audio.muted", new_state)   # 写盘只在这里
    if new_state:
        try:
            import pygame
            if pygame.mixer.get_init() is not None:
                pygame.mixer.stop()
        except Exception:
            pass
    _apply_mixer_state()


def is_background_enabled():
    return _settings.get_bool("background.enabled", False)


def set_background_enabled(v):
    _settings.set("background.enabled", bool(v))


# ==================== 图标生成 ====================

def _draw_speaker(d, cx, cy, s, color, muted):
    """喇叭图标"""
    bw = s * 0.16
    body = [
        (cx - s * 0.34, cy - s * 0.12),
        (cx - s * 0.16, cy - s * 0.12),
        (cx + s * 0.02, cy - s * 0.30),
        (cx + s * 0.02, cy + s * 0.30),
        (cx - s * 0.16, cy + s * 0.12),
        (cx - s * 0.34, cy + s * 0.12),
    ]
    d.polygon(body, fill=color)
    if muted:
        w = max(2, int(s * 0.075))
        d.line([(cx + s * 0.10, cy - s * 0.20), (cx + s * 0.38, cy + s * 0.20)],
               fill=color, width=w)
        d.line([(cx + s * 0.38, cy - s * 0.20), (cx + s * 0.10, cy + s * 0.20)],
               fill=color, width=w)
    else:
        w = max(2, int(s * 0.065))
        for i, r in enumerate((0.16, 0.28)):
            box = [cx + s * 0.02 - s * r, cy - s * r,
                   cx + s * 0.02 + s * r, cy + s * r]
            d.arc(box, start=-55, end=55, fill=color, width=w)


def _draw_picture(d, cx, cy, s, color, enabled):
    """相框 / 图片图标"""
    w = max(2, int(s * 0.07))
    x0, y0 = cx - s * 0.34, cy - s * 0.26
    x1, y1 = cx + s * 0.34, cy + s * 0.26
    d.rounded_rectangle([x0, y0, x1, y1], radius=s * 0.08,
                        outline=color, width=w)
    d.ellipse([cx - s * 0.20, cy - s * 0.16,
               cx - s * 0.06, cy - s * 0.02], fill=color)
    d.polygon([
        (x0 + w, y1 - w),
        (cx - s * 0.04, cy + s * 0.02),
        (cx + s * 0.10, cy + s * 0.16),
        (cx + s * 0.20, cy + s * 0.06),
        (x1 - w, y1 - w),
    ], fill=color)
    if not enabled:
        lw = max(2, int(s * 0.075))
        d.line([(x0 - s * 0.04, y1 + s * 0.04), (x1 + s * 0.04, y0 - s * 0.04)],
               fill=color, width=lw)


_ICON_DRAWERS = {
    "audio": _draw_speaker,
    "background": _draw_picture,
}


def _make_button_image(diameter, kind, active):
    size = max(8, int(diameter * SS))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size * 0.06
    box = [pad, pad, size - pad, size - pad]

    d.ellipse(box, fill=(24, 26, 32, 205))

    if kind == "audio":
        ring = (235, 120, 120, 235) if active else (150, 205, 255, 235)
        icon = (240, 240, 245, 255)
    else:
        ring = (150, 205, 255, 235) if active else (140, 145, 155, 220)
        icon = (240, 240, 245, 255) if active else (170, 175, 185, 255)

    d.ellipse(box, outline=ring, width=max(2, int(size * 0.045)))
    _ICON_DRAWERS[kind](d, size / 2.0, size / 2.0, size * 0.62, icon, active)

    img = img.resize((max(8, int(diameter)), max(8, int(diameter))),
                     Image.LANCZOS)

    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr[..., :3] *= arr[..., 3:4]           # 预乘
    return (arr * 255.0).astype(np.uint8).tobytes(), img.size[0], img.size[1]


# ==================== Shader ====================

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


def _compile(vs_src, fs_src):
    vs = gl.glCreateShader(gl.GL_VERTEX_SHADER)
    gl.glShaderSource(vs, vs_src)
    gl.glCompileShader(vs)
    if not gl.glGetShaderiv(vs, gl.GL_COMPILE_STATUS):
        raise RuntimeError(gl.glGetShaderInfoLog(vs))
    fs = gl.glCreateShader(gl.GL_FRAGMENT_SHADER)
    gl.glShaderSource(fs, fs_src)
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
    return p


def _upload_tex(data, w, h, old=0):
    if old:
        _delete_tex(old)
    tid = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tid


def _delete_tex(tid):
    try:
        gl.glDeleteTextures([tid])
    except Exception:
        try:
            gl.glDeleteTextures(1, [tid])
        except Exception:
            pass


# ==================== 单个按钮 ====================

class _ButtonItem:
    def __init__(self, bid, kind, get_state, on_click):
        self.bid = bid
        self.kind = kind
        self.get_state = get_state
        self.on_click = on_click
        self.tex_off = 0
        self.tex_on = 0
        self.hover_t = 0.0
        self.cx = 0.0
        self.cy = 0.0


# ==================== 按钮组 ====================

class SettingButtonGroup:
    def __init__(self, pet, ui_scale=1.0):
        self.pet = pet
        self.ui_scale = float(ui_scale)

        self._win_w = 0
        self._win_h = 0
        self._radius = 0.0
        self._tex_size = 0

        self._program = 0
        self._vao = 0
        self._vbo = 0
        self._gl_ready = False
        self._next_rebuild_try = 0.0

        self._group_alpha = 0.0
        self._last_tick = 0.0
        self.hovered = False

        self._buttons = [
            _ButtonItem("audio", "audio",
                        lambda: is_audio_muted(),
                        self._on_audio_click),
            _ButtonItem("background", "background",
                        lambda: is_background_enabled(),
                        self._on_background_click),
        ]

    # ---------- 点击行为 ----------
    def _on_audio_click(self):
        new_state = not is_audio_muted()
        set_audio_muted(new_state)
        print(f"[SettingButton] 音频静音 = {new_state}")

    def _on_background_click(self):
        new_state = not is_background_enabled()
        set_background_enabled(new_state)
        bg = getattr(self.pet, "background", None)
        if bg is not None:
            bg.set_enabled(new_state)
        print(f"[SettingButton] 背景启用 = {new_state}")

    # ---------- 布局 ----------
    def _relayout(self, win_w, win_h):
        s = self.ui_scale
        r = BASE_RADIUS * s
        self._radius = r
        cx = win_w - BASE_MARGIN * s - r
        cy0 = BASE_MARGIN * s + r + BASE_TOP_GAP * s
        step = r * 2.0 + BASE_BUTTON_GAP * s
        for i, b in enumerate(self._buttons):
            b.cx = cx
            b.cy = cy0 + i * step

    def _show_zone(self):
        if not self._buttons:
            return (0, 0, 0, 0)
        r = self._radius
        pad_x = ZONE_PADDING * self.ui_scale
        pad_y = ZONE_PADDING_Y * self.ui_scale
        first = self._buttons[0]
        last = self._buttons[-1]
        x0 = first.cx - r - pad_x
        y0 = first.cy - r - pad_y
        x1 = float(self._win_w)
        y1 = last.cy + r + pad_y
        return (x0, y0, x1, y1)
    
    def _hit_test(self, x, y):
        r = self._radius
        for b in self._buttons:
            dx = x - b.cx
            dy = y - b.cy
            if dx * dx + dy * dy <= r * r:
                return b
        return None

    def _hidden_by_chat(self):
        chat = getattr(self.pet, "chat_window", None)
        return chat is not None and getattr(chat, "active", False)

    def _cursor_rel(self):
        try:
            import glfw
            win = getattr(self.pet, "window", None)
            if win is None:
                return None
            sx, sy = glfw.get_cursor_pos(win)
            wx, wy = glfw.get_window_pos(win)
            return (sx, sy)
        except Exception:
            return None

    # ---------- GL ----------
    def _init_gl(self):
        if self._program:
            return True
        try:
            self._program = _compile(_VS, _FS)
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
            print(f"[SettingButton] GL 初始化失败: {e}")
            self._program = 0
            return False

    def _rebuild_textures(self):
        diameter = int(max(8, self._radius * 2.0))
        if diameter == self._tex_size and self._gl_ready:
            return
        try:
            if not self._init_gl():
                return
            for b in self._buttons:
                d0, w, h = _make_button_image(diameter, b.kind, False)
                d1, _, _ = _make_button_image(diameter, b.kind, True)
                b.tex_off = _upload_tex(d0, w, h, b.tex_off)
                b.tex_on = _upload_tex(d1, w, h, b.tex_on)
            self._tex_size = diameter
            self._gl_ready = True
        except Exception as e:
            print(f"[SettingButton] 纹理重建失败: {e}")
            self._gl_ready = False

    def set_scale(self, ui_scale):
        ui_scale = float(ui_scale)
        if abs(ui_scale - self.ui_scale) < 1e-6:
            return
        self.ui_scale = ui_scale
        self._tex_size = 0
        self._gl_ready = False

    # ---------- 每帧 ----------
    def tick_and_draw(self, now, win_w, win_h, cursor_rel=None):
        if self._hidden_by_chat():
            self.hovered = False
            self._group_alpha = 0.0
            self._last_tick = now
            return

        self.set_scale(getattr(self.pet, "ui_scale", self.ui_scale))

        if win_w != self._win_w or win_h != self._win_h:
            self._win_w = int(win_w)
            self._win_h = int(win_h)
            self._relayout(self._win_w, self._win_h)
            self._tex_size = 0
            self._gl_ready = False

        if not self._gl_ready:
            if now >= self._next_rebuild_try:
                self._next_rebuild_try = now + 1.0
                self._rebuild_textures()
            if not self._gl_ready:
                return

        if self._last_tick <= 0.0:
            dt = 0.0
        else:
            dt = max(0.0, min(0.1, now - self._last_tick))
        self._last_tick = now

        if cursor_rel is None:
            cursor_rel = self._cursor_rel()

        if cursor_rel is None:
            in_zone = False
            rx = ry = -1.0
        else:
            rx, ry = cursor_rel
            x0, y0, x1, y1 = self._show_zone()
            in_zone = (x0 <= rx <= x1) and (y0 <= ry <= y1)

        hit = self._hit_test(rx, ry) if in_zone else None
        self.hovered = hit is not None

        target = 1.0 if in_zone else 0.0
        k = min(1.0, HOVER_SPEED * dt)
        self._group_alpha += (target - self._group_alpha) * k
        if self._group_alpha < HIDDEN_EPS and target == 0.0:
            self._group_alpha = 0.0
            return

        for b in self._buttons:
            bt = 1.0 if (hit is b) else 0.0
            b.hover_t += (bt - b.hover_t) * k

        self._draw()

    def _draw(self):
        r = self._radius
        verts = []
        for b in self._buttons:
            a = self._group_alpha * (BASE_VISIBLE + (1.0 - BASE_VISIBLE) * b.hover_t)
            if a <= HIDDEN_EPS:
                continue
            verts.append((b, a))
        if not verts:
            return

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)  # 预乘
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(self._program)
        gl.glUniform2f(gl.glGetUniformLocation(self._program, "uScreen"),
                       float(self._win_w), float(self._win_h))
        gl.glUniform1i(gl.glGetUniformLocation(self._program, "uTex"), 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindVertexArray(self._vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)

        for b, a in verts:
            x0, y0 = b.cx - r, b.cy - r
            x1, y1 = b.cx + r, b.cy + r
            quad = np.array([
                x0, y0, 0.0, 0.0,
                x1, y0, 1.0, 0.0,
                x1, y1, 1.0, 1.0,
                x0, y0, 0.0, 0.0,
                x1, y1, 1.0, 1.0,
                x0, y1, 0.0, 1.0,
            ], dtype=np.float32)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_DYNAMIC_DRAW)

            tex = b.tex_on if b.get_state() else b.tex_off
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
            gl.glUniform1f(gl.glGetUniformLocation(self._program, "uAlpha"), float(a))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)

        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glUseProgram(0)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    # ---------- 点击 ----------
    def handle_click(self, x, y, *args, **kwargs):
        if self._hidden_by_chat():
            return False
        if self._group_alpha <= 0.05 or not self._gl_ready:
            return False
        try:
            b = self._hit_test(float(x), float(y))
        except Exception:
            return False
        if b is None:
            return False
        try:
            b.on_click()
        except Exception as e:
            print(f"[SettingButton] 点击处理失败: {e}")
        return True

    # 兼容可能的外部命名
    handle_mouse_click = handle_click
    on_click = handle_click

    def destroy(self):
        for b in self._buttons:
            _delete_tex(b.tex_off)
            _delete_tex(b.tex_on)
            b.tex_off = b.tex_on = 0
        self._gl_ready = False


# 兼容旧类名
SettingButton = SettingButtonGroup
