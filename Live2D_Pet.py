# -*- coding: utf-8 -*-
import sys
import os

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

# ============================================================================
# 路径设置
# ============================================================================
base_dir = os.path.dirname(os.path.abspath(__file__))
plugin_dir = os.path.join(base_dir, "Lib")
sys.path.insert(0, plugin_dir)

if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))

import time
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

from model_registry import ModelRegistry, ModelContext
from settings_store import get_settings

from dataclasses import dataclass
from typing import Optional
import win32gui, win32con
from ctypes import wintypes

from model_engine import XllamaEngine
from chat import IndependentChatWindow
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
from background_renderer import BackgroundRenderer
from llm_manager import LlmManager
from conversation_store import ConversationStore
from agent_service import AgentService
import setting_button

def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()

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

        # === 模型注册表与当前上下文 ===
        self.registry = ModelRegistry()
        self.models = self.registry.scan()
        try:
            _preferred = get_settings().get("model.current", "")
        except Exception:
            _preferred = ""
        self.current_context = self.registry.resolve(preferred_id=_preferred or None)
        print(f"[模型] 当前模型: {self.current_context.model_id if self.current_context else '无'}")
        
        # === 音效实例变量 ===
        self.sounds = {}
        self._audio_ready = False
        
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
        self.background = BackgroundRenderer(self)

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

        self.llm_manager = LlmManager(
            on_loaded=self._on_llm_loaded,
            on_load_failed=self._on_llm_load_failed,
        )
        self.agent = AgentService(
            llm_manager=self.llm_manager,
            on_bubble=self.queue_bubble,
            notify_user=self._notify_user,
        )
        
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
    
    # ==================== 文件 / 消息（转发到 AgentService） ====================
    def _get_active_uploader(self):
        return self.agent.docs_uploader

    def _bind_file_services(self):
        if self.chat_window is not None:
            self.agent.bind_chat_window(self.chat_window)

    def poll_file_upload(self):
        self.agent.poll()

    def handle_user_message(self, user_msg: str) -> str:
        return self.agent.handle_user_message(user_msg)
    
    # ==================== 兼容层（供 chat_window / menu_manager 使用） ====================
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
    def conversation_store(self):
        return self.agent.conversation_store

    @property
    def system_prompt(self):
        return self.agent.system_prompt

    @system_prompt.setter
    def system_prompt(self, value):
        self.agent.system_prompt = value

    @property
    def conversation_history(self):
        return self.agent.conversation_history

    @conversation_history.setter
    def conversation_history(self, value):
        self.agent.conversation_history = value

    @property
    def max_history(self):
        return self.agent.max_history

    @property
    def message_processor(self):
        return self.agent.message_processor

    @message_processor.setter
    def message_processor(self, value):
        self.agent.message_processor = value

    @property
    def docs_uploader(self):
        return self.agent.docs_uploader

    @docs_uploader.setter
    def docs_uploader(self, value):
        self.agent.docs_uploader = value
        if value is not None and self.agent.message_processor is not None:
            try:
                if getattr(self.agent.message_processor, "uploader", None) is not value:
                    self.agent.message_processor.uploader = value
            except Exception:
                pass
            
    # ==================== 音效初始化与播放 ====================
    def init_audio(self):            
        if self._audio_ready:
            return
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        except pygame.error as e:
            print(f"[音效] Pygame 初始化失败，音频功能将被禁用: {e}")
            return
            
        ctx = self.current_context
        if not ctx or not ctx.sound_dir or not ctx.sound_map:
            print("[音效] 当前模型无音效配置")
            return

        for semantic, base in ctx.sound_map.items():
            pattern = os.path.join(ctx.sound_dir, f"{base}_*.mp3")
            found_files = glob.glob(pattern)
            if not found_files:
                continue
            for path in found_files:
                try:
                    filename = os.path.splitext(os.path.basename(path))[0]
                    parts = filename.split("_")
                    if len(parts) == 2 and parts[1].isdigit():
                        key = f"{parts[0]}_{int(parts[1])}"
                    else:
                        key = filename
                    self.sounds[key] = pygame.mixer.Sound(path)
                except Exception as e:
                    print(f"[音效] 加载失败 {path}: {e}")
        self._audio_ready = True
        print(f"[音效] 已加载 {len(self.sounds)} 个音效")

    def play_sound(self, semantic_group, index):
        if not self._audio_ready or setting_button.is_audio_muted() or not self.sounds:
            return
        ctx = self.current_context
        if not ctx: return
        base = ctx.sound_map.get(semantic_group, semantic_group)
        key = f"{base}_{index}"
        snd = self.sounds.get(key)
        if snd:
            snd.play()

    def get_dialog_for_sound(self, semantic_group: str, index: int):
        ctx = self.current_context
        if not ctx: return None
        base = ctx.sound_map.get(semantic_group, semantic_group)
        key = f"{base}_{index}"
        return ctx.sound_dialog.get(key)

    def next_generation(self) -> int:
        return self.agent.next_generation()

    def is_generation_current(self, gen_id) -> bool:
        return self.agent.is_generation_current(gen_id)

    def _fallback_reply(self, user_msg) -> str:
        return self.agent._fallback_reply(user_msg)

    def remember_current_model(self):
        if self.current_context is None:
            return
        try:
            get_settings().set("model.current", self.current_context.model_id)
        except Exception as e:
            print(f"[模型] 保存当前模型失败: {e}")


    # ==================== 模型热切换 ====================
    def switch_model(self, target_model_id):
        if target_model_id is None:
            return False
        ctx = self.registry.get(target_model_id)
        if ctx is None:
            print(f"[模型切换] 未找到模型: {target_model_id}")
            return False
        if self.current_context and ctx.model_id == self.current_context.model_id:
            return False

        print(f"[模型切换] >>> {ctx.model_id}")

        # ---- 卸载旧模型 ----
        self._unload_current_model()

        # ---- 加载新模型 ----
        if not self.load_model(ctx):
            print(f"[模型切换] 新模型加载失败！")
            return False

        # ---- 记忆模型 ----
        self.remember_current_model()

        print(f"[模型切换] <<< 完成")
        return True

    def _unload_current_model(self):
        # 1. 删除旧模型的预乘纹理
        if self.model is not None:
            try:
                if hasattr(self.model, 'live2DModel') and hasattr(self.model.live2DModel, 'getDrawParam'):
                    draw_param = self.model.live2DModel.getDrawParam()
                    if draw_param is not None and hasattr(draw_param, 'textures'):
                        for tex_id in draw_param.textures:
                            if tex_id is not None and tex_id != 0:
                                try:
                                    gl.glDeleteTextures([tex_id])
                                except Exception:
                                    pass
            except Exception as e:
                print(f"[模型卸载] 删除纹理异常: {e}")

        # 2. 清空纹理预乘缓存记录
        try:
            texture_premultiply.clear_cache()
        except Exception:
            pass

        # 3. 释放模型对象
        self.model = None

        # 4. 重置运行时状态
        self.motion_lock = False
        self.lock_end_time = 0.0
        self.pending_motion = None
        self._model_json_dict = None
        self._motion_groups = {}
        self.physics_stabilized = False
        self.motion_transition_end = 0.0
        self.no_auto_expr_until = 0.0
        self.current_expression = "f01"

    def _reload_audio(self):
        self.sounds.clear()
        self._audio_ready = False
        self.init_audio()

    def _apply_character(self, ctx):
        from prompt import (set_model_character, clear_character_cache,
                            _load_character_data, get_character_name)
        from model_registry import resolve_character

        character_exists = False
        try:
            character_exists = os.path.exists(get_character_file_path())
        except Exception:
            character_exists = False

        if character_exists:
            clear_model_character()
            clear_character_cache()
        else:
            global_data = _load_character_data()
            eff_name, eff_desc = resolve_character(
                ctx,
                global_name=global_data.get("name"),
                global_desc=global_data.get("description"),
            )
            set_model_character(eff_name, eff_desc)
            clear_character_cache()

        if self.chat_window is not None:
            try:
                self.chat_window._refresh_character_ui()
            except Exception as e:
                print(f"[模型切换] 刷新角色名失败: {e}")

    def _on_llm_loaded(self, engine, use_gpu: bool, use_mmproj: bool):
        mode_desc = "显卡" if use_gpu else "内存"
        mm_desc = "多模态" if use_mmproj else "纯文本"

        try:
            from prompt import get_agent_system_prompt, clear_character_cache
            clear_character_cache()
            # 读取当前模式参数
            thinking_mode = os.environ.get("XLLAMA_ENABLE_THINKING", "0") == "1"
            web_search_enabled = False
            try:
                from internet_settings import InternetSettings
                web_search_enabled = InternetSettings.get_instance().is_enabled
            except Exception:
                pass
            supports_tool = False
            try:
                supports_tool = engine.supports_function_calling()
            except Exception:
                pass
            self.system_prompt = get_agent_system_prompt(
                include_tool_rules=supports_tool,
                thinking_mode=thinking_mode,
                web_search_enabled=web_search_enabled,
            )
            print("[LLM] system_prompt 已按当前人设重新生成")
        except Exception as e:
            print(f"[LLM] 重新生成 system_prompt 失败: {e}")

        self.queue_bubble("现在可以聊天啦！")
        try:
            if not engine.supports_function_calling():
                self.queue_bubble("这个脑子笨笨的...")
        except Exception:
            pass
        if self.chat_window and self.chat_window.active:
            from prompt import get_character_name
            self.chat_window.add_message(
                get_character_name(), f"大脑加载完毕（{mode_desc}/{mm_desc}）"
            )
            
    def _on_llm_load_failed(self, error_msg: str):
        self.queue_bubble("呜...模型加载失败了。")
        if self.chat_window and self.chat_window.active:
            from prompt import get_character_name
            self.chat_window.add_message(get_character_name(), "加载失败。")

    def _notify_user(self, text: str):
        if self.chat_window is not None and getattr(self.chat_window, "active", False):
            try:
                from prompt import get_character_name
                self.chat_window.add_message(get_character_name(), text)
                return
            except Exception:
                pass
        self.show_bubble(text)
        
    def load_llm_manually(self, use_gpu: bool, use_mmproj: bool = True):
        """供 UI 层调用的手动加载接口。"""
        if self.llm_manager.is_loading:
            return

        if self.llm_manager.engine is not None:
            self.unload_llm()

        self.llm_manager.load(use_gpu=use_gpu, use_mmproj=use_mmproj)

    def unload_llm(self):
        self.llm_manager.unload()
        self.agent.clear_for_reload()
        
    def _shutdown_llm(self):
        self._closing = True
        self.llm_manager.shutdown()
        
    def _cleanup_runtime(self):
        if getattr(self, "_runtime_cleaned", False):
            return
        self._runtime_cleaned = True

        self._shutdown_llm()

        try:
            self.agent.shutdown()
        except Exception as e:
            print(f"[退出] AgentService 清理失败: {e}")

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
            if getattr(self, "background", None) is not None:
                self.background.destroy()
        except Exception as e:
            print(f"[退出] 清理背景失败: {e}")

        try:
            if getattr(self, "setting_button", None) is not None:
                self.setting_button.destroy()
        except Exception as e:
            print(f"[退出] 清理按钮组失败: {e}")

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
            self.agent.bind_chat_window(self.chat_window)

        if self.chat_window.active:
            self.chat_window.hide()
        else:
            self.chat_window.show()
            
    # ==================== 点击动作封装 ====================
    def play_head_click_motion(self):
        if self.model is None: return False
        ctx = self.current_context
        group = ctx.get_semantic_group("tap_head") if ctx else "flick_head"
        
        if self.motion_lock:
            self.pending_motion = PendingMotion(group=group, sound_group="tap_head")
            return False
            
        if self.start_motion("tap_head", dialog_msg=None, sound_group="tap_head"):
            try:
                self.model.SetRandomExpression()
            except Exception:
                # 表情兜底
                exprs = ctx.expressions if ctx and ctx.expressions else ["f01", "f02", "f03", "f04"]
                if exprs:
                    try: self.model.SetExpression(random.choice(exprs))
                    except: pass
            return True
        return False

    def play_body_click_motion(self):
        if self.model is None: return False
        ctx = self.current_context
        group = ctx.get_semantic_group("tap_body") if ctx else "tap_body"
        
        if self.motion_lock:
            self.pending_motion = PendingMotion(group=group, sound_group="tap_body")
            return False
        return self.start_motion("tap_body", dialog_msg=None, sound_group="tap_body")
    
    # ==================== Live2D 部件命中 ====================
    def is_hit_part(self, x, y, keywords):
        hit_parts = self.model.HitPart(x, y, topOnly=False)
        if not hit_parts:
            return None, False
        for part_id in hit_parts:
            part_upper = part_id.upper()
            for kw in keywords:
                if kw.upper() in part_upper:
                    return part_id, True
        return hit_parts[0], False

    # ==================== model.json 缓存 ====================
    def load_model_json(self):
        if self._model_json_dict is not None:
            return self._model_json_dict
        ctx = self.current_context
        cache_key = ctx.model_json_path if ctx else ""
        
        if cache_key in Live2DPet._json_cache:
            self._model_json_dict = Live2DPet._json_cache[cache_key]
            if "motions" in self._model_json_dict:
                for group, motions in self._model_json_dict["motions"].items():
                    self._motion_groups[group] = list(range(len(motions)))
            return self._model_json_dict
            
        if ctx and os.path.exists(ctx.model_json_path):
            with open(ctx.model_json_path, "r", encoding="utf-8") as f:
                self._model_json_dict = json.load(f)
            Live2DPet._json_cache[cache_key] = self._model_json_dict
            if "motions" in self._model_json_dict:
                for group, motions in self._model_json_dict["motions"].items():
                    self._motion_groups[group] = list(range(len(motions)))
        return self._model_json_dict
    
    def get_valid_motion_index(self, group):
        indices = self._motion_groups.get(group)

        if indices:
            return random.choice(indices)

        return None

    # ==================== 动作 ====================
    def _on_finish_motion(self, group, no):
        if not self.motion_lock:
            return

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

    def start_motion(self, semantic_group, index=None, dialog_msg=None, sound_group=None, lock_duration=None):
        if self.motion_lock: return False
        ctx = self.current_context
        
        # 语义翻译
        actual_group = ctx.get_semantic_group(semantic_group) if ctx else semantic_group
        
        if index is None:
            index = self.get_valid_motion_index(actual_group)
            if index is None: return False
            
        try:
            self.model.StartMotion(actual_group, index, priority=3, onFinishMotionHandler=self._on_finish_motion)
            
            # 从 context 获取时长
            duration = lock_duration if lock_duration else (ctx.get_motion_duration(actual_group) if ctx else 3.0)
            self.lock_end_time = time.time() + duration + 1.0
            self.motion_lock = True
            self.last_interaction_time = time.time()
            
            if sound_group:
                self.play_sound(sound_group, index)
                self.no_auto_expr_until = time.time() + duration + 0.5
                
            final_msg = dialog_msg
            if final_msg is None and sound_group is not None and index is not None:
                final_msg = self.get_dialog_for_sound(sound_group, index)
            if final_msg is None and ctx:
                # 尝试从 context.messages 获取
                msgs = ctx.get_messages(semantic_group) 
                if msgs: final_msg = random.choice(msgs)
                
            if final_msg and self.window:
                chat_open = self.chat_window is not None and self.chat_window.active
                if not (chat_open and semantic_group == "idle"):
                    suppress_idle = False
                    if semantic_group == "idle":
                        try:
                            suppress_idle = self.setting_button.is_tip_active()
                        except Exception:
                            suppress_idle = False
                    if not suppress_idle:
                        self.show_bubble(final_msg)
            return True
        except Exception as e:
            print(f"[动作] 异常 ({actual_group}/{index}): {e}")
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

        self.setting_button.set_scale(self.ui_scale)

        return True
    
    def _on_window_resize(self, window, width, height):
        self._cached_win_w = width
        self._cached_win_h = height

        self.bubble_controller.update_metrics(
            win_width=width,
            win_height=height
        )

        self.setting_button.set_scale(self.ui_scale)

        if self.model:
            self.model.Resize(width, height)
            
    def load_model(self, context=None):
        if context is None:
            context = self.current_context
        if context is None:
            print("[Live2DPet] 无可用模型")
            return False
            
        self.current_context = context
        self.model = live2d.LAppModel()
        try:
            self.model.LoadModelJson(context.model_json_path)
            print(f"[Live2DPet] 模型 {context.model_id} 加载完成")
            
            # 物理文件（从 context 获取）
            if context.physics_path and os.path.exists(context.physics_path):
                self.model.loadPhysics(context.physics_path)
                print("[物理] 已加载物理效果")
                
            self.load_model_json()
            
            # 预乘纹理
            texture_premultiply.premultiply_model_textures(
                self.model, self._model_json_dict, context.model_dir, force_reload=False
            )
            
            self.model.Resize(self._cached_win_w, self._cached_win_h)
            try:
                self.model.SetAutoBlinkEnable(True)
                self.model.SetAutoBreathEnable(True)
            except Exception as e:
                print(f"[呼吸/眨眼] 启用失败: {e}")
        except Exception as e:
            print(f"[Live2DPet] 模型加载失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        self._apply_character(context)
        self._reload_audio()
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
            self.background.init()
            setting_button.enforce_mixer_state()

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

                self.agent.poll()
        
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
                        ctx = self.current_context
                        exprs = ctx.expressions if ctx and ctx.expressions else ["f01", "f02", "f03", "f04"]
                        if exprs:
                            try: self.model.SetExpression(random.choice(exprs))
                            except: pass
                        
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
                    ctx = self.current_context
                    msgs = ctx.get_messages("idle") if ctx else []
                    msg = random.choice(msgs) if msgs and random.random() < 0.3 else None
                    if self.start_motion("idle", dialog_msg=msg):
                        self.last_idle_time = now
                        self.idle_interval = random.uniform(30, 60)

                self.update_parameters_smooth()

                chat_active = bool(
                    self.chat_window is not None and getattr(self.chat_window, "active", False)
                )
                self.background.tick_and_draw(now, self._cached_win_w, self._cached_win_h, chat_active)

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

                active = (
                    self.motion_lock or
                    self.interaction.is_dragging or
                    (self.chat_window is not None and self.chat_window.active) or
                    time.time() - self.last_interaction_time < self.idle_delay
                )

                desired_fps = self.active_fps if active else self.idle_fps
                frame_time = 1.0 / desired_fps

                deadline = frame_start + frame_time
                while True:
                    now = time.perf_counter()
                    if now >= deadline:
                        break
                    remain = deadline - now
                    if remain > 0.001:
                        time.sleep(remain - 0.0005)
                    else:
                        time.sleep(0)
                    if deadline - time.perf_counter() > 0.002:
                        glfw.poll_events()

                frame_start = time.perf_counter()

        finally:
            self._cleanup_runtime()

if __name__ == "__main__":
    pet = Live2DPet()
    pet.run()
