# -*- coding: utf-8 -*-

import os
import json

from settings_store import BASE_DIR


_CAPABILITIES_FILE = os.path.join(BASE_DIR, "config", "model_capabilities.json")

def _load_config():
    global _config_cache

    if _config_cache is not None:
        return _config_cache

    try:
        with open(_CAPABILITIES_FILE, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)

        if not isinstance(_config_cache, dict):
            _config_cache = {}

    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[Capabilities] 读取 model_capabilities.json 失败: {e}")
        _config_cache = {}

    return _config_cache


def _normalize_keyword(value) -> str:
    return str(value or "").strip().lower()


def _match_unsupported_function_calling(model_name_lower: str, config: dict) -> bool:
    unsupported = config.get("_unsupported_function_calling", [])

    if not isinstance(unsupported, list):
        return False

    for keyword in unsupported:
        kw = _normalize_keyword(keyword)
        if kw and kw in model_name_lower:
            return True

    return False


def match_capabilities(model_name) -> dict:
    config = _load_config()

    result = dict(config.get("_default", {}))

    result["function_calling"] = True

    model_name_lower = _normalize_keyword(model_name)

    if not model_name_lower:
        return result

    keywords = [
        k for k in config.keys()
        if not str(k).startswith("_")
    ]

    keywords.sort(key=len, reverse=True)

    for keyword in keywords:
        keyword_lower = _normalize_keyword(keyword)

        if keyword_lower and keyword_lower in model_name_lower:
            matched = config.get(keyword) or {}

            if isinstance(matched, dict):
                for k, v in matched.items():
                    if k != "function_calling":
                        result[k] = v

            break

    if _match_unsupported_function_calling(model_name_lower, config):
        result["function_calling"] = False

    return result
