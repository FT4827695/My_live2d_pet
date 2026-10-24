# chat/ime_helper.py
import ctypes
import ctypes.wintypes as wintypes
import glfw
import win32gui

# ---------- IME 结构体 ----------
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class COMPOSITIONFORM(ctypes.Structure):
    _fields_ = [
        ("dwStyle", wintypes.DWORD),
        ("ptCurrentPos", POINT),
        ("rcArea", RECT)
    ]

CFS_POINT = 0x0002

# ---------- IME API 封装 ----------
imm32 = ctypes.WinDLL("imm32.dll", use_last_error=True)
ImmGetContext = imm32.ImmGetContext
ImmGetContext.restype = wintypes.HANDLE
ImmGetContext.argtypes = [wintypes.HWND]
ImmReleaseContext = imm32.ImmReleaseContext
ImmReleaseContext.restype = wintypes.BOOL
ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
ImmSetCompositionWindow = imm32.ImmSetCompositionWindow
ImmSetCompositionWindow.restype = wintypes.BOOL
ImmSetCompositionWindow.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMPOSITIONFORM)]


def set_ime_position(hwnd, cursor_x, cursor_y):
    if not hwnd:
        return
    himc = ImmGetContext(hwnd)
    if not himc:
        return
    cf = COMPOSITIONFORM()
    cf.dwStyle = CFS_POINT
    cf.ptCurrentPos.x = cursor_x
    cf.ptCurrentPos.y = cursor_y
    cf.rcArea.left = cursor_x
    cf.rcArea.top = cursor_y
    cf.rcArea.right = cursor_x + 2
    cf.rcArea.bottom = cursor_y + 20
    ImmSetCompositionWindow(himc, ctypes.byref(cf))
    ImmReleaseContext(hwnd, himc)
