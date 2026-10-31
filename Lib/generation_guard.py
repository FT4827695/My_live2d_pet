# Lib/generation_guard.py
# -*- coding: utf-8 -*-
"""对话轮次竞态防护"""

import threading


class GenerationGuard:
    def __init__(self):
        self._id = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def is_current(self, gen_id) -> bool:
        with self._lock:
            return gen_id == self._id
