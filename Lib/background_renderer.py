# -- coding: utf-8 --
"""
静态背景渲染器
"""
import os
import numpy as np
from OpenGL import GL as gl
from PIL import Image
from settings_store import get_settings, BASE_DIR

MAX_TEX_SIDE = 2048
DIM_SPEED = 6.0
RETRY_INTERVAL = 3.0

# ================= 外观常量（基准像素） =================
CORNER_RADIUS_PX   = 16.0   # 圆角半径
SHADOW_BLUR_PX     = 8.0    # 外阴影模糊宽度
SHADOW_OFFSET_Y_PX = 2.5    # 外阴影向下偏移
SHADOW_OPACITY     = 0.65   # 外阴影不透明度
CARD_MARGIN_PX     = SHADOW_BLUR_PX + SHADOW_OFFSET_Y_PX

_VS = """
#version 330 core
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUV;
uniform vec2 uScreen;
out vec2 vUV;
out vec2 vPos;      // 屏幕坐标传给片元着色器
void main(){
    vec2 ndc = vec2(aPos.x / uScreen.x * 2.0 - 1.0,
                    1.0 - aPos.y / uScreen.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    vUV = aUV;
    vPos = aPos;
}
"""

_FS = """
#version 330 core
in vec2 vUV;
in vec2 vPos;
uniform sampler2D uTex;
uniform float uAlpha;
// Uniform：圆角和阴影计算
uniform vec2 uWinSize;       // 窗口大小
uniform float uMargin;       // 背景图距离窗口边缘的内边距
uniform float uRadius;       // 圆角半径
uniform float uShadowBlur;   // 阴影模糊程度
uniform vec2 uShadowOffset;  // 阴影向下偏移量
uniform float uShadowOpacity;// 阴影不透明度
uniform vec4 uUVRect;        // cover 裁剪区 (u0,u1,v0,v1)，UV 空间保险栓
out vec4 FragColor;
void main(){
    vec2 center = uWinSize * 0.5;
    vec2 p = vPos - center;                  // 当前像素相对于中心的坐标
    vec2 halfSize = center - vec2(uMargin);  // 背景矩形的半宽高
    // --- 1. 背景矩形 SDF ---
    vec2 d = abs(p) - halfSize + vec2(uRadius);
    float dist = length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - uRadius;
    // --- 2. 阴影 SDF ---
    vec2 sp = p - uShadowOffset;
    vec2 sd = abs(sp) - halfSize + vec2(uRadius);
    float sdist = length(max(sd, 0.0)) + min(max(sd.x, sd.y), 0.0) - uRadius;
    // --- 3. 平滑遮罩 ---
    float mask = smoothstep(1.0, -1.0, dist);
    float shadow_mask = smoothstep(uShadowBlur, 0.0, sdist) * uShadowOpacity;
    shadow_mask *= (1.0 - mask);             // 背景内部不叠阴影
    // --- 4. 采样背景并预乘 Alpha ---
    vec4 texColor = texture(uTex, vUV);
    float inRect = step(uUVRect.x, vUV.x) * step(vUV.x, uUVRect.y)
                 * step(uUVRect.z, vUV.y) * step(vUV.y, uUVRect.w);
    float bg_a = texColor.a * mask * inRect;
    vec3 bg_rgb = texColor.rgb * mask * inRect;
    // --- 5. 混合阴影和背景 ---
    float out_a = bg_a + shadow_mask * (1.0 - bg_a);
    FragColor = vec4(bg_rgb * uAlpha, out_a * uAlpha);
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
        # ---- 缓存 ----
        self._loc = {}          # uniform location 一次性缓存
        self._layout_key = None  # (win_w, win_h, ui_scale, tex_w, tex_h)

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
            # uniform location 一次性缓存
            names = ("uScreen", "uTex", "uAlpha", "uWinSize", "uMargin",
                     "uRadius", "uShadowBlur", "uShadowOffset",
                     "uShadowOpacity", "uUVRect")
            self._loc = {n: gl.glGetUniformLocation(p, n) for n in names}
            gl.glUseProgram(p)
            gl.glUniform1i(self._loc["uTex"], 0)
            gl.glUseProgram(0)
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
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
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
            self._layout_key = None
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

    # ---------- 几何量重建（仅窗口/缩放/纹理变化时执行） ----------
    def _upload_layout(self, win_w, win_h, scale_ui):
        margin = CARD_MARGIN_PX * scale_ui
        rect_w = win_w - 2.0 * margin
        rect_h = win_h - 2.0 * margin
        scale = max(rect_w / self._tex_w, rect_h / self._tex_h)
        disp_w = self._tex_w * scale
        disp_h = self._tex_h * scale
        hu = (rect_w / disp_w) * 0.5
        hv = (rect_h / disp_h) * 0.5
        u0, u1 = 0.5 - hu, 0.5 + hu
        v0, v1 = 0.5 - hv, 0.5 + hv
        margin_x = margin / rect_w
        margin_y = margin / rect_h
        u_start = u0 - margin_x * (u1 - u0)
        u_end   = u1 + margin_x * (u1 - u0)
        v_start = v0 - margin_y * (v1 - v0)
        v_end   = v1 + margin_y * (v1 - v0)
        # 铺满整个窗口（含阴影带）
        quad = np.array([
            0.0,   0.0,   u_start, v_start,
            win_w, 0.0,   u_end,   v_start,
            win_w, win_h, u_end,   v_end,
            0.0,   0.0,   u_start, v_start,
            win_w, win_h, u_end,   v_end,
            0.0,   win_h, u_start, v_end,
        ], dtype=np.float32)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_DYNAMIC_DRAW)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        L = self._loc
        gl.glUniform2f(L["uScreen"], win_w, win_h)
        gl.glUniform2f(L["uWinSize"], win_w, win_h)
        gl.glUniform1f(L["uMargin"], float(margin))
        gl.glUniform1f(L["uRadius"], CORNER_RADIUS_PX * scale_ui)
        gl.glUniform1f(L["uShadowBlur"], SHADOW_BLUR_PX * scale_ui)
        gl.glUniform2f(L["uShadowOffset"], 0.0, SHADOW_OFFSET_Y_PX * scale_ui)
        gl.glUniform1f(L["uShadowOpacity"], SHADOW_OPACITY)
        gl.glUniform4f(L["uUVRect"], float(u0), float(u1), float(v0), float(v1))

    def _draw(self, win_w, win_h):
        scale_ui = getattr(self.pet, "ui_scale", 1.0)
        margin = CARD_MARGIN_PX * scale_ui
        if win_w - 2.0 * margin <= 0 or win_h - 2.0 * margin <= 0:
            return
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)  # 预乘
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(self._program)
        key = (win_w, win_h, scale_ui, self._tex_w, self._tex_h)
        if key != self._layout_key:
            self._upload_layout(win_w, win_h, scale_ui)
            self._layout_key = key
        gl.glUniform1f(self._loc["uAlpha"], float(self._alpha))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
        gl.glBindVertexArray(self._vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
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
        self._layout_key = None
