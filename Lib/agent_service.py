# Lib/agent_service.py
# -*- coding: utf-8 -*-
"""
AgentService：LLM 会话、对话历史、文件上传、用户消息编排。
"""

import os
import time
import random

from conversation_store import ConversationStore
from generation_guard import GenerationGuard
from prompt import SYSTEM_PROMPT, FALLBACK_REPLIES


class AgentService:
    def __init__(
        self,
        llm_manager,
        on_bubble=None,
        notify_user=None,
    ):
        self.llm_manager = llm_manager

        self.conversation_store = ConversationStore(max_turns=12)
        self.system_prompt = SYSTEM_PROMPT
        self.guard = GenerationGuard()

        self.docs_uploader = None
        self.message_processor = None

        self._file_poll_interval = 0.5
        self._next_file_poll_time = 0.0

        self.on_bubble = on_bubble
        self.notify_user = notify_user

        from services import MessageProcessor
        self.message_processor = MessageProcessor(self)

    # ==================== 上下文访问（供 MessageProcessor / ToolLoopDriver） ====================
    @property
    def llm_engine(self):
        return self.llm_manager.engine

    @property
    def model_ready(self):
        return self.llm_manager.is_ready

    @property
    def model_loading(self):
        return self.llm_manager.is_loading

    @property
    def max_history(self):
        return self.conversation_store.max_turns

    @property
    def conversation_history(self):
        return self.conversation_store.to_flat_list()

    @conversation_history.setter
    def conversation_history(self, value):
        if not value:
            self.conversation_store.clear()

    def next_generation(self) -> int:
        return self.guard.next()

    def is_generation_current(self, gen_id) -> bool:
        return self.guard.is_current(gen_id)

    def _fallback_reply(self, user_msg) -> str:
        return random.choice(FALLBACK_REPLIES)

    def queue_bubble(self, text: str):
        if self.on_bubble:
            try:
                self.on_bubble(text)
            except Exception:
                pass

    def _notify(self, text: str):
        if self.notify_user:
            try:
                self.notify_user(text)
            except Exception:
                pass
        else:
            self.queue_bubble(text)

    # ==================== 文件服务绑定 ====================
    def _get_active_uploader(self):
        return self.docs_uploader

    def bind_chat_window(self, chat_window):
        if chat_window is None:
            return

        if self.docs_uploader is None:
            self.docs_uploader = getattr(chat_window, "doc_uploader", None)

        cw_vm = getattr(chat_window, "vm", None)
        if cw_vm is not None:
            cw_processor = getattr(cw_vm, "msg_processor", None)
            if cw_processor is not None and cw_processor is not self.message_processor:
                self.message_processor = cw_processor

        if self.message_processor is not None and self.docs_uploader is not None:
            if getattr(self.message_processor, "uploader", None) is not self.docs_uploader:
                self.message_processor.uploader = self.docs_uploader
                
    # ==================== 文件上传轮询 ====================
    def poll(self):
        now = time.time()
        if now < self._next_file_poll_time:
            return
        self._next_file_poll_time = now + self._file_poll_interval

        uploader = self._get_active_uploader()
        if uploader is None:
            return

        attachments, errors = uploader.consume_results()

        if attachments:
            msg = self._format_upload_success_message(attachments)
            self._notify(msg)
            if self.message_processor is not None:
                self.message_processor.last_exportable = None
                self.message_processor.cache_attachments(attachments)

        if errors:
            for error in errors:
                self._notify(error.get("reason", "文件处理失败了。"))

    @staticmethod
    def _format_upload_success_message(attachments):
        items = []
        for att in attachments:
            filename = att.get("filename") or ""
            base_name = os.path.splitext(os.path.basename(filename))[0] or "文件"
            short_name = base_name[:4] + "..." if len(base_name) > 4 else base_name
            label = att.get("spoken_label") or "文件"
            file_no = att.get("file_no", "?")
            items.append(f"{label}{short_name}（{file_no}号）")
        if not items:
            return "预处理成功啦！"
        return "、".join(items) + " 预处理成功啦！"

    # ==================== 用户消息主入口 ====================
    def handle_user_message(self, user_msg: str) -> str:
        if not user_msg:
            return ""

        self.poll()

        uploader = self._get_active_uploader()
        if uploader:
            try:
                uploader.on_turn_finished()
            except Exception:
                pass

        processor = self.message_processor
        if processor is None:
            return self._fallback_reply(user_msg)

        file_nos = []
        if hasattr(processor, "cache_pool"):
            file_nos = processor.cache_pool.resolve_file_nos(user_msg)

        reply = processor.get_reply(user_msg, file_nos=file_nos)

        if processor and hasattr(processor, "cache_pool"):
            processor.cache_pool.tick()

        return reply or "（模型没有生成有效回复）"

    # ==================== 清理 ====================
    def clear_for_reload(self):
        self.conversation_store.clear()

        uploader = self._get_active_uploader()
        if uploader:
            try:
                uploader.clear_all(reset_counters=True)
                uploader.reset_upload_state()
            except Exception as e:
                print(f"[AgentService] 清理 uploader 失败: {e}")

        if self.message_processor:
            try:
                self.message_processor.clear_cache()
                self.message_processor.force_clear_cache()
            except Exception as e:
                print(f"[AgentService] 清理 message_processor 失败: {e}")

    def shutdown(self):
        uploader = self._get_active_uploader()
        if uploader:
            try:
                if hasattr(uploader, "shutdown"):
                    uploader.shutdown()
                else:
                    uploader.clear_all(reset_counters=True)
            except Exception as e:
                print(f"[AgentService] shutdown uploader 失败: {e}")

        if self.message_processor:
            try:
                self.message_processor.clear_cache()
                self.message_processor.force_clear_cache()
            except Exception:
                pass
