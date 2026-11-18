# -- coding: utf-8 --
"""
素描式模型切换过渡
"""
import numpy as np
from OpenGL import GL as gl

# ==================== 时间线常量（秒） ====================
OUT_DRAIN      = 0.26   # 退场：颜色溶解消退
OUT_ERASE      = 0.20   # 退场：线稿擦除
IN_LINE        = 0.45   # 入场：线稿揭示时长
IN_FILL_DELAY  = 0.28   # 入场：填色开始延迟
IN_FILL        = 0.42   # 入场：填色时长
IN_TOTAL       = 0.88   # 入场：总时长
LINE_FADE_T0   = 0.62   # 入场：线稿开始淡出（融入成品）
LINE_FADE_T1   = 0.88   # 入场：线稿完全消失
IN_SCALE_START = 0.92          # 入场起始缩放（<1，越小"弹出"感越强）
IN_SCALE_ANCHOR = (0.5, 0.62)  # 缩放锚点（uv 坐标，约角色躯干位置）

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
    // FBO 纹理 v=0 在底部，翻转以对齐屏幕方向
    vUV = vec2(aUV.x, 1.0 - aUV.y);
}
"""

_FS = """
#version 330 core
in vec2 vUV;
uniform sampler2D uTex;
uniform vec2  uTexel;      // 1/w, 1/h
uniform float uFill;       // 填色可见度 0..1
uniform float uLine;       // 线稿揭示度 0..1
uniform float uLineAlpha;  // 线稿整体强度（收尾淡出用）
uniform float uTime;
uniform float uScale;      // 入场缩放（1 = 不缩放）
uniform vec2  uAnchor;     // 缩放锚点（uv）
out vec4 FragColor;

float hash(vec2 p){
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
float vnoise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i),               hash(i + vec2(1.0, 0.0)), u.x),
               mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
// 预乘 alpha 还原后取亮度
float lum(vec4 c){
    float a = max(c.a, 1e-4);
    vec3 c0 = clamp(c.rgb / a, 0.0, 1.0);
    return dot(c0, vec3(0.299, 0.587, 0.114));
}

