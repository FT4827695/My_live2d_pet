# -*- coding: utf-8 -*-

"""
display_config.py
负责三档分辨率缩放配置与计算。
"""

import glfw

try:
    import win32api
except ImportError:
    win32api = None


# ==================== 三档分辨率缩放配置 ====================
# 以 4K 下当前视觉效果为基准

BASE_WINDOW_WIDTH = 440
BASE_WINDOW_HEIGHT = 660
BASE_BUBBLE_X_OFFSET = 30
BASE_BUBBLE_Y_OFFSET = 20

# 如果获取屏幕高度失败，使用 4K 高度兜底
DEFAULT_SCREEN_HEIGHT = 2160


def _get_screen_height() -> int:
    height = 0

    try:
        monitor = glfw.get_primary_monitor()
        mode = glfw.get_video_mode(monitor)

        if mode is None:
            height = 0
        elif hasattr(mode, "height"):
            height = mode.height
        elif hasattr(mode, "size") and hasattr(mode.size, "height"):
            height = mode.size.height
        else:
            height = mode[1]
    except Exception:
        try:
            if win32api is not None:
                # SM_CYSCREEN = 1
                height = win32api.GetSystemMetrics(1)
            else:
                height = 0
        except Exception:
            height = 0

    if height <= 0:
        height = DEFAULT_SCREEN_HEIGHT

    return int(height)


def compute_display_metrics():
    height = _get_screen_height()

    if height >= 2000:
        scale = 1.0
    elif height >= 1300:
        scale = 1440.0 / 2160.0
    else:
        scale = 0.5

    target_w = max(1, int(round(BASE_WINDOW_WIDTH * scale)))
    target_h = max(1, int(round(BASE_WINDOW_HEIGHT * scale)))

    bubble_x_offset = max(1, int(round(BASE_BUBBLE_X_OFFSET * scale)))
    bubble_y_offset = max(1, int(round(BASE_BUBBLE_Y_OFFSET * scale)))

    return (
        scale,
        target_w,
        target_h,
        bubble_x_offset,
        bubble_y_offset
    )

# ==================== 聊天窗口缩放配置 ====================

# 聊天窗口基础尺寸（SCALE=1.0 时的设计值）
BASE_CHAT_WIDTH = 380
BASE_CHAT_HEIGHT = 520

# 聊天窗口放大系数
CHAT_SIZE_MULTIPLIER = 1.2

# 原始设计缩放（旧 SCALE = 1.2）
CHAT_ORIGINAL_SCALE = 1.3

# 4K 基准字号表（按 28/26 ≈ 1.077 比例调整）
CHAT_FONT_SIZES_4K = {
    'message':     28,   # 消息文本
    'title':       30,   # 标题 "雫的小黑板"
    'close':       26,   # 关闭按钮 "X"
    'menu':        24,   # 右键/设置菜单项
    'status':      26,   # 录音状态文字
    'upload_icon': 32,   # 上传按钮 "+" 图标
    'badge_num':   19,   # 文件数角标数字
    'clear_icon':  22,   # 清除按钮 "×" 图标
}


def compute_chat_metrics(display_scale: float):
    scale = CHAT_ORIGINAL_SCALE * display_scale
    
    w = max(1, int(BASE_CHAT_WIDTH * scale * CHAT_SIZE_MULTIPLIER))
    h = max(1, int(BASE_CHAT_HEIGHT * scale * CHAT_SIZE_MULTIPLIER))
    
    fonts = {
        key: max(1, int(round(size * display_scale)))
        for key, size in CHAT_FONT_SIZES_4K.items()
    }
    
    return scale, w, h, fonts
