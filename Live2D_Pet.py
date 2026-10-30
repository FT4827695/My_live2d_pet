# -*- coding: utf-8 -*-

import sys
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
plugin_dir = os.path.join(base_dir, "Lib")
sys.path.insert(0, plugin_dir)

import time
import datetime
import random
import threading
import ctypes
import json
import glfw
import OpenGL.GL as gl
import pygame
import pyautogui
import glob
import texture_premultiply
import gl_direct
import opt_rotation
import live2d.v2 as live2d

from dataclasses import dataclass
from typing import Optional
from pynput.mouse import Listener as MouseListener
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import GPUtil
import win32gui, win32con, win32api
from ctypes import wintypes

from model_engine import XllamaEngine
from chat import IndependentChatWindow
from services import MessageProcessor
from prompt import SYSTEM_PROMPT, FALLBACK_REPLIES
from bubble import BubbleRenderer
from bubble_controller import BubbleController, DEFAULT_FONT_PATHS as FONT_PATHS
from hardware_monitor import HardwareMonitor
from window_manager import WindowManager
from display_config import (
    BASE_WINDOW_WIDTH,
    BASE_WINDOW_HEIGHT,
    BASE_BUBBLE_X_OFFSET,
    BASE_BUBBLE_Y_OFFSET,
    compute_display_metrics,
)
from interaction_controller import InteractionController
import setting_button

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM
)


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()

# ==================== 配置 ====================
MODEL_DIR = os.path.join(BASE_DIR, "live2d_models", "shizuku")
MODEL_JSON = os.path.join(MODEL_DIR, "shizuku.model.json")
SOUND_DIR = os.path.join(MODEL_DIR, "snd")

MOTION_DURATIONS = {
    "tap_body": 4.0,
    "shake": 4.0,
    "pinch_in": 4.0,
    "pinch_out": 4.0,
    "idle": 2.5,
    "flick_head": 3.0,
}

MESSAGES = {
    "idle": ["看什么呢？", "陪我嘛～"],
}


# ==================== 音效-文字映射 ====================
SOUND_DIALOG_MAP = {
    "flickHead_0": "有点儿无聊哎",
    "flickHead_1": "让我算算怎么回事...",
    "flickHead_2": "给你讲讲这花代表的意思",
    "pinchIn_0": "嗯...感觉怪怪的",
    "pinchIn_1": "啊咧？中彩了？",
    "pinchIn_2": "欸？肩膀...好沉啊",
    "pinchOut_0": "麦克风Ok，广播Ok，就绪",
    "pinchOut_1": "啊啊，今天是晴天呢",
    "pinchOut_2": "测试，麦克风测试，啊啊",
    "shake_0": "哇啊啊，完了完了完了",
    "shake_1": "哈哈，真有意思",
    "shake_2": "哇，吓了一跳",
    "tapBody_0": "来吧，练习练习",
    "tapBody_1": "元！气！满满！",
    "tapBody_2": "打起精神，出发！",
}


# ==================== 音效预加载 ====================
try:
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
except pygame.error as e:
    print(f"[音效] Pygame 初始化失败，音频功能将被禁用: {e}")

SOUNDS = {}
SOUND_NAME_MAP = {
    "tap_body": "tapBody",
    "flick_head": "flickHead",
    "pinch_in": "pinchIn",
    "pinch_out": "pinchOut",
    "shake": "shake"
}

for group, base in SOUND_NAME_MAP.items():
    pattern = os.path.join(SOUND_DIR, f"{base}_*.mp3")
    found_files = glob.glob(pattern)

    if not found_files:
        print(f"[音效] 目录未找到匹配 '{base}' 的音效文件")
        continue

    for path in found_files:
        try:
            filename = os.path.splitext(os.path.basename(path))[0]
            parts = filename.split("_")

            if len(parts) == 2 and parts[1].isdigit():
                key = f"{parts[0]}_{int(parts[1])}"
            else:
                key = filename

            SOUNDS[key] = pygame.mixer.Sound(path)
        except Exception as e:
            print(f"[音效] 加载失败 {path}: {e}")


