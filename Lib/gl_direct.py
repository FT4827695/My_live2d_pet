"""
gl_direct.py — ctypes 直调 OpenGL，替代 PyOpenGL 包装层
"""

import ctypes
import ctypes.util
import sys
import array as _array
import numpy as np

# ============================================================
# GL 类型
# ============================================================
GLenum      = ctypes.c_uint
GLboolean   = ctypes.c_ubyte
GLbitfield  = ctypes.c_uint
GLint       = ctypes.c_int
GLuint      = ctypes.c_uint
GLsizei     = ctypes.c_int
GLfloat     = ctypes.c_float
GLintptr    = ctypes.c_int64
GLsizeiptr  = ctypes.c_int64

# ============================================================
# 状态
# ============================================================
_initialized = False
_opengl32 = None
_wglGetProcAddress = None
_f = {}          # 函数指针字典
_failed = set()  # 加载失败的函数名


# ============================================================
# 函数加载器（兼容 NVIDIA / AMD / Intel）
# ============================================================
def _load(name, restype, argtypes):
    # 1) wglGetProcAddress
    addr = _wglGetProcAddress(name.encode('ascii'))
    if addr and addr not in (1, 2, 3, 0xFFFFFFFF):
        ftype = ctypes.WINFUNCTYPE(restype, *argtypes)
        return ftype(addr)

    # 2) opengl32.dll 导出
    try:
        func = getattr(_opengl32, name)
        func.restype = restype
        func.argtypes = argtypes
        return func
    except AttributeError:
        pass

    return None


# ============================================================
# 初始化
# ============================================================
def init():
    global _initialized, _opengl32, _wglGetProcAddress

    if _initialized:
        return True

    try:
        _opengl32 = ctypes.windll.opengl32
    except OSError:
        print("[gl_direct] 无法加载 opengl32.dll")
        return False

    _wglGetProcAddress = _opengl32.wglGetProcAddress
    _wglGetProcAddress.restype = ctypes.c_void_p
    _wglGetProcAddress.argtypes = [ctypes.c_char_p]

    # ---- 函数签名表 ----
    # (名称, 返回类型, 参数类型列表)
    _SIGS = [
        # --- 高频：drawTexture 路径 ---
        ("glUniform4f",            None,       [GLuint, GLfloat, GLfloat, GLfloat, GLfloat]),
        ("glUniform1i",            None,       [GLint, GLint]),
        ("glUniformMatrix4fv",     None,       [GLint, GLsizei, GLboolean, ctypes.c_void_p]),
        ("glBindVertexArray",      None,       [GLuint]),
        ("glBindTexture",          None,       [GLenum, GLuint]),
        ("glBindBuffer",           None,       [GLenum, GLuint]),
        ("glUseProgram",           None,       [GLuint]),
        ("glEnable",               None,       [GLenum]),
        ("glDisable",              None,       [GLenum]),
        ("glActiveTexture",        None,       [GLenum]),
        ("glBufferSubData",        None,       [GLenum, GLintptr, GLsizeiptr, ctypes.c_void_p]),
        ("glBufferData",           None,       [GLenum, GLsizeiptr, ctypes.c_void_p, GLenum]),
        ("glDrawElements",         None,       [GLenum, GLsizei, GLenum, ctypes.c_void_p]),
        ("glBlendFuncSeparate",    None,       [GLenum, GLenum, GLenum, GLenum]),
        ("glBlendEquationSeparate",None,       [GLenum, GLenum]),
        ("glColorMask",            None,       [GLboolean, GLboolean, GLboolean, GLboolean]),
        ("glFrontFace",            None,       [GLenum]),
        # --- 中频：setupDraw / clearBuffer ---
        ("glClear",                None,       [GLbitfield]),
        ("glClearColor",           None,       [GLfloat, GLfloat, GLfloat, GLfloat]),
        ("glGetIntegerv",          None,       [GLenum, ctypes.POINTER(GLint)]),
        ("glViewport",             None,       [GLint, GLint, GLsizei, GLsizei]),
        # --- 低频：初始化 / VAO 创建 ---
        ("glGenBuffers",           None,       [GLsizei, ctypes.POINTER(GLuint)]),
        ("glGenVertexArrays",      None,       [GLsizei, ctypes.POINTER(GLuint)]),
        ("glVertexAttribPointer",  None,       [GLuint, GLint, GLenum, GLboolean, GLsizei, ctypes.c_void_p]),
        ("glEnableVertexAttribArray", None,    [GLuint]),
        ("glGenTextures",          None,       [GLsizei, ctypes.POINTER(GLuint)]),
        ("glTexParameteri",        None,       [GLenum, GLenum, GLint]),
        ("glTexImage2D",           None,       [GLenum, GLint, GLint, GLsizei, GLsizei,
                                                GLint, GLenum, GLenum, ctypes.c_void_p]),
        ("glGenerateMipmap",       None,       [GLenum]),
        ("glDeleteBuffers",        None,       [GLsizei, ctypes.POINTER(GLuint)]),
        ("glDeleteVertexArrays",   None,       [GLsizei, ctypes.POINTER(GLuint)]),
        ("glDeleteTextures",       None,       [GLsizei, ctypes.POINTER(GLuint)]),
        # --- 补齐渲染器早绑定的原始名 ---
        ("glDisableVertexAttribArray", None,    [GLuint]),
        ("glDrawArrays",             None,     [GLenum, GLint, GLsizei]),
        ("glBlendFunc",              None,     [GLenum, GLenum]),
        ("glPixelStorei",            None,     [GLenum, GLint]),
        ("glGetUniformLocation",     GLint,    [GLuint, ctypes.c_char_p]),
        ("glGenFramebuffers",        None,     [GLsizei, ctypes.POINTER(GLuint)]),
        ("glBindFramebuffer",        None,     [GLenum, GLuint]),
        ("glDeleteFramebuffers",     None,     [GLsizei, ctypes.POINTER(GLuint)]),
        ("glFramebufferTexture2D",   None,     [GLenum, GLenum, GLenum, GLuint, GLint]),
        ("glCheckFramebufferStatus", GLenum,    [GLenum]),
    ]

    ok_count = 0
    for name, restype, argtypes in _SIGS:
        func = _load(name, restype, argtypes)
        if func is not None:
            _f[name] = func
            ok_count += 1
        else:
            _failed.add(name)

    if _failed:
        print(f"[gl_direct] 警告：{len(_failed)} 个函数加载失败: {_failed}")

    critical = {"glUniform4f", "glDrawElements", "glBindVertexArray",
                "glBufferSubData", "glUniformMatrix4fv", "glBindTexture"}
    if not critical.issubset(set(_f.keys())):
        missing = critical - set(_f.keys())
        print(f"[gl_direct] 关键函数缺失: {missing}，回退 PyOpenGL")
        _f.clear()
        return False

    _initialized = True
    print(f"[gl_direct] 已加载 {ok_count} 个 GL 函数（ctypes 直调）")
    return True


