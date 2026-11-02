# internet_settings.py
import os
import json
import copy
import threading

from settings_store import BASE_DIR
import secret_store


_INTERNET_SETTING_PATH = os.path.join(BASE_DIR, "config", "Internet_setting.json")

_TAVILY_DEFAULT_API_BASE = "https://api.tavily.com"

_DEFAULT_SETTINGS = {
    "web_search_enabled": False,
    "proxy": "",
    "request_timeout_sec": 15,
    "max_results_per_source": 3,
    "cache_ttl_sec": 600,
    "max_snippet_chars": 2048,
    "max_article_chars": 4096,
    "tavily_enabled": True,
    "tavily_fallback_to_general": True,
    "search_apis": [
        {
            "id": "api_1",
            "display_name": "API 1",
            "api_base": _TAVILY_DEFAULT_API_BASE,
            "api_key_enc": "",
            "enabled": True,
        },
        {
            "id": "api_2",
            "display_name": "API 2",
            "api_base": "",
            "api_key_enc": "",
            "enabled": False,
        },
        {
            "id": "api_3",
            "display_name": "API 3",
            "api_base": "",
            "api_key_enc": "",
            "enabled": False,
        },
    ],
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
    def _normalize_search_apis(self):
        defaults = copy.deepcopy(_DEFAULT_SETTINGS["search_apis"])
        incoming = self._data.get("search_apis")
        if not isinstance(incoming, list):
            self._data["search_apis"] = defaults
            return

        by_id = {}
        for item in incoming:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item

        merged = []
        for default in defaults:
            existing = by_id.get(default["id"])
            if isinstance(existing, dict):
                base = copy.deepcopy(default)
                if isinstance(existing.get("api_base"), str):
                    base["api_base"] = existing["api_base"]
                if isinstance(existing.get("api_key_enc"), str):
                    base["api_key_enc"] = existing["api_key_enc"]
                base["enabled"] = bool(existing.get("enabled", default["enabled"]))
                merged.append(base)
            else:
                merged.append(copy.deepcopy(default))
        self._data["search_apis"] = merged

    def _load(self):
        try:
            if os.path.exists(_INTERNET_SETTING_PATH):
                with open(_INTERNET_SETTING_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self._data = copy.deepcopy(_DEFAULT_SETTINGS)
                    self._data.update(saved)
                    # 清理旧明文字段
                    self._data.pop("tavily_api_key", None)
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

        self._normalize_search_apis()

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
    def tavily_fallback_to_general(self) -> bool:
        return bool(self._data.get("tavily_fallback_to_general", True))

    # ---------- 搜索 API 槽位 ----------
    def _find_api(self, api_id: str):
        for item in self._data.get("search_apis", []):
            if isinstance(item, dict) and item.get("id") == api_id:
                return item
        return None

    def get_search_api_ui(self) -> list:
        out = []
        for item in self._data.get("search_apis", []):
            if not isinstance(item, dict):
                continue
            out.append({
                "id": item.get("id", ""),
                "display_name": item.get("display_name", ""),
                "api_base": str(item.get("api_base", "")),
                "has_key": bool(item.get("api_key_enc")),
                "enabled": bool(item.get("enabled", False)),
            })
        return out

    def get_api_base(self, api_id: str) -> str:
        item = self._find_api(api_id)
        if item is None:
            return ""
        return str(item.get("api_base", "")).strip()

    def get_api_key(self, api_id: str) -> str:
        item = self._find_api(api_id)
        if item is None:
            return ""
        token = str(item.get("api_key_enc", "")).strip()
        if not token:
            return ""
        return secret_store.decrypt_text(token)

    def is_api_enabled(self, api_id: str) -> bool:
        item = self._find_api(api_id)
        if item is None:
            return False
        return bool(item.get("enabled", False))

    @staticmethod
    def _is_safe_api_base(url: str) -> bool:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return False
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower().split(":")[0].strip("[]")
        except Exception:
            return False
        if not host:
            return False
        if host in ("localhost", "0.0.0.0", "127.0.0.1", "::1"):
            return False
        if host.endswith((".local", ".localhost", ".internal")):
            return False
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            try:
                a = int(parts[0])
                b = int(parts[1])
                if a in (0, 10, 127):
                    return False
                if a == 169 and b == 254:
                    return False
                if a == 172 and 16 <= b <= 31:
                    return False
                if a == 192 and b == 168:
                    return False
            except Exception:
                pass
        return True

    def save_search_apis(self, entries: list):
        if not isinstance(entries, list):
            return False, "参数格式错误"

        by_id = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            api_id = str(entry.get("id", "")).strip()
            if api_id:
                by_id[api_id] = entry

        plans = []
        for item in self._data.get("search_apis", []):
            if not isinstance(item, dict):
                continue
            api_id = item.get("id", "")
            incoming = by_id.get(api_id)
            if incoming is None:
                continue

            display = item.get("display_name", api_id)

            enabled = bool(incoming.get("enabled", False))
            api_base = str(incoming.get("api_base", "")).strip()

            if api_id == "api_1" and enabled and not api_base:
                api_base = _TAVILY_DEFAULT_API_BASE

            if api_base and not self._is_safe_api_base(api_base):
                return False, f"{display} 的 API 链接不合法（仅允许公网 http/https 地址）"

            key_enc = None
            if not api_base:
                key_enc = ""
            elif "api_key" in incoming:
                raw_key = incoming.get("api_key")
                if isinstance(raw_key, str) and raw_key != "":
                    try:
                        key_enc = secret_store.encrypt_text(raw_key)
                    except Exception:
                        return False, f"{display} 的密钥加密失败"

            plans.append((item, api_base, enabled, key_enc))
            
        with self._io_lock:
            for item, api_base, enabled, key_enc in plans:
                item["api_base"] = api_base
                item["enabled"] = enabled
                if key_enc is not None:
                    item["api_key_enc"] = key_enc
            self._save()
        return True, ""

    # ---------- 来源 ----------
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
        if self.tavily_enabled and self.is_api_enabled("api_1"):
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
