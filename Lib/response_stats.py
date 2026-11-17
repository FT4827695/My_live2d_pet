# -*- coding: utf-8 -*-

import os
import re
import time
import json
import urllib.request
from collections import deque
from dataclasses import dataclass, field


# ============================================================
# 0. 环境配置
# ============================================================
def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


# ============================================================
# 1. 上下文预算
# ============================================================
DEFAULT_MAX_GEN_TOKENS = 8192


def get_n_ctx() -> int:
    return max(512, _env_int("XLLAMA_CTX", 8192))


def get_max_gen_tokens(n_ctx=None) -> int:
    n_ctx = get_n_ctx() if n_ctx is None else max(512, int(n_ctx))
    cap = _env_int("XLLAMA_MAX_GEN_TOKENS", DEFAULT_MAX_GEN_TOKENS)
    ratio = _env_float("XLLAMA_GEN_RATIO", 0.4)
    return max(256, min(cap, int(n_ctx * ratio)))


def get_max_prompt_tokens(n_ctx=None) -> int:
    n_ctx = get_n_ctx() if n_ctx is None else max(512, int(n_ctx))
    return max(256, n_ctx - get_max_gen_tokens(n_ctx))


# ============================================================
# 2. Token 估算
# ============================================================
_CJK_RE = re.compile(r'[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]')


def estimate_text_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk_div = max(0.5, _env_float("XLLAMA_TOK_CJK_DIV", 1.3))
    other_div = max(1.0, _env_float("XLLAMA_TOK_OTHER_DIV", 4.0))
    other = len(_CJK_RE.sub("", text))
    cjk = len(text) - other
    return max(1, int(cjk / cjk_div + other / other_div))


def get_image_token_cost() -> int:
    return max(256, _env_int("XLLAMA_IMAGE_TOKENS", 1536))