def is_ready():
    return _initialized


# ============================================================
# 数据指针辅助
# ============================================================
def _data_ptr(data):
    cls = data.__class__
    if cls is np.ndarray:
        return data.ctypes.data, data.nbytes
    if cls is _array.array:
        return data.buffer_info()[0], len(data) * data.itemsize
    if isinstance(data, (bytes, bytearray, memoryview)):
        buf = np.frombuffer(data, dtype=np.uint8)   # 视图持有引用，调用期内不会被 GC
        return buf.ctypes.data, buf.nbytes
    if isinstance(data, np.ndarray):
        return data.ctypes.data, data.nbytes
    if isinstance(data, _array.array):
        return data.buffer_info()[0], len(data) * data.itemsize
    buf = np.asarray(data, dtype=np.float32)
    return buf.ctypes.data, buf.nbytes

def _uint16_ptr(data):
    if isinstance(data, np.ndarray) and data.dtype == np.uint16:
        return data.ctypes.data, data.nbytes
    if isinstance(data, _array.array) and data.typecode == 'H':
        return data.buffer_info()[0], len(data) * 2
    buf = np.asarray(data, dtype=np.uint16)
    return buf.ctypes.data, buf.nbytes


def bufferSubData(target, offset, data):
    cls = data.__class__
    if cls is _array.array:
        _f["glBufferSubData"](target, offset, len(data) * data.itemsize,
                              data.buffer_info()[0])
    elif cls is np.ndarray:
        _f["glBufferSubData"](target, offset, data.nbytes, data.ctypes.data)
    else:
        ptr, size = _data_ptr(data)
        _f["glBufferSubData"](target, offset, size, ptr)

# ============================================================
# 包装函数（与 Live2DGLWrapper 签名兼容）
# ============================================================

# ---- 高频 ----

def uniform4f(loc, x, y, z, w):
    _f["glUniform4f"](loc, x, y, z, w)

def uniform1i(loc, x):
    _f["glUniform1i"](loc, int(x))

