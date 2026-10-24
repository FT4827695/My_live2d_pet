"""
voice_input.py
VoiceInput:      底层录音 + Vosk 识别引擎
VoiceController: 语音交互控制器
"""
import os
import sys
import json
import time
import threading
import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer


def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


# ============================================================
# 底层录音引擎
# ============================================================
class VoiceInput:
    def __init__(self, model_path=None):
        if model_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            model_path = os.path.join(base_dir, "vosk-model-small-cn-0.22")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Vosk model not found: {model_path}")
        self.model = Model(model_path)
        self.recording = False
        self.audio_data = []
        self.stream = None
        self.callback = None

    def start_recording(self, callback=None):
        if self.recording:
            return
        self.callback = callback
        self.recording = True
        self.audio_data = []

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"[VoiceInput] Audio status: {status}")
            if self.recording:
                self.audio_data.append(indata.copy())

        self.stream = sd.InputStream(
            samplerate=16000, channels=1, dtype='int16',
            blocksize=4000,
            callback=audio_callback
        )
        self.stream.start()

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        callback = self.callback
        audio_data = self.audio_data
        self.audio_data = []
        stream = self.stream
        self.stream = None
        threading.Thread(
            target=self._stop_and_recognize_async,
            args=(stream, audio_data, callback),
            daemon=True
        ).start()

    def _stop_and_recognize_async(self, stream, audio_data, callback):
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                print(f"[VoiceInput] 关闭音频流失败: {e}")

        # 2. 识别
        text = ""
        try:
            if audio_data:
                audio = np.concatenate(audio_data, axis=0)
                audio_bytes = audio.tobytes()
                recognizer = KaldiRecognizer(self.model, 16000)
                if recognizer.AcceptWaveform(audio_bytes):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").replace(" ", "")
                else:
                    partial = json.loads(recognizer.PartialResult())
                    text = partial.get("partial", "").replace(" ", "")
            else:
                print("[VoiceInput] 没有录制到音频")
        except Exception as e:
            print(f"[VoiceInput] 识别失败: {e}")
            text = ""

        if callback:
            callback(text)

    def release(self):
        self.recording = False
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.audio_data = []


# ============================================================
# 语音交互控制器（从 chat_window.py 迁入）
# ============================================================
class VoiceController:
    """
    管理长按录音的完整生命周期：
    按下 → 计时 → 触发录音 → 释放停止 → 结果注入
    通过回调解耦，不直接依赖 glfw / ChatViewModel / ChatWindow。
    """
    DEFAULT_LONG_PRESS_DELAY = 0.5  # 默认长按触发阈值（秒）

    def __init__(self, model_path=None, model_settings=None):
        self._voice_input = None
        self._model_path = model_path
        self._model_settings = model_settings
        # 状态
        self._is_recording = False
        self._recording_status_text = ""
        self._long_press_timer = None
        self._press_start_time = 0.0
        self._active = False
        # ---- 外部回调（由主窗口在创建后设置） ----
        self.on_ui_update = None       # callable(urgent=False)
        self.on_mark_dirty = None      # callable()
        self.on_add_message = None     # callable(sender, text)
        self.on_voice_text = None      # callable(text)

    # ---------- 延迟绑定 model_settings ----------
    def set_model_settings(self, model_settings) -> None:
        self._model_settings = model_settings

    # ---------- 语音配置只读属性 ----------
    @property
    def voice_enabled(self) -> bool:
        if self._model_settings is None:
            return True
        val = self._model_settings.data.get('voice_enabled', True)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ('1', 'true', 'yes', 'on')

    @property
    def voice_shortcut(self) -> str:
        if self._model_settings is None:
            return 'wheel'
        val = self._model_settings.data.get('voice_shortcut', 'wheel')
        return str(val).strip().lower()

    # ---------- 只读属性 ----------
    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def recording_status_text(self) -> str:
        return self._recording_status_text

    @property
    def press_start_time(self) -> float:
        return self._press_start_time

    # ---------- 窗口状态同步 ----------
    def set_active(self, active: bool):
        self._active = active

    # ---------- 交互入口 ----------
    def start_long_press(self, delay: float = None):
        # 全局开关关闭时，不启动任何录音流程
        if not self.voice_enabled:
            return

        if delay is None:
            delay = self.DEFAULT_LONG_PRESS_DELAY

        self._press_start_time = time.time()
        self._cancel_timer()
        self._long_press_timer = threading.Timer(
            delay, self._on_long_press_triggered
        )
        self._long_press_timer.daemon = True
        self._long_press_timer.start()

    def handle_release(self) -> bool:
        if self._is_recording:
            self._stop_recording_and_inject()
            return True
        else:
            self._cancel_timer()
            return False

    def cancel_long_press(self) -> None:
        self._cancel_timer()

    # ---------- 内部逻辑 ----------
    def _on_long_press_triggered(self):
        if not self._active or self._is_recording:
            return
        # 再次检查开关（防止计时期间被关闭）
        if not self.voice_enabled:
            return

        # 懒加载 VoiceInput
        if self._voice_input is None:
            try:
                if self._model_path is None:
                    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    self._model_path = os.path.join(base_dir, "vosk-model-small-cn-0.22")
                self._voice_input = VoiceInput(model_path=self._model_path)
            except Exception as e:
                if self.on_add_message:
                    self.on_add_message("shizuku", f"语音加载失败: {e}")
                return

        self._is_recording = True
        self._recording_status_text = "录音中..."
        # 通知 UI 刷新
        if self.on_ui_update:
            self.on_ui_update()
        if self.on_mark_dirty:
            self.on_mark_dirty()
        self._voice_input.start_recording(callback=self._on_voice_result)

    def _stop_recording_and_inject(self):
        if not self._is_recording or self._voice_input is None:
            return
        self._is_recording = False
        self._recording_status_text = "识别中..."
        if self.on_ui_update:
            self.on_ui_update()
        self._voice_input.stop_recording()

    def _on_voice_result(self, text: str):
        self._recording_status_text = ""
        if self.on_ui_update:
            self.on_ui_update()
        if text and self._active:
            if self.on_voice_text:
                self.on_voice_text(text)

    # ---------- 资源管理 ----------
    def _cancel_timer(self):
        if self._long_press_timer is not None:
            self._long_press_timer.cancel()
            self._long_press_timer = None

    def stop_and_release(self):
        self._cancel_timer()
        if self._is_recording and self._voice_input is not None:
            self._voice_input.stop_recording()
            self._is_recording = False
        if self._voice_input is not None:
            self._voice_input.release()
            self._voice_input = None
        self._recording_status_text = ""

    def cleanup(self):
        self.stop_and_release()
        self.on_ui_update = None
        self.on_mark_dirty = None
        self.on_add_message = None
        self.on_voice_text = None
