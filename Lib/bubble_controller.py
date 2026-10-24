# -*- coding: utf-8 -*-

"""
bubble_controller.py

负责气泡系统的显示控制。

"""

import threading

from bubble import BubbleRenderer
from display_config import (
    BASE_WINDOW_WIDTH,
    BASE_WINDOW_HEIGHT,
    BASE_BUBBLE_X_OFFSET,
    BASE_BUBBLE_Y_OFFSET,
)


DEFAULT_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/SimSun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
]


class BubbleController:
    def __init__(self, renderer=None, font_paths=None):
        self.renderer = renderer if renderer is not None else BubbleRenderer()
        self.font_paths = font_paths if font_paths is not None else DEFAULT_FONT_PATHS

        self.win_width = BASE_WINDOW_WIDTH
        self.win_height = BASE_WINDOW_HEIGHT
        self.x_offset = BASE_BUBBLE_X_OFFSET
        self.y_offset = BASE_BUBBLE_Y_OFFSET

        self._pending_text = None
        self._lock = threading.Lock()

    # ==================== 参数同步 ====================

    def update_metrics(
        self,
        win_width=None,
        win_height=None,
        x_offset=None,
        y_offset=None
    ):
        if win_width is not None:
            self.win_width = win_width

        if win_height is not None:
            self.win_height = win_height

        if x_offset is not None:
            self.x_offset = x_offset

        if y_offset is not None:
            self.y_offset = y_offset

    # ==================== 生命周期 ====================

    def init(self):
        self.renderer.init()

    def cleanup(self):
        self.renderer.cleanup()

    # ==================== 显示 ====================

    def show(self, text, duration=None, vertical_shift=0):
        if not text:
            return

        self.renderer.show(
            text,
            win_width=self.win_width,
            duration=duration,
            font_paths=self.font_paths,
            y_offset=self.y_offset,
            x_offset=self.x_offset,
            vertical_shift=vertical_shift
        )

    def queue(self, text):
        with self._lock:
            self._pending_text = text

    def process_pending(self):
        text = None

        with self._lock:
            if self._pending_text:
                text = self._pending_text
                self._pending_text = None

        if text:
            self.show(text)

    # ==================== 绘制 ====================

    def draw(self):
        self.renderer.draw(self.win_width, self.win_height)
