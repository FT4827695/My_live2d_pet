# Lib/conversation_store.py
# -*- coding: utf-8 -*-

import json


class ConversationStore:
    MAX_TRACE_HEAD = 256

    def __init__(self, max_turns: int = 12):
        self.max_turns = max_turns
        self._turns = []   # [{"user": str, "assistant": str, "tool_trace": list}]

    # ---------- 写 ----------
    def append_turn(self, user: str, assistant: str, tool_trace=None):
        self._turns.append({
            "user": user or "",
            "assistant": assistant or "",
            "tool_trace": tool_trace or [],
        })
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns:]

    def clear(self):
        self._turns.clear()

    # ---------- 工具摘要 ----------
    @classmethod
    def _format_tool_trace(cls, tool_trace) -> str:
        if not tool_trace:
            return ""
        lines = ["\n[上轮工具调用]"]
        for t in tool_trace:
            name = t.get("name", "?")
            args = t.get("args") or {}
            ok = bool(t.get("ok", False))
            head = str(t.get("result_head", "") or "").strip()
            if len(head) > cls.MAX_TRACE_HEAD:
                head = head[:cls.MAX_TRACE_HEAD] + "..."

            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except Exception:
                args_str = str(args)
            if len(args_str) > 80:
                args_str = args_str[:80] + "..."

            status = "成功" if ok else "失败"
            if head:
                lines.append(f"- {name}({args_str}) → {status}：{head}")
            else:
                lines.append(f"- {name}({args_str}) → {status}")
        return "\n".join(lines)

    @classmethod
    def _compose_assistant_content(cls, assistant_text: str, tool_trace) -> str:
        summary = cls._format_tool_trace(tool_trace)
        if not summary:
            return assistant_text
        return f"{assistant_text}\n{summary}"

    # ---------- 读 ----------
    def build_api_messages(
        self,
        system: str,
        user_content: str,
        estimate_fn,
        max_prompt_tokens: int,
        image_tokens: int = 0,
    ):
        fixed_tokens = (
            estimate_fn(system)
            + estimate_fn(user_content)
            + max(0, image_tokens)
        )
        remaining = max_prompt_tokens - fixed_tokens

        selected = []   # [(user_text, assistant_text_with_summary)]
        if remaining > 0:
            used = 0
            for turn in reversed(self._turns):
                user_text = turn["user"]
                assistant_text = self._compose_assistant_content(
                    turn["assistant"], turn.get("tool_trace") or []
                )
                turn_tokens = estimate_fn(user_text) + estimate_fn(assistant_text)
                if used + turn_tokens > remaining:
                    break
                selected.append((user_text, assistant_text))
                used += turn_tokens

        selected.reverse()

        flat = []
        for u, a in selected:
            flat.append({"role": "user", "content": u})
            flat.append({"role": "assistant", "content": a})
            
        return [
            {"role": "system", "content": system},
            *flat,
            {"role": "user", "content": user_content},
        ]

    def to_flat_list(self):
        flat = []
        for turn in self._turns:
            flat.append({"role": "user", "content": turn["user"]})
            flat.append({
                "role": "assistant",
                "content": self._compose_assistant_content(
                    turn["assistant"], turn.get("tool_trace") or []
                ),
            })
        return flat

    def __len__(self):
        return len(self._turns)