def play_sound(group_name, index):
    if setting_button.is_audio_muted():
        return
    if not SOUNDS:
        return

    base = SOUND_NAME_MAP.get(group_name, group_name)
    key = f"{base}_{index}"
    snd = SOUNDS.get(key)

    if snd:
        snd.play()
    else:
        print(f"[音效] 未找到: {key}")


def get_dialog_for_sound(sound_group: str, index: int) -> str:
    prefix = SOUND_NAME_MAP.get(sound_group, sound_group)
    key = f"{prefix}_{index}"
    return SOUND_DIALOG_MAP.get(key)


@dataclass
class PendingMotion:
    group: str
    index: Optional[int] = None
    dialog_msg: Optional[str] = None
    sound_group: Optional[str] = None


# ==================== 主应用程序类 ====================
class Live2DPet:
    _json_cache = {}

    @classmethod
    def clear_json_cache(cls):
        cls._json_cache.clear()

    def __init__(self):
        self.model = None
        self.window = None
        self.window_manager = WindowManager()
        self.setting_button = setting_button.SettingButton(self)
        self.interaction = InteractionController(self)
        self.bubble_controller = BubbleController()
        self.bubble = self.bubble_controller.renderer

        self.physics_stabilized = False
        self.motion_transition_end = 0.0
        self.no_auto_expr_until = 0.0
        self.last_user_action_end_time = 0.0
        self.idle_after_user_cooldown = 5.0

        self.hardware_monitor = HardwareMonitor(alert_cooldown=30, poll_interval=10)

        self.motion_lock = False
        self.lock_end_time = 0.0
        self.pending_motion: Optional[PendingMotion] = None
        self.current_expression = "f01"
        self.next_expression_time = 0.0

        self.smooth_cursor_x = 0.0
        self.smooth_cursor_y = 0.0
        self.track_head_x = 0.0
        self.track_head_y = 0.0
        self.track_eye_x = 0.0
        self.track_eye_y = 0.0

        self.last_idle_time = 0.0
        self.idle_interval = 30.0

        self._model_json_dict = None
        self._motion_groups = {}

        # 独立聊天窗口实例
        self.chat_window = None

        self.llm_engine = None
        self.model_ready = False
        self.model_loading = False

        self._closing = False
        self._runtime_cleaned = False

        self.conversation_history = []
        self.max_history = 12
        self.system_prompt = SYSTEM_PROMPT

        # ===== 竞态防护：对话轮次管理 =====
        self._generation_id = 0
        self._gen_lock = threading.Lock()

        # ===== 文件缓存与消息处理 =====
        self.docs_uploader = None
        self.message_processor = None

        # ===== 文件上传轮询节流 =====
        self._file_poll_interval = 0.5
        self._next_file_poll_time = 0.0

        # ===== 动态帧率 =====
        self.last_interaction_time = time.time()
        self.active_fps = int(os.getenv("PET_ACTIVE_FPS", "45"))
        self.idle_fps = int(os.getenv("PET_IDLE_FPS", "33"))
        self.idle_delay = float(os.getenv("PET_IDLE_DELAY", "3.0"))
        self.last_cursor_x = None
        self.last_cursor_y = None

        # ===== 三档缩放 =====
        self.ui_scale = 1.0
        self.bubble_x_offset = BASE_BUBBLE_X_OFFSET
        self.bubble_y_offset = BASE_BUBBLE_Y_OFFSET
        self._cached_win_w = BASE_WINDOW_WIDTH
        self._cached_win_h = BASE_WINDOW_HEIGHT

        self.bubble_controller.update_metrics(
            win_width=self._cached_win_w,
            win_height=self._cached_win_h,
            x_offset=self.bubble_x_offset,
            y_offset=self.bubble_y_offset
        )
        
    # ==================== 三档分辨率计算 ====================
    def _compute_display_metrics(self):
        return compute_display_metrics()
    
    # ==================== 文件服务绑定 ====================
    def _get_active_uploader(self):
        uploader = getattr(self, "docs_uploader", None)

        if uploader is None and self.chat_window is not None:
            uploader = getattr(self.chat_window, "doc_uploader", None)

        return uploader

    def _bind_file_services(self):
        if self.docs_uploader is None and self.chat_window is not None:
            self.docs_uploader = getattr(self.chat_window, "doc_uploader", None)
        if self.chat_window is not None and getattr(self.chat_window, "vm", None) is not None:
            self.message_processor = self.chat_window.vm.msg_processor
        if self.message_processor is not None and self.docs_uploader is not None:
            if getattr(self.message_processor, "uploader", None) is not self.docs_uploader:
                self.message_processor.uploader = self.docs_uploader
        
    def _format_upload_success_message(self, attachments):
        items = []

        for att in attachments:
            filename = att.get("filename") or ""
            base_name = os.path.splitext(os.path.basename(filename))[0] or "文件"

            if len(base_name) > 4:
                short_name = base_name[:4] + "..."
            else:
                short_name = base_name

            label = att.get("spoken_label") or "文件"
            file_no = att.get("file_no", "?")

            items.append(f"{label}{short_name}（{file_no}号）")

        if not items:
            return "预处理成功啦！"

        return "、".join(items) + " 预处理成功啦！"
    
    def poll_file_upload(self):
        self._bind_file_services()
        uploader = self._get_active_uploader()
        if uploader is None:
            return

        attachments, errors = uploader.consume_results()

        if attachments:
            success_msg = self._format_upload_success_message(attachments)

            if self.chat_window and self.chat_window.active:
                self.chat_window.add_message("shizuku", success_msg)
            else:
                self.show_bubble(success_msg)

            if self.message_processor is not None:
                self.message_processor.last_exportable = None
                self.message_processor.cache_attachments(attachments)

        if errors:
            for error in errors:
                msg = error.get("reason", "文件处理失败了。")
                if self.chat_window and self.chat_window.active:
                    self.chat_window.add_message("shizuku", msg)
                else:
                    self.show_bubble(msg)
                    
    def handle_user_message(self, user_msg: str) -> str:
        if not user_msg:
            return ""

        self._bind_file_services()

        self.poll_file_upload()

        uploader = self._get_active_uploader()
        if uploader:
            uploader.on_turn_finished()

        processor = self.message_processor
        if processor is None:
            return self._fallback_reply(user_msg)

        file_nos = []
        if hasattr(processor, 'cache_pool'):
            file_nos = processor.cache_pool.resolve_file_nos(user_msg)

        reply = processor.get_reply(user_msg, file_nos=file_nos)

        if processor and hasattr(processor, 'cache_pool'):
            processor.cache_pool.tick()

        return reply or "（模型没有生成有效回复）"
    
    # ==================== 模型加载 / 卸载 ====================
    def load_llm_manually(self, use_gpu: bool, use_mmproj: bool = True):
        """供 UI 层调用的手动加载接口"""
        if self.model_loading:
            return

        if self.llm_engine and self.llm_engine.server is not None:
            self.unload_llm()

        self.model_loading = True

        def worker():
            try:
                from model_engine import XllamaEngine

                engine = XllamaEngine()

                if engine.start_server(use_gpu=use_gpu, use_mmproj=use_mmproj):
                    if getattr(self, "_closing", False):
                        try:
                            engine.stop_server()
                        except Exception:
                            pass
                        return

                    self.llm_engine = engine
                    self.model_ready = True

                    mode_desc = "显卡" if use_gpu else "内存"
                    mm_desc = "多模态" if use_mmproj else "纯文本"

                    self.queue_bubble("现在可以聊天啦！")

                    if not engine.supports_function_calling():
                        self.queue_bubble("这个脑子笨笨的...")

                    if self.chat_window and self.chat_window.active:
                        self.chat_window.add_message(
                            "shizuku",
                            f"大脑加载完毕（{mode_desc}/{mm_desc}）"
                        )
                else:
                    if getattr(self, "_closing", False):
                        return
        
                    self.model_ready = False
                    self.queue_bubble("呜...模型加载失败了。")

                    if self.chat_window and self.chat_window.active:
                        self.chat_window.add_message("shizuku", "加载失败。")

            except Exception as e:
                print(f"[主控] 模型加载异常: {e}")
                if not getattr(self, "_closing", False):
                    self.model_ready = False
            finally:
                self.model_loading = False
        
        threading.Thread(target=worker, daemon=True).start()

    def unload_llm(self):
        if self.llm_engine:
            self.llm_engine.stop_server()
            self.llm_engine = None

        self.model_ready = False
        self.model_loading = False
        self.conversation_history = []

        self._bind_file_services()

        uploader = self._get_active_uploader()
        if uploader:
            uploader.clear_all(reset_counters=True)
            uploader.reset_upload_state()

        if self.message_processor:
            self.message_processor.clear_cache()
            self.message_processor.force_clear_cache()
            
    def _shutdown_llm(self):
        self._closing = True

        engine = getattr(self, "llm_engine", None)
        if engine is not None:
            try:
                engine.abort_generation()
            except Exception:
                pass

            try:
                engine.stop_server()
            except Exception as e:
                print(f"[退出] 停止模型引擎失败: {e}")

        self.llm_engine = None
        self.model_ready = False
        self.model_loading = False
    
    def _cleanup_runtime(self):
        if getattr(self, "_runtime_cleaned", False):
            return

        self._runtime_cleaned = True

        self._shutdown_llm()

        try:
            uploader = self._get_active_uploader()
            if uploader:
                if hasattr(uploader, "shutdown"):
                    uploader.shutdown()
                else:
                    uploader.clear_all(reset_counters=True)
        except Exception as e:
            print(f"[退出] 清理上传器失败: {e}")

        try:
            if getattr(self, "message_processor", None):
                self.message_processor.clear_cache()
                self.message_processor.force_clear_cache()
        except Exception as e:
            print(f"[退出] 清理消息处理器失败: {e}")

        try:
            self.bubble_controller.cleanup()
        except Exception as e:
            print(f"[退出] 清理气泡失败: {e}")

        try:
            if self.chat_window:
                self.chat_window.cleanup()
                self.chat_window = None
        except Exception as e:
            print(f"[退出] 清理聊天窗口失败: {e}")

        try:
            if self.model:
                self.model = None
        except Exception as e:
            print(f"[退出] 释放 Live2D 模型失败: {e}")

        try:
            live2d.dispose()
        except Exception as e:
            print(f"[退出] live2d.dispose 失败: {e}")

        try:
            self.window_manager.destroy()
        except Exception as e:
            print(f"[退出] 销毁窗口失败: {e}")

        self.window = None

        try:
            self.hardware_monitor.stop()
        except Exception as e:
            print(f"[退出] 停止硬件监控失败: {e}")

        try:
            pygame.quit()
        except Exception as e:
            print(f"[退出] pygame.quit 失败: {e}")

        try:
            texture_premultiply.clear_cache()
        except Exception as e:
            print(f"[退出] 清理纹理缓存失败: {e}")

        try:
            Live2DPet.clear_json_cache()
        except Exception as e:
            print(f"[退出] 清理 JSON 缓存失败: {e}")
        
    def show_bubble(self, text, duration=None):
        self.bubble_controller.show(text, duration=duration)

    def queue_bubble(self, text):
        self.bubble_controller.queue(text)
        
    # ==================== 聊天窗口切换 ====================
    def toggle_chat_window(self, window):
        if self.chat_window is None:
            self.chat_window = IndependentChatWindow(window, self, display_scale=self.ui_scale)
            self._bind_file_services()

        if self.chat_window.active:
            self.chat_window.hide()
        else:
            self.chat_window.show()

    # ==================== 点击动作封装 ====================

    def play_head_click_motion(self):
        if self.model is None:
            return False

        if self.motion_lock:
            self.pending_motion = PendingMotion(
                group="flick_head",
                sound_group="flick_head"
            )
            return False

        if self.start_motion(
            "flick_head",
            dialog_msg=None,
            sound_group="flick_head"
        ):
            try:
                self.model.SetRandomExpression()
            except Exception as e:
                print(f"[表情] SetRandomExpression 失败，回退硬编码: {e}")
                new_expr = random.choice(["f01", "f02", "f03", "f04"])
                try:
                    self.model.SetExpression(new_expr)
                except Exception:
                    pass
            return True

        return False

    def play_body_click_motion(self):
        if self.model is None:
            return False

        if self.motion_lock:
            self.pending_motion = PendingMotion(
                group="tap_body",
                sound_group="tap_body"
            )
            return False

        return self.start_motion(
            "tap_body",
            dialog_msg=None,
            sound_group="tap_body"
        )
            
    # ==================== Live2D 部件命中 ====================
    def is_hit_part(self, x, y, keywords):
        hit_parts = self.model.HitPart(x, y, topOnly=True)

        if not hit_parts:
            return None, False

        part_id = hit_parts[0]
        part_upper = part_id.upper()

        for kw in keywords:
            if kw.upper() in part_upper:
                return part_id, True

        return part_id, False

    # ==================== model.json 缓存 ====================
    def load_model_json(self):
        if self._model_json_dict is not None:
            return self._model_json_dict

        cache_key = MODEL_JSON

        if cache_key in Live2DPet._json_cache:
            self._model_json_dict = Live2DPet._json_cache[cache_key]
            print("[缓存] 使用缓存的 model.json")

            if "motions" in self._model_json_dict:
                for group, motions in self._model_json_dict["motions"].items():
                    self._motion_groups[group] = list(range(len(motions)))

            return self._model_json_dict

        if os.path.exists(MODEL_JSON):
            with open(MODEL_JSON, "r", encoding="utf-8") as f:
                self._model_json_dict = json.load(f)

            Live2DPet._json_cache[cache_key] = self._model_json_dict

            if "motions" in self._model_json_dict:
                for group, motions in self._model_json_dict["motions"].items():
                    self._motion_groups[group] = list(range(len(motions)))

            print("[缓存] 已缓存 model.json")

        return self._model_json_dict

    def get_valid_motion_index(self, group):
        indices = self._motion_groups.get(group)

        if indices:
            return random.choice(indices)

        return None

    # ==================== 动作 ====================
    def _on_finish_motion(self, group, no):
        """动作完成回调 - 精确释放锁"""
        if not self.motion_lock:
            return

        print(f"[动作] 完成: {group}_{no}")

        self.motion_lock = False
        self.last_user_action_end_time = time.time()
        self.motion_transition_end = time.time() + 0.3

        if self.pending_motion is not None:
            pm = self.pending_motion
            self.pending_motion = None

            self.start_motion(
                pm.group,
                index=pm.index,
                dialog_msg=pm.dialog_msg,
                sound_group=pm.sound_group
            )

    def start_motion(self, group, index=None, dialog_msg=None, sound_group=None, lock_duration=None):
        if self.motion_lock:
            return False

        if index is None:
            index = self.get_valid_motion_index(group)

            if index is None:
                print(f"[动作] 跳过 {group}: 无有效动作")
                return False

        try:
            self.model.StartMotion(
                group,
                index,
                priority=3,
                onFinishMotionHandler=self._on_finish_motion
            )

            duration = lock_duration if lock_duration else MOTION_DURATIONS.get(group, 3.0)
            self.lock_end_time = time.time() + duration + 1.0
            self.motion_lock = True
            self.last_interaction_time = time.time()

            if sound_group:
                play_sound(sound_group, index)
                self.no_auto_expr_until = time.time() + duration + 0.5

            final_msg = dialog_msg

            if final_msg is None and sound_group is not None and index is not None:
                final_msg = get_dialog_for_sound(sound_group, index)

            if final_msg is None and group in MESSAGES:
                final_msg = random.choice(MESSAGES[group])

            if final_msg and self.window:
                chat_open = self.chat_window is not None and self.chat_window.active
                if not (chat_open and group == "idle"):
                    self.show_bubble(final_msg)
                
            return True

        except Exception as e:
            print(f"[动作] 异常 ({group}/{index}): {e}")
            return False

    # ==================== GLFW 回调 ====================
    def on_key(self, window, key, scancode, action, mods):
        if key == glfw.KEY_ESCAPE and action == glfw.PRESS:
            glfw.set_window_should_close(window, True)

    def on_mouse_scroll(self, window, xoffset, yoffset):
        pass

    def on_char(self, window, codepoint):
        pass

    # ==================== 窗口 ====================
    def init_glfw_window(self):
        if not self.window_manager.ensure_glfw_initialized():
            return False

        # ===== 启动时计算三档缩放 =====
        (
            self.ui_scale,
            target_w,
            target_h,
            self.bubble_x_offset,
            self.bubble_y_offset
        ) = self._compute_display_metrics()

        self._cached_win_w = target_w
        self._cached_win_h = target_h

        print(
            f"[分辨率] scale={self.ui_scale:.3f}, "
            f"window={target_w}x{target_h}, "
            f"bubble_offset=({self.bubble_x_offset}, {self.bubble_y_offset})"
        )

        ok = self.window_manager.init_window(
            width=target_w,
            height=target_h,
            title="Live2D Pet",
            on_mouse_move=self.interaction.on_mouse_move,
            on_mouse_button=self.interaction.on_mouse_button,
            on_key=self.on_key,
            on_scroll=self.on_mouse_scroll,
            on_char=self.on_char,
            on_window_resize=self._on_window_resize,
        )
        
        if not ok:
            return False

        self.window = self.window_manager.window
        self._cached_win_w, self._cached_win_h = self.window_manager.get_window_size()

        self.bubble_controller.update_metrics(
            win_width=self._cached_win_w,
            win_height=self._cached_win_h,
            x_offset=self.bubble_x_offset,
            y_offset=self.bubble_y_offset
        )

        self.setting_button.set_scale(
            self.ui_scale, self._cached_win_w, self._cached_win_h
        )

        return True
    
    def _on_window_resize(self, window, width, height):
        self._cached_win_w = width
        self._cached_win_h = height

        self.bubble_controller.update_metrics(
            win_width=width,
            win_height=height
        )

        self.setting_button.set_scale(self.ui_scale, width, height)

        if self.model:
            self.model.Resize(width, height)
            
    def load_model(self):
        self.model = live2d.LAppModel()

        try:
            self.model.LoadModelJson(MODEL_JSON)
            print("[Live2DPet] 模型加载完成")

            physics_path = os.path.join(MODEL_DIR, "shizuku.physics.json")

            if os.path.exists(physics_path):
                self.model.loadPhysics(physics_path)
                print("[物理] 已加载物理效果")
            else:
                print("[物理] 未找到 physics.json")

            self.load_model_json()

            # 预乘所有纹理（使用缓存）
            texture_premultiply.premultiply_model_textures(
                self.model,
                self._model_json_dict,
                MODEL_DIR,
                force_reload=False
            )

            # ===== 使用当前缩放后的窗口尺寸 =====
            self.model.Resize(self._cached_win_w, self._cached_win_h)

            # ===== SDK 自动呼吸/眨眼 =====
            try:
                self.model.SetAutoBlinkEnable(True)
                self.model.SetAutoBreathEnable(True)
                print("[呼吸/眨眼] SDK 自动模式已开启")
            except Exception as e:
                print(f"[呼吸/眨眼] 启用失败（可能 SDK 版本不支持）: {e}")

        except Exception as e:
            print(f"[Live2DPet] 模型加载失败: {e}")
            import traceback
            traceback.print_exc()
            return False

        return True

    # ==================== 参数平滑 ====================
    def update_parameters_smooth(self):
        win_w = self._cached_win_w
        win_h = self._cached_win_h

        if win_w > 0 and win_h > 0:
            sx, sy = pyautogui.position()

            if self.last_cursor_x is not None:
                if abs(sx - self.last_cursor_x) > 2 or abs(sy - self.last_cursor_y) > 2:
                    self.last_interaction_time = time.time()

            self.last_cursor_x, self.last_cursor_y = sx, sy

            wx, wy = glfw.get_window_pos(self.window)

            raw_rx, raw_ry = sx - wx, sy - wy
            self.cursor_rel_raw = (raw_rx, raw_ry)

            rel_x = max(0, min(win_w, sx - wx))
            rel_y = max(0, min(win_h, sy - wy))

            target_x = (rel_x / win_w) * 6.0 - 3.0
            target_y = ((win_h - rel_y) / win_h) * 6.0 - 3.0

            self.smooth_cursor_x += (target_x - self.smooth_cursor_x) * 0.20
            self.smooth_cursor_y += (target_y - self.smooth_cursor_y) * 0.15

            h_dx = self.smooth_cursor_x - self.track_head_x
            h_dy = self.smooth_cursor_y - self.track_head_y
            e_dx = self.smooth_cursor_x - self.track_eye_x
            e_dy = self.smooth_cursor_y - self.track_eye_y

            head_f = 0.35
            eye_f = 0.20

            self.track_head_x += h_dx * head_f
            self.track_head_y += h_dy * head_f * 0.9
            self.track_eye_x += e_dx * eye_f
            self.track_eye_y += e_dy * eye_f * 0.8

            if not hasattr(self, "track_head_z"):
                self.track_head_z = 0.0

            self.track_head_z += (self.track_head_y * 0.4 - self.track_head_z) * 0.2

            try:
                # ----- 头部角度 -----
                self.model.SetParameterValue("PARAM_ANGLE_X", self.track_head_x * 4.0)
                self.model.SetParameterValue("PARAM_ANGLE_Y", self.track_head_y * 3.5)
                self.model.SetParameterValue("PARAM_ANGLE_Z", self.track_head_z * 2.5)

                # ----- 眼球（快速跟随） -----
                self.model.SetParameterValue("PARAM_EYE_BALL_X", self.track_eye_x * 0.40)
                self.model.SetParameterValue("PARAM_EYE_BALL_Y", self.track_eye_y * 0.35)

                # ----- 身体角度 -----
                self.model.SetParameterValue("PARAM_BODY_ANGLE_X", self.track_head_x * 1.2)
                self.model.SetParameterValue("PARAM_BODY_ANGLE_Z", self.track_head_y * 0.8)

            except Exception as e:
                print(f"[参数设置异常] {e}", flush=True)

    # ==================== 主循环 ====================
    def run(self):
        if not self.init_glfw_window():
            return

        try:
            if not self.load_model():
                return

            self.motion_transition_end = 0.0
            self.hardware_monitor.start()
            self.bubble_controller.init()

            now = time.time()
            self.last_idle_time = now
            self.idle_interval = random.uniform(30, 60)
            self.next_expression_time = now + random.uniform(6, 30)

            listener_thread = threading.Thread(
                target=self.interaction.start_global_listener,
                daemon=True
            )
            listener_thread.start()
            print("[全局滚轮] 监听已启动")

            frame_start = time.perf_counter()

            while not glfw.window_should_close(self.window):
                glfw.poll_events()

                # ===== 轮询文件上传结果 =====
                if self.chat_window is not None or getattr(self, "docs_uploader", None) is not None:
                    current_poll_time = time.time()
                    if current_poll_time >= self._next_file_poll_time:
                        self.poll_file_upload()
                        self._next_file_poll_time = current_poll_time + self._file_poll_interval
        
                live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)

                now = time.time()

                # 自动表情切换（空闲时）
                if (
                    not self.motion_lock
                    and now >= self.motion_transition_end
                    and now >= self.next_expression_time
                    and now >= self.no_auto_expr_until
                ):
                    try:
                        self.model.SetRandomExpression()
                    except Exception:
                        new_expr = random.choice(["f01", "f02", "f03", "f04"])
                        try:
                            self.model.SetExpression(new_expr)
                        except Exception:
                            pass
                    self.next_expression_time = now + random.uniform(6, 30)

                # 硬件报警气泡
                alert_msg = self.hardware_monitor.take_alert_message()
                if alert_msg:
                    self.show_bubble(alert_msg, duration=4.0)
            
                self.bubble_controller.process_pending()

                if self.motion_lock and time.time() > self.lock_end_time:
                    self.motion_lock = False
                    self.last_user_action_end_time = time.time()
                    self.motion_transition_end = time.time() + 0.5

                    if self.pending_motion is not None:
                        pm = self.pending_motion
                        self.pending_motion = None
                        self.start_motion(
                            pm.group,
                            index=pm.index,
                            dialog_msg=pm.dialog_msg,
                            sound_group=pm.sound_group
                        )

                scroll_direction = self.interaction.consume_pending_scroll_action()
                if scroll_direction is not None:
                    if not self.motion_lock:
                        grp = "pinch_in" if scroll_direction > 0 else "pinch_out"
                        self.start_motion(grp, dialog_msg=None, sound_group=grp)

                if (
                    not self.motion_lock
                    and now - self.last_idle_time >= self.idle_interval
                    and now - self.last_user_action_end_time >= self.idle_after_user_cooldown
               ):
                    msg = random.choice(MESSAGES["idle"]) if random.random() < 0.3 else None
                    if self.start_motion("idle", dialog_msg=msg):
                        self.last_idle_time = now
                        self.idle_interval = random.uniform(30, 60)

                self.update_parameters_smooth()

                # 绘制主窗口模型
                self.model.Update()
                self.model.Draw()

                gl.glUseProgram(0)
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

                self.bubble_controller.draw()

                self.setting_button.tick_and_draw(
                    now, self._cached_win_w, self._cached_win_h,
                    cursor_rel=getattr(self, "cursor_rel_raw", None),
                )

                # 绘制独立聊天窗口
                if self.chat_window and self.chat_window.active:
                    self.chat_window.update_position()
                    current_time = time.time()
                    self.chat_window.update_cursor_blink(current_time)
                    self.chat_window.tick()

                    if self.chat_window.is_dirty():
                        glfw.make_context_current(self.chat_window.window)
                        rendered = self.chat_window.draw()
                        if rendered:
                            glfw.swap_buffers(self.chat_window.window)
                        glfw.make_context_current(self.window)

                glfw.swap_buffers(self.window)

                active = (
                    self.motion_lock or
                    self.interaction.is_dragging or
                    (self.chat_window is not None and self.chat_window.active) or
                    time.time() - self.last_interaction_time < self.idle_delay
                )

                desired_fps = self.active_fps if active else self.idle_fps
                frame_time = 1.0 / desired_fps
                elapsed = time.perf_counter() - frame_start

                if elapsed < frame_time:
                    glfw.wait_events_timeout(frame_time - elapsed)
                    remaining = frame_time - (time.perf_counter() - frame_start)
                    if remaining > 0.002:
                        time.sleep(remaining)

                frame_start = time.perf_counter()

        finally:
            self._cleanup_runtime()
        
    # ==================== 竞态防护 ====================
    def next_generation(self):
        with self._gen_lock:
            self._generation_id += 1
            return self._generation_id

    def is_generation_current(self, gen_id):
        with self._gen_lock:
            return gen_id == self._generation_id

    def _fallback_reply(self, user_message):
        return random.choice(FALLBACK_REPLIES)


if __name__ == "__main__":
    pet = Live2DPet()
    pet.run()