void main(){
    // ---- 入场缩放弹跳：围绕锚点对采样 uv 做 zoom ----
    vec2 uv = uAnchor + (vUV - uAnchor) / max(uScale, 0.05);
    vec4 base = texture(uTex, uv);

    // ---- 手绘"boil"抖动：仅作用于提线采样 ----
    float bt = floor(uTime * 8.0);
    vec2 j = vec2(vnoise(uv * 180.0 + bt * 7.13),
                  vnoise(uv * 180.0 + 41.7 + bt * 7.13)) - 0.5;
    vec2 buv = uv + j * uTexel * 1.8;
    
    // ---- 3x3 邻域采样 ----
    vec4 c00 = texture(uTex, buv + vec2(-uTexel.x, -uTexel.y));
    vec4 c10 = texture(uTex, buv + vec2( 0.0,     -uTexel.y));
    vec4 c20 = texture(uTex, buv + vec2( uTexel.x, -uTexel.y));
    vec4 c01 = texture(uTex, buv + vec2(-uTexel.x,  0.0));
    vec4 c11 = texture(uTex, buv);
    vec4 c21 = texture(uTex, buv + vec2( uTexel.x,  0.0));
    vec4 c02 = texture(uTex, buv + vec2(-uTexel.x,  uTexel.y));
    vec4 c12 = texture(uTex, buv + vec2( 0.0,      uTexel.y));
    vec4 c22 = texture(uTex, buv + vec2( uTexel.x,  uTexel.y));

    // ---- 剪影线：alpha 通道 Sobel ----
    float gx = (c20.a + 2.0*c21.a + c22.a) - (c00.a + 2.0*c01.a + c02.a);
    float gy = (c02.a + 2.0*c12.a + c22.a) - (c00.a + 2.0*c10.a + c20.a);
    float edgeA = sqrt(gx*gx + gy*gy);

    // ---- 内部线：预乘亮度 Sobel，按中心 alpha 归一 ----
    float m00 = lum(c00)*c00.a, m10 = lum(c10)*c10.a, m20 = lum(c20)*c20.a;
    float m01 = lum(c01)*c01.a,                       m21 = lum(c21)*c21.a;
    float m02 = lum(c02)*c02.a, m12 = lum(c12)*c12.a, m22 = lum(c22)*c22.a;
    float lx = (m20 + 2.0*m21 + m22) - (m00 + 2.0*m01 + m02);
    float ly = (m02 + 2.0*m12 + m22) - (m00 + 2.0*m10 + m20);
    float edgeL = sqrt(lx*lx + ly*ly) / max(c11.a, 0.15);

    float line = clamp(max(edgeA * 1.5, edgeL * 1.2) - 0.10, 0.0, 1.0);
    line = smoothstep(0.05, 0.55, line);

    // ---- 线稿揭示掩膜：自上而下扫描 + 噪声毛边 ----
    float n1 = vnoise(vUV * vec2(6.0, 5.0));
    float field = mix(vUV.y, n1, 0.35);
    float lineMask = smoothstep(field - 0.06, field + 0.06, uLine * 1.15 - 0.075);

    // ---- 填色掩膜：噪声溶解 ----
    float n2 = vnoise(vUV * vec2(9.0, 8.0) + 17.3);
    float fillMask = smoothstep(n2 - 0.12, n2 + 0.12, uFill * 1.24 - 0.12);

    // ---- 合成：先铺色，再压线 ----
    vec3 ink = vec3(0.16, 0.17, 0.20);
    float La = line * lineMask * uLineAlpha;
    float Ba = base.a * fillMask;
    vec3  Bc = base.a > 1e-4 ? base.rgb / base.a : vec3(0.0);
    float aOut = La + Ba * (1.0 - La);
    vec3  cOut = (ink * La + Bc * Ba * (1.0 - La)) / max(aOut, 1e-4);
    FragColor = vec4(cOut * aOut, aOut);   // 输出预乘
}
"""

def ease_out_back(t, c1=1.70158):
    t = max(0.0, min(1.0, t))
    c3 = c1 + 1.0
    t -= 1.0
    return 1.0 + c3 * t * t * t + c1 * t * t

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

class SketchTransition:
    def __init__(self):
        self._w = 0
        self._h = 0
        self._fbo = 0
        self._tex = 0
        self._prog = 0
        self._vao = 0
        self._vbo = 0
        self._loc = {}
        self._ok = False

    # ---------- 资源 ----------
    def ensure(self, w, h):
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return False
        if self._ok and w == self._w and h == self._h:
            return True
        try:
            if not self._prog:
                self._prog = _compile(_VS, _FS)
                self._loc = {n: gl.glGetUniformLocation(self._prog, n)
                             for n in ("uScreen", "uTex", "uTexel",
                                       "uFill", "uLine", "uLineAlpha", "uTime",
                                       "uScale", "uAnchor")}
                self._vao = int(gl.glGenVertexArrays(1))
                self._vbo = int(gl.glGenBuffers(1))
            if self._tex:
                gl.glDeleteTextures([self._tex])
            if self._fbo:
                gl.glDeleteFramebuffers([self._fbo])
            self._tex = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            self._fbo = int(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self._tex, 0)
            status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            if status != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError(f"FBO 不完整: {status}")
            self._w, self._h = w, h
            self._ok = True
            return True
        except Exception as e:
            print(f"[Sketch] 初始化失败: {e}")
            self._ok = False
            return False

    # ---------- 捕获模型到 FBO ----------
    def begin_capture(self):
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._fbo)
        gl.glViewport(0, 0, self._w, self._h)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def end_capture(self):
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glViewport(0, 0, self._w, self._h)

    # ---------- 合成上屏 ----------
    def draw_composite(self, fillp, linep, line_alpha, now, scale=1.0):
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)  # 预乘
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(self._prog)
        gl.glUniform2f(self._loc["uScreen"], float(self._w), float(self._h))
        gl.glUniform1i(self._loc["uTex"], 0)
        gl.glUniform2f(self._loc["uTexel"], 1.0 / self._w, 1.0 / self._h)
        gl.glUniform1f(self._loc["uFill"], float(fillp))
        gl.glUniform1f(self._loc["uLine"], float(linep))
        gl.glUniform1f(self._loc["uLineAlpha"], float(line_alpha))
        gl.glUniform1f(self._loc["uTime"], float(now))
        gl.glUniform1f(self._loc["uScale"], float(scale))
        ax, ay = IN_SCALE_ANCHOR
        gl.glUniform2f(self._loc["uAnchor"], float(ax), float(ay))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
        w, h = float(self._w), float(self._h)
        quad = np.array([
            0, 0, 0, 0,   w, 0, 1, 0,   w, h, 1, 1,
            0, 0, 0, 0,   w, h, 1, 1,   0, h, 0, 1,
        ], dtype=np.float32)
        gl.glBindVertexArray(self._vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_DYNAMIC_DRAW)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 16, None)
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, 16,
                                 gl.ctypes.c_void_p(8))
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glUseProgram(0)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    def destroy(self):
        try:
            if self._tex:
                gl.glDeleteTextures([self._tex])
            if self._fbo:
                gl.glDeleteFramebuffers([self._fbo])
            if self._prog:
                gl.glDeleteProgram(self._prog)
            if self._vao:
                gl.glDeleteVertexArrays([self._vao])
            if self._vbo:
                gl.glDeleteBuffers([self._vbo])
        except Exception:
            pass
        self._tex = self._fbo = self._prog = self._vao = self._vbo = 0
        self._ok = False
