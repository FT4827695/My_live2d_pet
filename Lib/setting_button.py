# -- coding: utf-8 --
import os
import sys
import json
import time as _time
import numpy as np
import OpenGL.GL as gl
import glfw
from PIL import Image, ImageDraw

try:
    import win32api
except Exception:
    win32api = None

# ==================== 音频静音控制 ====================
_state_loaded = False
_audio_muted = False
_state_file_path = None


def _get_state_file_path():
    global _state_file_path
    if _state_file_path is not None:
        return _state_file_path
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    _state_file_path = os.path.join(base, "mute_state.json")
    return _state_file_path


def _load_state_once():
    global _state_loaded, _audio_muted
    if _state_loaded:
        return
    _state_loaded = True
    try:
        path = _get_state_file_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _audio_muted = bool(data.get("audio_muted", False))
    except Exception as e:
        print(f"[SettingButton] 读取静音状态失败: {e}")


def _save_state():
    try:
        path = _get_state_file_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"audio_muted": _audio_muted}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def is_audio_muted():
    _load_state_once()
    return _audio_muted


def enforce_mixer_state():
    try:
        import pygame
        if pygame.mixer.get_init() is None:
            return
        target = 0.0 if _audio_muted else 1.0
        if abs(pygame.mixer.get_volume() - target) > 1e-6:
            pygame.mixer.set_volume(target)
    except Exception:
        pass


def set_audio_muted(v):
    global _audio_muted
    _load_state_once()
    new_state = bool(v)
    changed = (new_state != _audio_muted)
    _audio_muted = new_state
    _save_state()
    if not changed:
        return
    if _audio_muted:
        try:
            import pygame
            if pygame.mixer.get_init() is not None:
                pygame.mixer.stop()
        except Exception:
            pass
    enforce_mixer_state()


# ==================== 纹理生成与预乘处理 ====================
def _draw_speaker_icon(size, muted):
    size = max(32, int(size))
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 深色半透明圆底
    margin = int(s * 0.05)
    draw.ellipse([margin, margin, s - margin, s - margin], fill=(20, 20, 20, 220))
    # 外发光边框（根据状态改变颜色）
    border_color = (255, 100, 100, 255) if muted else (200, 200, 200, 255)
    draw.ellipse([margin, margin, s - margin, s - margin],
                 outline=border_color, width=max(2, int(s * 0.03)))
    # 喇叭图形
    fg = (255, 80, 80, 255) if muted else (255, 255, 255, 255)
    cy = s * 0.50
    body = [
        (s * 0.22, s * 0.42), (s * 0.34, s * 0.42), (s * 0.48, s * 0.28),
        (s * 0.48, s * 0.72), (s * 0.34, s * 0.58), (s * 0.22, s * 0.58),
    ]
    draw.polygon(body, fill=fg)
    draw.rectangle([s * 0.16, s * 0.44, s * 0.24, s * 0.56], fill=fg)
    if not muted:
        arc_cx = s * 0.54
        for rp_ratio, wd_ratio in ((0.10, 0.035), (0.18, 0.030), (0.26, 0.025)):
            rp = int(s * rp_ratio)
            wd = max(2, int(s * wd_ratio))
            draw.arc([int(arc_cx - rp), int(cy - rp), int(arc_cx + rp), int(cy + rp)],
                     start=-55, end=55, fill=fg, width=wd)
    else:
        wd = max(2, int(s * 0.05))
        draw.line([(s * 0.60, s * 0.34), (s * 0.84, s * 0.66)], fill=fg, width=wd)
        draw.line([(s * 0.60, s * 0.66), (s * 0.84, s * 0.34)], fill=fg, width=wd)
    img = img.resize((size, size), Image.LANCZOS)
    # 预乘 Alpha，配合 (ONE, ONE_MINUS_SRC_ALPHA) 混合
    data = np.array(img, dtype=np.float32)
    alpha = data[:, :, 3:4] / 255.0
    data[:, :, 0:3] *= alpha
    return Image.fromarray(data.astype(np.uint8), "RGBA")


def _upload_texture(img):
    data = img.tobytes("raw", "RGBA")
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return int(tex)


