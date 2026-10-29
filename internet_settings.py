# internet_settings.py
import os
import sys
import json
import copy
import threading


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


_INTERNET_SETTING_PATH = os.path.join(get_base_dir(), "Internet_setting.json")


_DEFAULT_SETTINGS = {
    "web_search_enabled": False,
    "proxy": "",
    "request_timeout_sec": 15,
    "max_results_per_source": 3,
    "cache_ttl_sec": 600,
    "max_snippet_chars": 2048,
    "max_article_chars": 4096,
    "tavily_enabled": True,
    "tavily_api_key": "",
    "tavily_fallback_to_general": True,
    "sources": [
        {
            "id": "bing_news",
            "type": "news_search",
            "enabled": True,
            "display_name": "Bing新闻"
        },
        {
            "id": "baidu",
            "type": "baidu_search",
            "enabled": True,
            "display_name": "百度",
            "target_domain": "www.baidu.com",
            "no_body": False
        },
        {
            "id": "sogou",
            "type": "sogou_search",
            "enabled": True,
            "display_name": "搜狗",
            "target_domain": "www.sogou.com",
            "no_body": False
        }
    ]
}

class InternetSettings:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._io_lock = threading.RLock()
        self._data = {}
        self._load()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---------- 读写 ----------
    def _load(self):
        try:
            if os.path.exists(_INTERNET_SETTING_PATH):
                with open(_INTERNET_SETTING_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self._data = copy.deepcopy(_DEFAULT_SETTINGS)
                    self._data.update(saved)
                    if not isinstance(self._data.get("sources"), list):
                        self._data["sources"] = copy.deepcopy(_DEFAULT_SETTINGS["sources"])
                else:
                    self._data = copy.deepcopy(_DEFAULT_SETTINGS)
            else:
                self._data = copy.deepcopy(_DEFAULT_SETTINGS)
                self._save()
                print("[InternetSettings] 首次运行，已生成默认 Internet_setting.json")
        except Exception as e:
            print(f"[InternetSettings] 读取配置失败: {e}")
            self._data = copy.deepcopy(_DEFAULT_SETTINGS)

    def reload(self):
        with self._io_lock:
            self._load()

    def _save(self):
        with self._io_lock:
            try:
                tmp_path = _INTERNET_SETTING_PATH + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=4)
                os.replace(tmp_path, _INTERNET_SETTING_PATH)
            except Exception as e:
                print(f"[InternetSettings] 保存配置失败: {e}")
        

    # ---------- 读接口 ----------
    @property
    def is_enabled(self) -> bool:
        return bool(self._data.get("web_search_enabled", False))

    @property
    def proxy(self) -> str:
        return str(self._data.get("proxy", "")).strip()

    @property
    def proxies(self) -> dict:
        p = self.proxy
        if not p:
            return {}
        return {"http": p, "https": p}

    @property
    def request_timeout(self) -> int:
        try:
            return int(self._data.get("request_timeout_sec", 15))
        except Exception:
            return 15

    @property
    def max_results_per_source(self) -> int:
        try:
            return int(self._data.get("max_results_per_source", 3))
        except Exception:
            return 3

    @property
    def cache_ttl(self) -> int:
        try:
            return int(self._data.get("cache_ttl_sec", 600))
        except Exception:
            return 600

    @property
    def max_snippet_chars(self) -> int:
        try:
            return int(self._data.get("max_snippet_chars", 1024))
        except Exception:
            return 1024

    @property
    def max_article_chars(self) -> int:
        try:
            return int(self._data.get("max_article_chars", 2048))
        except Exception:
            return 2048

    @property
    def tavily_enabled(self) -> bool:
        return bool(self._data.get("tavily_enabled", True))

    @property
    def tavily_api_key(self) -> str:
        return str(self._data.get("tavily_api_key", "")).strip()

    @property
    def tavily_fallback_to_general(self) -> bool:
        return bool(self._data.get("tavily_fallback_to_general", True))

    def enabled_sources(self) -> list:
        sources = self._data.get("sources", [])
        return [s for s in sources if isinstance(s, dict) and s.get("enabled", False)]

    def get_source(self, source_id: str):
        for s in self._data.get("sources", []):
            if isinstance(s, dict) and s.get("id") == source_id:
                return s
        return None

    _TYPE_LABELS = {
        "site_search": "通用",
        "news_search": "资讯",
        "baidu_search": "通用",
        "sogou_search": "通用",
    }

    def enabled_source_names(self) -> str:
        names = []
        if self.tavily_enabled:
            names.append("Tavily[AI]")
        for s in self.enabled_sources():
            label = self._TYPE_LABELS.get(str(s.get("type", "")), "通用")
            names.append(f"{s.get('display_name', s.get('id', ''))}[{label}]")
        return ", ".join(names) if names else "无"

    # ---------- 写接口 ----------
    def set_enabled(self, value: bool):
        self._data["web_search_enabled"] = bool(value)
        self._save()

    @staticmethod
    def get_settings_path() -> str:
        return _INTERNET_SETTING_PATH
