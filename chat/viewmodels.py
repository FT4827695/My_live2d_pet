import threading
import queue
from services import MessageProcessor


class ChatViewModel:

    def __init__(self, pet_instance, wake_up_func=None):
        self.pet = pet_instance
        self.messages = []
        self.msg_processor = MessageProcessor(pet_instance)
        self.msg_processor.uploader = getattr(pet_instance, "docs_uploader", None)
        self.update_queue = queue.Queue()
        self.wake_up_func = wake_up_func
        self.is_generating = False
        self._generating_idx = None

    def add_message(self, sender: str, text: str):
        self.messages.append((sender, text))

    def update_message(self, index: int, new_text: str):
        if 0 <= index < len(self.messages):
            self.messages[index] = (self.messages[index][0], new_text)

    def clear_messages(self):
        self.messages.clear()

    def stop_generation(self):
        if not getattr(self, "is_generating", False):
            return False
        if hasattr(self.pet, "next_generation"):
            try:
                self.pet.next_generation()
            except Exception:
                pass
        engine = getattr(self.pet, "llm_engine", None)
        if engine is not None and hasattr(engine, "abort_generation"):
            try:
                engine.abort_generation()
            except Exception:
                pass
        self.is_generating = False
        stopped_text = "（放弃思考）"
        idx = getattr(self, '_generating_idx', None)
        if idx is not None and 0 <= idx < len(self.messages):
            self.messages[idx] = ("shizuku", stopped_text)
            self.update_queue.put((idx, stopped_text))
            if self.wake_up_func:
                self.wake_up_func()
            return True
        for i in range(len(self.messages) - 1, -1, -1):
            sender, text = self.messages[i]
            if sender == "shizuku" and text == "（思考中...）":
                self.messages[i] = (sender, stopped_text)
                self.update_queue.put((i, stopped_text))
                if self.wake_up_func:
                    self.wake_up_func()
                return True
        if self.wake_up_func:
            self.wake_up_func()
        return False

    def _finish_turn(self):
        # 1. UI 暂存区清理
        uploader = getattr(self.pet, "docs_uploader", None)
        if uploader:
            try:
                uploader.on_turn_finished()
            except Exception as e:
                print(f"[ChatVM] 文件暂存区清理失败: {e}")
        # 2. 缓存池 TTL 递减
        processor = getattr(self, "msg_processor", None)
        if processor and hasattr(processor, 'cache_pool'):
            try:
                processor.cache_pool.tick()
            except Exception as e:
                print(f"[ChatVM] 缓存池 TTL 更新失败: {e}")

    def send_message_async(self, user_msg: str, file_nos=None):
        my_gen_id = self.pet.next_generation()
        self.is_generating = True

        engine = self.pet.llm_engine
        if engine and hasattr(engine, 'abort_generation'):
            engine.abort_generation()
            for i in range(len(self.messages) - 1, -1, -1):
                sender, text = self.messages[i]
                if sender == "shizuku" and text == "（思考中...）":
                    self.messages[i] = (sender, "（放弃思考）")
                    self.update_queue.put((i, "（放弃思考）"))
                    break

        self.add_message("shizuku", "（思考中...）")
        thinking_idx = len(self.messages) - 1
        self._generating_idx = thinking_idx

        def fetch_reply():
            final_reply = None

            def stream_cb(chunk):
                self.update_queue.put(('stream_chunk', thinking_idx, chunk))
                if self.wake_up_func:
                    self.wake_up_func()

            try:
                final_reply = self.msg_processor.get_reply(
                    user_msg,
                    file_nos=file_nos,
                    generation_id=my_gen_id,
                    stream_callback=stream_cb,
                )
                if not final_reply:
                    final_reply = "（模型未返回有效回复）"
            except Exception as e:
                print(f"[ChatVM] 获取回复失败: {e}")
                final_reply = "刚才处理失败了，要重试一次吗？"
            finally:
                self._finish_turn()

            if not self.pet.is_generation_current(my_gen_id):
                if self.wake_up_func:
                    self.wake_up_func()
                return

            self.is_generating = False
            self.update_queue.put((thinking_idx, final_reply))
            if self.wake_up_func:
                self.wake_up_func()

        threading.Thread(target=fetch_reply, daemon=True).start()
