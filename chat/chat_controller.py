"""
chat_controller.py
聊天业务逻辑：消息管理、发送、更新队列消费、消息选择/复制。
"""
import queue
from services import ClipboardService, TextUtils


class ChatController:
    def __init__(self, vm, input_box, doc_uploader, scroll_manager, selected_messages,
                 request_update, mark_dirty, get_active, get_clean_message_lines):
        self.vm = vm
        self.input_box = input_box
        self.doc_uploader = doc_uploader
        self.scroll_manager = scroll_manager
        self.selected_messages = selected_messages
        self.request_update = request_update
        self.mark_dirty = mark_dirty
        self.get_active = get_active
        self.get_clean_message_lines = get_clean_message_lines
        self._bind_uploader_to_services()
        self._is_sending = False

    def _bind_uploader_to_services(self):
        pet = getattr(self.vm, "pet", None)
        if pet is not None:
            if getattr(pet, "docs_uploader", None) is None:
                pet.docs_uploader = self.doc_uploader
            processor = getattr(self.vm, "msg_processor", None)
            if processor is not None:
                uploader = getattr(pet, "docs_uploader", None) or self.doc_uploader
                if getattr(processor, "uploader", None) is None:
                    processor.uploader = uploader

    def notify_user(self, text):
        pet = getattr(self.vm, "pet", None)
        if pet is None:
            return False

        delivered = False

        chat_window = getattr(pet, "chat_window", None)
        if chat_window is not None and getattr(chat_window, "active", False):
            try:
                chat_window.add_message("shizuku", text)
                delivered = True
            except Exception as e:
                print(f"[ChatController] notify_user 写聊天窗口失败: {e}")

        try:
            if hasattr(pet, "queue_bubble"):
                pet.queue_bubble(text)
                delivered = True
        except Exception as e:
            print(f"[ChatController] notify_user queue_bubble 失败: {e}")

        return delivered
    
    # ==================== 消息管理 ====================

    def add_message(self, sender, text):
        text = TextUtils.clean_text(text)
        old_visible = self.scroll_manager.scrollbar_visible
        self.vm.add_message(sender, text)
        self.scroll_manager.cached_total_lines = None
        if self.scroll_manager.scroll_to_bottom(force=True):
            self.mark_dirty()
        self.selected_messages.clear()
        if self.get_active() and self.scroll_manager.cursor_in_msg_area:
            self.scroll_manager.update_scrollbar_visibility(cursor_in_area=True)
        if self.scroll_manager.scrollbar_visible != old_visible:
            self.request_update(urgent=True)
        else:
            self.request_update()
        self.mark_dirty()

    def clear_chat(self):
        self.scroll_manager.cached_total_lines = None
        self.scroll_manager.scroll_offset = 0
        if self.scroll_manager.scroll_to_bottom(force=True):
            self.request_update()
            self.mark_dirty()

    # ==================== 消息发送 ====================

    def send_message(self):
        if self._is_sending:
            return

        user_msg = self.input_box.get_text().strip()
        if not user_msg:
            return

        self._is_sending = True

        try:
            self.add_message("user", user_msg)
            self.input_box.set_text("")

            pet = getattr(self.vm, "pet", None)
            if pet is not None and hasattr(pet, "poll_file_upload"):
                try:
                    pet.poll_file_upload()
                except Exception as e:
                    print(f"[ChatController] poll_file_upload 异常: {e}")

            if self.doc_uploader:
                self.doc_uploader.on_turn_finished()
                self.request_update(urgent=True)
                self.mark_dirty()

            processor = getattr(self.vm, "msg_processor", None)

            file_nos = []
            if processor and hasattr(processor, 'cache_pool'):
                file_nos = processor.cache_pool.resolve_file_nos(user_msg)

            self.vm.send_message_async(user_msg, file_nos=file_nos)

            self.scroll_manager.cached_total_lines = None
            if self.scroll_manager.scroll_to_bottom(force=True):
                self.request_update()
                self.mark_dirty()

            self.request_update()
            self.mark_dirty()

        finally:
            self._is_sending = False
            
    # ==================== 更新队列 ====================

    def process_updates(self):
        stream_chunks = {}
        try:
            while True:
                item = self.vm.update_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 3 and item[0] == 'stream_chunk':
                    _, idx, chunk = item
                    stream_chunks[idx] = stream_chunks.get(idx, "") + chunk
                    continue
                if isinstance(item, tuple) and len(item) == 2:
                    if item[0] == 'voice_text':
                        self.input_box.insert_text(item[1])
                        self.request_update(urgent=True)
                        self.mark_dirty()
                    elif item[0] == 'announce':
                        self.add_message("shizuku", item[1])
                    elif isinstance(item[0], int):
                        thinking_idx, reply = item
                        if reply not in ("（放弃思考）", "（已停止生成）"):
                            self._append_stats_line()
                        if thinking_idx < len(self.vm.messages):
                            sender, current_text = self.vm.messages[thinking_idx]
                            if (
                                sender == "shizuku"
                                and current_text != "（思考中...）"
                                and reply not in ("（放弃思考）", "（已停止生成）")
                            ):
                                continue
                            stream_chunks.pop(thinking_idx, None)
                            self.vm.update_message(thinking_idx, reply)
                            self.scroll_manager.cached_total_lines = None
                            if self.scroll_manager.scroll_to_bottom(force=False):
                                self.mark_dirty()
                            self.request_update(urgent=True)
                            self.mark_dirty()
        except queue.Empty:
            pass
        if stream_chunks:
            for idx, chunks in stream_chunks.items():
                if idx < len(self.vm.messages):
                    sender, text = self.vm.messages[idx]
                    if text in ("（放弃思考）", "（已停止生成）"):
                        continue
                    if text == "（思考中...）":
                        self.vm.update_message(idx, chunks)
                    else:
                        self.vm.update_message(idx, text + chunks)
            self.scroll_manager.cached_total_lines = None
            if self.scroll_manager.scroll_to_bottom(force=False):
                self.mark_dirty()
            self.request_update(urgent=False)
            self.mark_dirty()

    def _append_stats_line(self):
        try:
            processor = getattr(self.vm, "msg_processor", None)
            stats = getattr(processor, "last_stats", None) if processor else None
            if stats is None or not stats.is_meaningful():
                return
            stats.finish()
            line = stats.format_line()
            if not line:
                return
            processor.last_stats = None        # 防止重复消费
            self.add_message("stats", line)    # 单独一行显示
        except Exception as e:
            print(f"[Stats] 统计行显示失败: {e}")
            
    # ==================== 文件操作 ====================

    def start_file_selection(self):
        self.doc_uploader.select_file_async()

    def clear_file(self):
        if self.doc_uploader:
            removed_file_nos = self.doc_uploader.clear_staged(reset_counters=True)
            # 从缓存池中删除对应条目
            vm = getattr(self, "_vm", None) or getattr(self, "vm", None)
            if vm and getattr(vm, "msg_processor", None):
                processor = vm.msg_processor
                processor.clear_cache()
                for fn in removed_file_nos:
                    processor.cache_pool.remove_by_file_no(fn)
                print(f"[ClearFile] 已从缓存池移除 {len(removed_file_nos)} 个条目: {removed_file_nos}")
            
    # ==================== 消息复制 ====================

    def copyselected_messages(self):
        if not self.selected_messages:
            return False
        sorted_indices = sorted(self.selected_messages)
        lines = []
        for idx in sorted_indices:
            if idx >= len(self.vm.messages):
                continue
            sender, text = self.vm.messages[idx]
            if sender == "user":
                prefix = "我: "
            elif sender == "stats":
                prefix = ""
            else:
                prefix = "雫: "
            lines.append(prefix + text)
        if lines:
            ClipboardService.set_text("\n".join(lines))
        return True

    def copy_msg_text_range(self, selection):
        if not selection or not self.get_clean_message_lines:
            return ""
        (s_msg, s_line, s_char), (e_msg, e_line, e_char) = selection
        messages = self.vm.messages
        result_lines = []
        for msg_idx in range(s_msg, min(e_msg + 1, len(messages))):
            sender, text = messages[msg_idx]
            clean_lines = self.get_clean_message_lines(sender, text)
            if not clean_lines:
                continue
            start_line = s_line if msg_idx == s_msg else 0
            end_line = e_line if msg_idx == e_msg else len(clean_lines) - 1
            for line_idx in range(start_line, min(end_line + 1, len(clean_lines))):
                line_text = clean_lines[line_idx]
                char_start = s_char if (msg_idx == s_msg and line_idx == s_line) else 0
                char_end = e_char if (msg_idx == e_msg and line_idx == e_line) else len(line_text)
                result_lines.append(line_text[char_start:char_end])
        return "\n".join(result_lines)