# ==================== SettingButton ====================
class SettingButton:
    BASE_RADIUS = 26
    BASE_MARGIN = 16
    BASE_TOP_GAP = 110 
    HOVER_SPEED = 15.0
    MIN_VISIBLE_ALPHA = 0.15  # 窗口内常态半透明
    HIDDEN_EPS = 0.02
    HOVER_ALPHA = 0.95         # 悬停高亮透明度
    HIDDEN_ALPHA = 0.0         # 窗口外
    
    def __init__(self, pet):
        self.pet = pet
        self._ui_scale = 1.0
        self._win_w = 800
        self._win_h = 600
        self._radius = self.BASE_RADIUS
        self._margin = self.BASE_MARGIN
        self._top_gap = self.BASE_TOP_GAP
        self.hovered = False
        self.hover_alpha = 0.0
        self._last_tick = 0.0
        self._tex_normal = 0
        self._tex_muted = 0
        self._icon_size = 0
        self._gl_ready = False
        self._next_rebuild_try = 0.0

    def set_scale(self, ui_scale, win_w, win_h):
        self._ui_scale = float(ui_scale) if ui_scale else 1.0
        self._win_w = int(win_w)
        self._win_h = int(win_h)
        self._radius = max(20, int(round(self.BASE_RADIUS * self._ui_scale)))
        self._margin = max(8, int(round(self.BASE_MARGIN * self._ui_scale)))
        self._top_gap = max(40, int(round(self.BASE_TOP_GAP * self._ui_scale)))
        target_icon = self._radius * 2
        if (not self._gl_ready) or abs(target_icon - self._icon_size) >= 1:
            self._rebuild_textures(target_icon)

    def _rebuild_textures(self, icon_size):
        try:
            if self._tex_normal:
                gl.glDeleteTextures([self._tex_normal])
            if self._tex_muted:
                gl.glDeleteTextures([self._tex_muted])
            self._tex_normal = 0
            self._tex_muted = 0
        except Exception:
            pass
        try:
            self._tex_normal = _upload_texture(_draw_speaker_icon(icon_size, muted=False))
            self._tex_muted = _upload_texture(_draw_speaker_icon(icon_size, muted=True))
            self._icon_size = icon_size
            self._gl_ready = True
            print(f"[SettingButton] 纹理重建完成 size={icon_size}")
        except Exception as e:
            print(f"[SettingButton] 纹理创建失败: {e}")
            self._gl_ready = False

    def _get_center(self):
        cx = self._win_w - self._margin - self._radius
        cy = self._margin + self._radius + self._top_gap   # 下移后的中心
        return cx, cy

    def _hit_test(self, x, y):
        if not self._gl_ready or self._radius <= 0:
            return False
        cx, cy = self._get_center()
        dx, dy = x - cx, y - cy
        return dx * dx + dy * dy <= self._radius * self._radius

    def _cursor_rel(self):
        try:
            if win32api is not None:
                sx, sy = win32api.GetCursorPos()
            else:
                import pyautogui
                sx, sy = pyautogui.position()
        except Exception:
            return None
        win = getattr(self.pet, "window", None)
        if win is None:
            return None
        try:
            wx, wy = glfw.get_window_pos(win)
        except Exception:
            return None
        return float(sx - wx), float(sy - wy)

    def update_hover(self, x, y):
        try:
            self.hovered = self._hit_test(float(x), float(y))
        except Exception:
            self.hovered = False

    def try_click(self, x, y):
        if self._hidden_by_chat():
            return False
        if self.hover_alpha <= 0.05:
            return False
        try:
            hit = self._hit_test(float(x), float(y))
        except Exception:
            return False
        if not hit:
            return False
        new_state = not is_audio_muted()
        set_audio_muted(new_state)
        print(f"[SettingButton] 音频静音 = {new_state}")
        return True

    def _hidden_by_chat(self):
        chat = getattr(self.pet, "chat_window", None)
        return chat is not None and getattr(chat, "active", False)

    def tick_and_draw(self, now, win_w, win_h, cursor_rel=None):
        enforce_mixer_state()

        if self._hidden_by_chat():
            self.hovered = False
            return
        if win_w != self._win_w or win_h != self._win_h:
            self._win_w = int(win_w)
            self._win_h = int(win_h)
        if not self._gl_ready:
            if now >= self._next_rebuild_try:
                self._next_rebuild_try = now + 1.0
                self._rebuild_textures(self._radius * 2)
            if not self._gl_ready:
                return
            
        if self._last_tick <= 0.0:
            self._last_tick = now
            dt = 0.0
        else:
            dt = max(0.0, min(0.1, now - self._last_tick))
            self._last_tick = now

        # ===== 每帧重算可见性 / 悬停 =====
        if cursor_rel is None:
            cursor_rel = self._cursor_rel()   # 兜底路径
        if cursor_rel is None:
            in_window = False
            rx = ry = -1.0
        else:
            rx, ry = cursor_rel
            in_window = (0.0 <= rx <= self._win_w) and (0.0 <= ry <= self._win_h)
        
        self.hovered = in_window and self._hit_test(rx, ry)
        if not in_window:
            target = self.HIDDEN_ALPHA
        elif self.hovered:
            target = self.HOVER_ALPHA
        else:
            target = self.MIN_VISIBLE_ALPHA
            
        if dt > 0.0:
            k = min(1.0, self.HOVER_SPEED * dt)
            self.hover_alpha += (target - self.hover_alpha) * k
            if abs(self.hover_alpha - target) < 0.005:
                self.hover_alpha = target
        alpha = self.hover_alpha

        if alpha <= self.HIDDEN_EPS:
            return  # 完全隐藏

        cx, cy = self._get_center()
        r = self._radius
        tex = self._tex_muted if is_audio_muted() else self._tex_normal
        if tex <= 0:
            return

        # === 保存 GL 状态，彻底隔离 ===
        gl.glPushAttrib(gl.GL_ALL_ATTRIB_BITS)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glPushMatrix()
        gl.glLoadIdentity()
        gl.glOrtho(0, self._win_w, self._win_h, 0, -1, 1)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glPushMatrix()
        gl.glLoadIdentity()

        gl.glUseProgram(0)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_ALPHA_TEST)
        try:
            gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
            gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
            gl.glDisableClientState(gl.GL_COLOR_ARRAY)
        except Exception:
            pass
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexEnvi(gl.GL_TEXTURE_ENV, gl.GL_TEXTURE_ENV_MODE, gl.GL_MODULATE)

        gl.glColor4f(alpha, alpha, alpha, alpha)
        x0, y0, x1, y1 = cx - r, cy - r, cx + r, cy + r
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0.0, 1.0); gl.glVertex2f(x0, y0)
        gl.glTexCoord2f(1.0, 1.0); gl.glVertex2f(x1, y0)
        gl.glTexCoord2f(1.0, 0.0); gl.glVertex2f(x1, y1)
        gl.glTexCoord2f(0.0, 0.0); gl.glVertex2f(x0, y1)
        gl.glEnd()

        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glPopMatrix()
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glPopMatrix()
        gl.glPopAttrib()
        gl.glColor4f(1.0, 1.0, 1.0, 1.0)