def uniformMatrix4fv(loc, transpose, value):
    if isinstance(value, np.ndarray):
        if value.dtype != np.float32 or not value.flags['C_CONTIGUOUS']:
            value = np.ascontiguousarray(value, dtype=np.float32)
        ptr = value.ctypes.data                    # int，直接传
    elif isinstance(value, _array.array) and value.typecode == 'f':
        ptr = value.buffer_info()[0]               # int，直接传
    elif isinstance(value, (bytes, bytearray, memoryview)):
        ptr = np.frombuffer(value, dtype=np.float32).ctypes.data
    else:
        ptr = np.asarray(value, dtype=np.float32).ctypes.data
    _f["glUniformMatrix4fv"](loc, 1, int(transpose), ptr)
    
def bindVertexArray(vao):
    _f["glBindVertexArray"](vao)

def bindTexture(target, tid):
    _f["glBindTexture"](target, tid)

def bindBuffer(target, buf):
    _f["glBindBuffer"](target, buf)

def useProgram(prog):
    _f["glUseProgram"](prog)

def enable(cap):
    _f["glEnable"](cap)

def disable(cap):
    _f["glDisable"](cap)

def activeTexture(unit):
    _f["glActiveTexture"](unit)

def bufferSubData(target, offset, data):
    ptr, size = _data_ptr(data)
    _f["glBufferSubData"](target, offset, size, ptr)

def bufferData(target, data, usage):
    if target == 0x8892:      # GL_ARRAY_BUFFER
        ptr, size = _data_ptr(data)
    elif target == 0x8893:    # GL_ELEMENT_ARRAY_BUFFER
        ptr, size = _uint16_ptr(data)
    else:
        ptr, size = _data_ptr(data)
    _f["glBufferData"](target, size, ptr, usage)

def drawElements(mode, count, dtype, indices):
    _f["glDrawElements"](mode, count, dtype,
                         ctypes.c_void_p(0) if indices is None else indices)

def blendFuncSeparate(srcRGB, dstRGB, srcAlpha, dstAlpha):
    _f["glBlendFuncSeparate"](srcRGB, dstRGB, srcAlpha, dstAlpha)

def blendEquationSeparate(modeRGB, modeAlpha):
    _f["glBlendEquationSeparate"](modeRGB, modeAlpha)

def colorMask(r, g, b, a):
    _f["glColorMask"](int(r), int(g), int(b), int(a))

def frontFace(mode):
    _f["glFrontFace"](mode)

# ---- 中频 ----

def clear(mask):
    _f["glClear"](mask)

def clearColor(r, g, b, a):
    _f["glClearColor"](r, g, b, a)

def getIntegerv(pname):
    result = GLint(0)
    _f["glGetIntegerv"](pname, ctypes.byref(result))
    return result.value

def viewport(x, y, w, h):
    _f["glViewport"](x, y, w, h)

# ---- 低频 ----

def genBuffers(n=1):
    bufs = (GLuint * n)()
    _f["glGenBuffers"](n, bufs)
    return bufs[0] if n == 1 else list(bufs)

def genVertexArrays(n=1):
    arrs = (GLuint * n)()
    _f["glGenVertexArrays"](n, arrs)
    return arrs[0] if n == 1 else list(arrs)

def vertexAttribPointer(index, size, vtype, normalized, stride, pointer):
    _f["glVertexAttribPointer"](index, size, vtype, int(normalized), stride,
                                ctypes.c_void_p(0) if pointer is None else pointer)

def enableVertexAttribArray(index):
    _f["glEnableVertexAttribArray"](index)

def genTextures(n=1):
    texs = (GLuint * n)()
    _f["glGenTextures"](n, texs)
    return texs[0] if n == 1 else list(texs)

def texParameteri(target, pname, param):
    _f["glTexParameteri"](target, pname, param)

def texImage2D(target, level, internalfmt, w, h, border, fmt, dtype, data):
    ptr = 0 if data is None else _data_ptr(data)[0]
    _f["glTexImage2D"](target, level, internalfmt, w, h, border, fmt, dtype, ptr)
    
def generateMipmap(target):
    _f["glGenerateMipmap"](target)

def deleteBuffers(n, bufs):
    if isinstance(bufs, int):
        arr = (GLuint * 1)(bufs)
    else:
        arr = (GLuint * n)(*bufs)
    _f["glDeleteBuffers"](n, arr)

def deleteVertexArrays(n, arrs):
    if isinstance(arrs, int):
        a = (GLuint * 1)(arrs)
    else:
        a = (GLuint * n)(*arrs)
    _f["glDeleteVertexArrays"](n, a)

