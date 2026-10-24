# -*- coding: utf-8 -*-
"""
response_stats.py —— 单轮回复统计模块
=====================================
"""

import time
import urllib.request
import json
from dataclasses import dataclass, field


# ============================================================
# 1. Token 估算（服务端未返回 usage 时的兜底）
# ============================================================

def estimate_text_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // 2)


def estimate_messages_tokens(messages) -> int:
    total = 0

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

                part_type = part.get("type")

                if part_type == "text":
                    total += estimate_text_tokens(part.get("text", ""))
                elif part_type == "image_url":
                    total += 1536
    return total

# ============================================================
# 2. 流式钩子（在 model_engine 子进程内调用）
# ============================================================

def inject_stream_options(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload

    stream_options = payload.get("stream_options")
    if not isinstance(stream_options, dict):
        stream_options = {}

    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options

    return payload


def capture_usage_from_chunk(chunk, holder: dict) -> None:
    if not isinstance(chunk, dict):
        return
    if not isinstance(holder, dict):
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
# 3. KV 缓存使用率探测（基于 /slots 端点）
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

    used_total = 0
    ctx_total = 0

    for s in slots:
        if not isinstance(s, dict):
            continue

        try:
            n_ctx = int(s.get("n_ctx") or 0)
            n_used = int(s.get("n_prompt_tokens") or 0)
        except Exception:
            continue

        if n_ctx > 0:
            ctx_total += n_ctx
            used_total += max(0, min(n_used, n_ctx))

    if ctx_total <= 0:
        return None

    return used_total / ctx_total
# ============================================================
# 4. 核心统计类
# ============================================================

@dataclass
class ResponseStats:
    """一次用户提问的完整统计（自动累加 Agent 多轮调用）"""
    created_at: float = field(default_factory=time.perf_counter)
    finished_at: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    kv_cache_usage: float = -1.0   # -1 = 未知
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
    
    def add_estimate(self, messages=None, reply_text: str = "") -> None:
        pt = estimate_messages_tokens(messages) if messages else 0
        ct = estimate_text_tokens(reply_text)
        if pt <= 0 and ct <= 0: return
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.model_calls += 1
        self.estimated = True

    def set_kv_usage(self, value) -> None:
        if isinstance(value, (int, float)) and value >= 0:
            self.kv_cache_usage = max(0.0, min(float(value), 1.0))

    def record_raw_dict(self, raw, messages=None) -> None:
        if not isinstance(raw, dict): return
        usage = raw.get("usage")
        if usage:
            self.add_usage(usage)
        else:
            self.add_estimate(messages=messages, reply_text=raw.get("content", ""))
        self.set_kv_usage(raw.get("kv_cache_used"))

    def record_stream_result(self, usage=None, kv=None, messages=None, reply_text: str = "") -> None:
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

    def is_meaningful(self) -> bool:
        return self.model_calls > 0 or self.total_tokens > 0 or self.kv_cache_usage >= 0

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        if sec >= 60:
            m, s = divmod(sec, 60)
            return f"{int(m)}m{s:.1f}s"
        return f"{sec:.1f}s"

    def format_line(self) -> str:
        parts = [f"{self._fmt_duration(self.elapsed)}"]
        
        if self.completion_tokens > 0:
            speed = self.completion_tokens / self.elapsed if self.elapsed > 0 else 0
            tok = f"{self.completion_tokens} tokens · {speed:.1f} t/s"
            if self.estimated:
                tok += "≈"
            parts.append(tok)
                        
        if self.kv_cache_usage >= 0:
            parts.append(f"KV {self.kv_cache_usage * 100:.1f}%")
            
        if self.model_calls > 1:
            parts.append(f"×{self.model_calls}轮")
            
        return " · ".join(parts)