def estimate_messages_tokens(messages) -> int:
    total = 0
    images = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    total += estimate_text_tokens(part.get("text", ""))
                elif ptype == "image_url":
                    images += 1
        reasoning = m.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            total += estimate_text_tokens(reasoning)
        for tc in (m.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            total += estimate_text_tokens(fn.get("name", ""))
            total += estimate_text_tokens(fn.get("arguments", ""))
    return total + images * get_image_token_cost()


# ============================================================
# 3. 下一轮预留量
# ============================================================
_RECENT_GEN = deque(maxlen=max(2, _env_int("XLLAMA_RESERVE_WINDOW", 12)))
_RECENT_TOOL = deque(maxlen=max(2, _env_int("XLLAMA_RESERVE_WINDOW", 12)))


def note_generation_tokens(n) -> None:
    try:
        n = int(n)
    except Exception:
        return
    if n > 0:
        _RECENT_GEN.append(n)


def note_tool_result_tokens(n) -> None:
    try:
        n = int(n)
    except Exception:
        return
    if n > 0:
        _RECENT_TOOL.append(n)


def estimate_reserve_tokens(n_ctx=None, max_gen_tokens=None) -> int:
    n_ctx = get_n_ctx() if n_ctx is None else max(512, int(n_ctx))
    if max_gen_tokens is None:
        max_gen_tokens = get_max_gen_tokens(n_ctx)
    safety = max(1.0, _env_float("XLLAMA_RESERVE_SAFETY", 1.5))
    floor = max(64, _env_int("XLLAMA_RESERVE_FLOOR", 512))

    if _RECENT_GEN:
        gen_est = min(max_gen_tokens, max(floor, int(max(_RECENT_GEN) * safety)))
    else:
        gen_est = max_gen_tokens

    tool_est = int(max(_RECENT_TOOL) * safety) if _RECENT_TOOL else 0
    return gen_est + tool_est


def get_kv_wrapup_threshold(n_ctx=None) -> float:
    n_ctx = get_n_ctx() if n_ctx is None else max(512, int(n_ctx))
    base = _env_float("XLLAMA_KV_WRAPUP", 0.85)
    floor = _env_float("XLLAMA_KV_WRAPUP_FLOOR", 0.30)
    reserve = estimate_reserve_tokens(n_ctx)
    return max(floor, min(base, base - reserve / n_ctx))


# ============================================================
# 4. 流式钩子
# ============================================================
def inject_stream_options(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    opts = payload.get("stream_options")
    if not isinstance(opts, dict):
        opts = {}
    opts["include_usage"] = True
    payload["stream_options"] = opts
    return payload


def capture_usage_from_chunk(chunk, holder: dict) -> None:
    if not isinstance(chunk, dict) or not isinstance(holder, dict):
        return
    try:
        usage = chunk.get("usage")
        if isinstance(usage, dict) and (
            usage.get("prompt_tokens")
            or usage.get("completion_tokens")
            or usage.get("total_tokens")
        ):
            holder["usage"] = usage
    except Exception:
        pass


# ============================================================
# 5. KV 使用率探测
# ============================================================
def _resolve_base_url(server):
    addr = getattr(server, "listening_address", None)
    if callable(addr):
        try:
            addr = addr()
        except Exception:
            return None
    if isinstance(addr, bytes):
        addr = addr.decode("utf-8", errors="replace")
    if not isinstance(addr, str) or not addr.strip():
        return None
    addr = addr.strip()
    if not addr.startswith("http"):
        addr = "http://" + addr
    return addr.rstrip("/")


def probe_kv_cache_usage(server):
    if server is None:
        return None
    base = _resolve_base_url(server)
    if not base:
        return None
    try:
        req = urllib.request.Request(base + "/slots", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as r:
            slots = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(slots, list) or not slots:
        return None
    ratios = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        try:
            n_ctx = int(s.get("n_ctx") or 0)
            n_used = int(s.get("n_prompt_tokens") or 0)
        except Exception:
            continue
        if n_ctx > 0:
            ratios.append(max(0.0, min(1.0, n_used / n_ctx)))
    return max(ratios) if ratios else None


# ============================================================
# 6. 核心统计类
# ============================================================
@dataclass
class ResponseStats:
    created_at: float = field(default_factory=time.perf_counter)
    finished_at: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    kv_cache_usage: float = -1.0
    kv_peak: float = -1.0
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    estimated: bool = False

    # ---------- 记录 ----------
    def add_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        try:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
        except Exception:
            return
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict):
            details = {}
        try:
            cached = int(details.get("cached_tokens") or 0)
        except Exception:
            cached = 0
        if pt <= 0 and ct <= 0:
            return
        self.prompt_tokens += max(pt, 0)
        self.completion_tokens += max(ct, 0)
        self.cached_tokens += max(cached, 0)
        self.model_calls += 1
        if _env_int("XLLAMA_STATS_DEBUG", 0):
            print(f"[Stats] raw usage: {usage}")        
        note_generation_tokens(ct)

    def add_estimate(self, messages=None, reply_text: str = "") -> None:
        pt = estimate_messages_tokens(messages) if messages else 0
        ct = estimate_text_tokens(reply_text)
        if pt <= 0 and ct <= 0:
            return
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.model_calls += 1
        self.estimated = True
        note_generation_tokens(ct)

    def set_kv_usage(self, value) -> None:
        if isinstance(value, (int, float)) and value >= 0:
            v = max(0.0, min(float(value), 1.0))
            self.kv_cache_usage = v
            self.kv_peak = max(self.kv_peak, v)

    def add_model_time(self, seconds) -> None:
        try:
            s = float(seconds)
        except Exception:
            return
        if s > 0:
            self.model_seconds += s

    def add_tool_time(self, seconds) -> None:
        try:
            s = float(seconds)
        except Exception:
            return
        if s > 0:
            self.tool_seconds += s

    def record_raw_dict(self, raw, messages=None) -> None:
        if not isinstance(raw, dict):
            return
        usage = raw.get("usage")
        if usage:
            self.add_usage(usage)
        else:
            self.add_estimate(messages=messages, reply_text=raw.get("content", ""))
        self.set_kv_usage(raw.get("kv_cache_used"))

    def record_stream_result(self, usage=None, kv=None, messages=None, reply_text="") -> None:
        if usage:
            self.add_usage(usage)
        else:
            self.add_estimate(messages=messages, reply_text=reply_text)
        self.set_kv_usage(kv)

    # ---------- 结束与输出 ----------
    def finish(self) -> None:
        if self.finished_at <= 0:
            self.finished_at = time.perf_counter()

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at > 0 else time.perf_counter()
        return max(0.0, end - self.created_at)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def reuse_ratio(self) -> float:
        if self.estimated or self.prompt_tokens <= 0:
            return -1.0
        mode = os.environ.get("XLLAMA_REUSE_DENOM", "inclusive").strip().lower()
        denom = (self.prompt_tokens + self.cached_tokens
                 if mode == "disjoint" else self.prompt_tokens)
        if denom <= 0:
            return -1.0
        return max(0.0, min(1.0, self.cached_tokens / denom))
    
    def is_meaningful(self) -> bool:
        return self.model_calls > 0 or self.total_tokens > 0 or self.kv_cache_usage >= 0

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        if sec >= 60:
            m, s = divmod(sec, 60)
            return f"{int(m)}m{s:.1f}s"
        return f"{sec:.1f}s"

    def format_line(self) -> str:
        parts = [self._fmt_duration(self.elapsed)]
        if self.completion_tokens > 0:
            speed = self.completion_tokens / self.elapsed if self.elapsed > 0 else 0.0
            tok = f"{self.completion_tokens} tokens · {speed:.1f} t/s"
            if self.estimated:
                tok += "≈"
            parts.append(tok)
        if self.kv_cache_usage >= 0:
            parts.append(f"KV {self.kv_cache_usage * 100:.1f}%")
        if self.model_calls > 1:
            parts.append(f"×{self.model_calls}轮")
        if _env_int("XLLAMA_SHOW_REUSE", 0):
            r = self.reuse_ratio
            if r >= 0:
                lo = _env_float("XLLAMA_REUSE_SHOW_LOW", 0.25)
                hi = _env_float("XLLAMA_REUSE_SHOW_HIGH", 0.85)
                if r < lo or r > hi:
                    parts.append(f"复用{r * 100:.0f}%")
        if _env_int("XLLAMA_STATS_VERBOSE", 0) and self.tool_seconds > 0.05:
            parts.append(f"模{self.model_seconds:.1f}s/工{self.tool_seconds:.1f}s")
        return " · ".join(parts)