def deleteTextures(n, texs):
    if isinstance(texs, int):
        a = (GLuint * 1)(texs)
    else:
        a = (GLuint * n)(*texs)
    _f["glDeleteTextures"](n, a)

# ---- 基础状态 ----
def blendFunc(src, dst):
    _f["glBlendFunc"](src, dst)

def drawArrays(mode, first, count):
    _f["glDrawArrays"](mode, first, count)

def disableVertexAttribArray(index):
    _f["glDisableVertexAttribArray"](index)

def pixelStorei(pname, param):
    _f["glPixelStorei"](pname, param)

def getUniformLocation(program, name):
    if isinstance(name, str):
        name = name.encode("utf-8")
    return _f["glGetUniformLocation"](program, name)

def bindFramebuffer(target, fb):
    _f["glBindFramebuffer"](target, fb)

def framebufferTexture2D(target, attachment, textarget, texture, level):
    _f["glFramebufferTexture2D"](target, attachment, textarget, texture, level)

def checkFramebufferStatus(target):
    return _f["glCheckFramebufferStatus"](target)

# ---- 与 OpenGL.GL 调用习惯兼容的 gen/delete/原始签名 ----
def _make_gen(fn_name):
    def fn(n=1, arrays=None):
        if arrays is None:
            out = (GLuint * max(n, 1))()
            _f[fn_name](max(n, 1), out)
            return out[0] if n == 1 else list(out)
        cls = arrays.__class__
        if cls is np.ndarray:
            ptr = arrays.ctypes.data
        elif cls is _array.array:
            ptr = arrays.buffer_info()[0]
        elif isinstance(arrays, (list, tuple)):
            tmp = (GLuint * n)(*arrays)
            _f[fn_name](n, tmp)
            return arrays
        else:
            ptr = arrays
        _f[fn_name](n, ptr)
        return arrays
    fn.__name__ = fn_name
    return fn

def _make_delete(fn_name):
    def fn(n, arrays):
        if arrays is None:
            return
        cls = arrays.__class__
        if isinstance(arrays, int):
            tmp = (GLuint * 1)(arrays)
            _f[fn_name](1, tmp)
            return
        if cls is np.ndarray:
            ptr = arrays.ctypes.data
        elif cls is _array.array:
            ptr = arrays.buffer_info()[0]
        elif isinstance(arrays, (list, tuple)):
            tmp = (GLuint * n)(*arrays)
            _f[fn_name](n, tmp)
            return
        else:
            ptr = arrays
        _f[fn_name](n, ptr)
    fn.__name__ = fn_name
    return fn

glGenVertexArrays_compat   = _make_gen("glGenVertexArrays")
glDeleteVertexArrays_compat = _make_delete("glDeleteVertexArrays")
glGenFramebuffers_compat   = _make_gen("glGenFramebuffers")
glDeleteFramebuffers_compat = _make_delete("glDeleteFramebuffers")
glGenBuffers_compat        = _make_gen("glGenBuffers")
glDeleteBuffers_compat     = _make_delete("glDeleteBuffers")
glGenTextures_compat       = _make_gen("glGenTextures")
glDeleteTextures_compat    = _make_delete("glDeleteTextures")

def glBufferData_raw(target, size, data, usage):
    if data is None:
        ptr = 0
    else:
        cls = data.__class__
        if cls is np.ndarray:
            ptr = data.ctypes.data
        elif cls is _array.array:
            ptr = data.buffer_info()[0]
        else:
            ptr = np.asarray(data).ctypes.data
    _f["glBufferData"](target, size, ptr, usage)

def glBufferSubData_raw(target, offset, size, data):
    if data is None:
        ptr = 0
    else:
        cls = data.__class__
        if cls is np.ndarray:
            ptr = data.ctypes.data
        elif cls is _array.array:
            ptr = data.buffer_info()[0]
        else:
            ptr = np.asarray(data).ctypes.data
    _f["glBufferSubData"](target, offset, size, ptr)


