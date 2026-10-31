# settings_store.py
# -*- coding: utf-8 -*-
"""
统一配置存储：
"""

import os
import sys
import json
import copy
import threading

# ==================== 路径 ====================

def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here).lower() == "lib":
        return os.path.dirname(here)
    return here

BASE_DIR = get_base_dir()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SETTING_PATH = os.path.join(CONFIG_DIR, "setting.json")

# ==================== 默认配置 ====================

DEFAULTS = {
    "version": 1,
    "audio": {
        "muted": False,
    },
    "background": {
        "enabled": False,
        "file": "background.png",
        "opacity": 1.0,
        "dim_on_chat": True,
        "dim_alpha": 0.45,
    },
}


def _deep_merge(base, user):
    out = copy.deepcopy(base)
    if not isinstance(user, dict):
        return out
    for k, v in user.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ==================== SettingsStore ====================

class SettingsStore:
    def __init__(self, path=SETTING_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._load()

    # ---------- 加载（启动执行一次） ----------
    def _load(self):
        data = None
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception as e:
            print(f"[Settings] 读取失败，使用默认配置: {e}")
            self._backup_broken()
            data = None

        self._data = _deep_merge(DEFAULTS, data if isinstance(data, dict) else {})

        if data is None:
            self.save()

    def _backup_broken(self):
        try:
            if os.path.exists(self._path):
                os.replace(self._path, self._path + ".broken")
        except Exception:
            pass

    # ---------- 读：纯内存 ----------
    def get(self, key, default=None):
        with self._lock:
            node = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def get_bool(self, key, default=False):
        return bool(self.get(key, default))

    def get_float(self, key, default=0.0):
        try:
            return float(self.get(key, default))
        except Exception:
            return float(default)

    # ---------- 写：内存 + 可选落盘 ----------
    def set(self, key, value, save=True):
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            if node.get(parts[-1]) == value:
                return False
            node[parts[-1]] = value
        if save:
            self.save()
        return True

    def save(self):
        with self._lock:
            snapshot = copy.deepcopy(self._data)
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            print(f"[Settings] 保存失败: {e}")


# ==================== 单例 ====================

_store = None
_store_lock = threading.Lock()


def get_settings():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SettingsStore()
    return _store
