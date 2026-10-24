# -*- coding: utf-8 -*-
"""
message_layout.py
聊天消息折行布局缓存管理器。
"""

import threading
from dataclasses import dataclass
from typing import Any, Tuple

from services import TextUtils


@dataclass(frozen=True)
class LayoutEntry:
    sender: str
    text_hash: int
    text_len: int
    lines: Tuple[str, ...]
    line_count: int


@dataclass(frozen=True)
class LayoutSnapshot:
    entries: Tuple[LayoutEntry, ...]
    prefix: Tuple[int, ...]
    total_lines: int
    available_width: int
    scale_key: Any

    def is_empty(self):
        return self.total_lines <= 0


class MessageLayoutManager:

    def __init__(self, get_text_width):
        self._get_text_width = get_text_width
        self._available_width = 0
        self._scale_key = None

        self._entries = []
        self._prefix = [0]

        self._snapshot = LayoutSnapshot(
            entries=(),
            prefix=(0,),
            total_lines=0,
            available_width=0,
            scale_key=None,
        )

        self._lock = threading.RLock()

    @property
    def snapshot(self) -> LayoutSnapshot:
        return self._snapshot

    def set_geometry(self, available_width: int, scale_key=None) -> bool:
        with self._lock:
            available_width = int(available_width)
            if (
                available_width == self._available_width
                and scale_key == self._scale_key
            ):
                return False

            self._available_width = available_width
            self._scale_key = scale_key
            self._entries = []
            self._prefix = [0]
            self._update_snapshot_locked()
            return True

    def sync(self, messages):
        with self._lock:
            if self._available_width <= 0:
                return self._snapshot

            n = len(messages)

            if n == 0:
                if self._entries:
                    self._entries = []
                    self._prefix = [0]
                    self._update_snapshot_locked()
                return self._snapshot

            force_prefix = False

            # 消息数量变化：可能是清空、追加、截断
            if n != len(self._entries):
                if n < len(self._entries):
                    self._entries = self._entries[:n]
                else:
                    self._entries.extend([None] * (n - len(self._entries)))

                self._prefix = [0] * (n + 1)
                force_prefix = True

            changed_from = None

            for i, (sender, text) in enumerate(messages):
                ent = self._entries[i]

                text_hash = hash(text)
                text_len = len(text)

                if (
                    ent is None
                    or ent.sender != sender
                    or ent.text_hash != text_hash
                    or ent.text_len != text_len
                ):
                    self._entries[i] = self._build_entry(sender, text)
                    if changed_from is None:
                        changed_from = i

            if force_prefix or changed_from is not None:
                start = 0 if force_prefix else changed_from

                if start == 0:
                    self._prefix[0] = 0

                for i in range(start, n):
                    self._prefix[i + 1] = self._prefix[i] + self._entries[i].line_count

                self._update_snapshot_locked()

            return self._snapshot

    def clear(self):
        with self._lock:
            self._entries = []
            self._prefix = [0]
            self._update_snapshot_locked()

    def _build_entry(self, sender: str, text: str) -> LayoutEntry:
        lines = TextUtils.get_message_lines(
            sender,
            text,
            self._available_width,
            self._get_text_width,
        )

        if not lines:
            lines = [""]

        lines = tuple(lines)

        return LayoutEntry(
            sender=sender,
            text_hash=hash(text),
            text_len=len(text),
            lines=lines,
            line_count=len(lines),
        )

    def _update_snapshot_locked(self):
        self._snapshot = LayoutSnapshot(
            entries=tuple(self._entries),
            prefix=tuple(self._prefix),
            total_lines=self._prefix[-1] if self._prefix else 0,
            available_width=self._available_width,
            scale_key=self._scale_key,
        )