# ============================================================
# patch_wrapper：替换 Live2DGLWrapper 的方法
# ============================================================
def patch_wrapper():
    if not _initialized:
        print("[gl_direct] 未初始化，跳过 patch")
        return False

    from live2d.v2.core.live2d_gl_wrapper import Live2DGLWrapper as W

    # 高频（drawTexture 路径，每帧 ~1900 次）
    W.uniform4f            = staticmethod(uniform4f)
    W.uniform1i            = staticmethod(uniform1i)
    W.uniformMatrix4fv     = staticmethod(uniformMatrix4fv)
    W.bindTexture          = staticmethod(bindTexture)
    W.bindBuffer           = staticmethod(bindBuffer)
    W.useProgram           = staticmethod(useProgram)
    W.enable               = staticmethod(enable)
    W.disable              = staticmethod(disable)
    W.activeTexture        = staticmethod(activeTexture)
    W.bufferSubData        = staticmethod(bufferSubData)
    W.bufferData           = staticmethod(bufferData)
    W.drawElements         = staticmethod(drawElements)
    W.blendFuncSeparate    = staticmethod(blendFuncSeparate)
    W.blendEquationSeparate= staticmethod(blendEquationSeparate)
    W.colorMask            = staticmethod(colorMask)
    W.frontFace            = staticmethod(frontFace)

    # 中频
    W.clear                = staticmethod(clear)
    W.clearColor           = staticmethod(clearColor)
    W.getParameter         = staticmethod(getIntegerv)
    W.viewport             = staticmethod(viewport)

    # 低频
    W.createBuffer         = staticmethod(lambda: genBuffers(1))
    W.createTexture        = staticmethod(lambda: genTextures(1))
    W.vertexAttribPointer  = staticmethod(vertexAttribPointer)
    W.enableVertexAttribArray = staticmethod(enableVertexAttribArray)
    W.texParameteri        = staticmethod(texParameteri)
    W.texImage2D           = staticmethod(texImage2D)
    W.deleteBuffer         = staticmethod(lambda b: deleteBuffers(1, b))
    W.deleteTexture        = staticmethod(lambda t: deleteTextures(1, t))

    # ---- VAO / 其它遗漏项 ----
    W.bindVertexArray          = staticmethod(bindVertexArray)
    W.glBindVertexArray        = staticmethod(bindVertexArray)
    W.createVertexArray        = staticmethod(lambda: genVertexArrays(1))
    W.genVertexArrays          = staticmethod(glGenVertexArrays_compat)
    W.glGenVertexArrays        = staticmethod(glGenVertexArrays_compat)
    W.deleteVertexArray        = staticmethod(lambda v: deleteVertexArrays(1, v))
    W.glDeleteVertexArrays     = staticmethod(glDeleteVertexArrays_compat)
    W.disableVertexAttribArray = staticmethod(disableVertexAttribArray)
    W.drawArrays               = staticmethod(drawArrays)
    W.blendFunc                = staticmethod(blendFunc)
    W.pixelStorei              = staticmethod(pixelStorei)
    W.getUniformLocation       = staticmethod(getUniformLocation)
    W.bindFramebuffer          = staticmethod(bindFramebuffer)
    W.framebufferTexture2D     = staticmethod(framebufferTexture2D)
    W.checkFramebufferStatus   = staticmethod(checkFramebufferStatus)
    
    print(f"[gl_direct] Live2DGLWrapper 已 patch（{len(_f)} 个函数）")
    return True

def patch_modules():
    if not _initialized:
        print("[gl_direct] 未初始化，跳过 patch_modules")
        return False

    mapping = {
        # ---- 实测每帧 bypass 的调用（glstat 清单）----
        "glBindVertexArray": bindVertexArray,             
        "glGenVertexArrays": glGenVertexArrays_compat,
        "glDeleteVertexArrays": glDeleteVertexArrays_compat,
        "glBindBuffer": bindBuffer,
        "glEnableVertexAttribArray": enableVertexAttribArray,
        "glDisableVertexAttribArray": disableVertexAttribArray,
        "glVertexAttribPointer": vertexAttribPointer,
        "glDrawArrays": drawArrays,
        # ---- 签名完全一致的状态调用，覆盖主程序/气泡的每帧调用 ----
        "glUseProgram": useProgram,
        "glEnable": enable,
        "glDisable": disable,
        "glActiveTexture": activeTexture,
        "glBindTexture": bindTexture,
        "glBlendFunc": blendFunc,
        "glBlendFuncSeparate": blendFuncSeparate,
        "glBlendEquationSeparate": blendEquationSeparate,
        "glUniform4f": uniform4f,
        "glUniform1i": uniform1i,
    }

    count = 0
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not (mod_name.startswith("live2d") or mod_name == "OpenGL.GL"):
            continue
        for attr, fn in mapping.items():
            cur = getattr(mod, attr, None)
            if cur is None or cur is fn:
                continue
            if getattr(cur, "__module__", None) == __name__:
                continue
            try:
                setattr(mod, attr, fn)
                count += 1
            except Exception:
                pass
    print(f"[gl_direct] 早绑定替换 {count} 处（含 OpenGL.GL 模块自身）")
    return True
