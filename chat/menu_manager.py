import os
import sys
import json
import time
import threading
import glfw
from typing import Any, Dict, List, Optional, Tuple, Callable
from services import ClipboardService


# ============================================================
# 可循环切换的参数选项
# ============================================================
CTX_OPTIONS = [
    4096,
    8192,
    16384,
    24576,
    49152,
    122880
]
KV_CACHE_OPTIONS = [
    "f16",
    "q8_0",
    "iq4_nl",
    "q4_0",
    "q8_iq4", 
]
TEMPERATURE_OPTIONS = [
    0.0,
    0.3,
    0.5,
    0.7,
    1.0,
]
VRAM_EXTRA_OPTIONS = [
    -512,
    -256,
    0,
    512,
    768,
    1024
]
MTP_OPTIONS = [
    0,
    1,
    2,
    4,
]


def _get_root_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_model_settings_path() -> str:
    return os.path.join(_get_root_dir(), "config", "model_settings.json")


def _get_character_file_path() -> str:
    return os.path.join(_get_root_dir(), "config", "character.json")

class ModelSettings:
    DEFAULTS: Dict[str, Any] = {
        "ctx": 8192,
        "kv_cache": "q8_0",
        "temperature": 0.7,
        "vram_extra_mb": 0,
        "enable_thinking": False,
        "mtp": 0,
        "voice_enabled": True,       # 全局语音开关
        "voice_shortcut": "wheel",   # 录音快捷键（'wheel' 或 'off'）
        "mixed_kv_ratio": 0.99,
    }

    def __init__(self, path: Optional[str] = None):
        self.path = path or _get_model_settings_path()
        self.data: Dict[str, Any] = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self.data.update(saved)
        except Exception as e:
            print(f"[MenuManager] 读取模型参数失败: {e}")
            self.data = dict(self.DEFAULTS)

    def save(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[MenuManager] 保存模型参数失败: {e}")

    def apply_to_env(self) -> None:
        try:
            ctx = int(self.data.get("ctx", self.DEFAULTS["ctx"]))
        except Exception:
            ctx = self.DEFAULTS["ctx"]
        kv_cache = str(
            self.data.get("kv_cache", self.DEFAULTS["kv_cache"])
        ).strip().lower()
        try:
            temperature = float(
                self.data.get("temperature", self.DEFAULTS["temperature"])
            )
        except Exception:
            temperature = self.DEFAULTS["temperature"]
        try:
            vram_extra_mb = int(
                self.data.get("vram_extra_mb", self.DEFAULTS["vram_extra_mb"])
            )
        except Exception:
            vram_extra_mb = self.DEFAULTS["vram_extra_mb"]

        os.environ["XLLAMA_CTX"] = str(ctx)
        os.environ["XLLAMA_KV_CACHE"] = kv_cache
        os.environ["XLLAMA_TEMPERATURE"] = str(temperature)
        os.environ["XLLAMA_VRAM_EXTRA_MB"] = str(vram_extra_mb)

        enable_thinking_raw = self.data.get(
            "enable_thinking",
            self.DEFAULTS["enable_thinking"],
        )
        if isinstance(enable_thinking_raw, bool):
            enable_thinking = enable_thinking_raw
        else:
            enable_thinking = str(enable_thinking_raw).strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        os.environ["XLLAMA_ENABLE_THINKING"] = "1" if enable_thinking else "0"

        try:
            mtp = int(self.data.get("mtp", self.DEFAULTS["mtp"]))
        except Exception:
            mtp = self.DEFAULTS["mtp"]
        if mtp > 0:
            os.environ["XLLAMA_SPEC_MODE"] = "mtp"
            os.environ["XLLAMA_SPEC_DRAFT"] = str(mtp)
        else:
            os.environ["XLLAMA_SPEC_MODE"] = "off"
            os.environ["XLLAMA_SPEC_DRAFT"] = "1"

        try:
            mixed_kv_ratio = float(
                self.data.get("mixed_kv_ratio", self.DEFAULTS["mixed_kv_ratio"])
            )
        except Exception:
            mixed_kv_ratio = self.DEFAULTS["mixed_kv_ratio"]
        os.environ["XLLAMA_MIXED_KV_RATIO"] = str(mixed_kv_ratio)

        print(
            f"[MenuManager] 已应用加载参数 "
            f"CTX={ctx}, KV Cache={kv_cache}, "
            f"Temperature={temperature}, VRAM Extra={vram_extra_mb} MB, "
            f"Enable Thinking={enable_thinking}, "
            f"MTP={mtp if mtp > 0 else '关闭'}"
        )


class MenuManager:
    def __init__(
        self,
        pet_instance,
        view_model,
        input_box,
        add_message_callback: Callable[[str, str], None],
        get_gear_rect_callback: Callable[[], Tuple[int, int, int, int]],
        scale: float,
        width: int,
        height: int,
    ):
        self.pet = pet_instance
        self.vm = view_model
        self.input_box = input_box
        self.add_message = add_message_callback
        self.get_gear_rect = get_gear_rect_callback
        self.scale = scale
        self.width = width
        self.height = height
        self._announce_timers = {}

        # 菜单状态
        self.context_menu: Dict[str, Any] = {
            "visible": False,
            "x": 0,
            "y": 0,
            "items": [],
            "rects": [],
        }
        self.settings_menu: Dict[str, Any] = {
            "visible": False,
            "x": 0,
            "y": 0,
            "items": [],
            "rects": [],
        }
        # 菜单B（语音设置）
        self.settings_menu_b: Dict[str, Any] = {
            "visible": False,
            "x": 0,
            "y": 0,
            "items": [],
            "rects": [],
        }
        # ('context'|'settings'|'settings_b', item_id)
        self.hovered_menu_item: Optional[Tuple[str, Any]] = None

        # 折叠菜单状态
        self.load_submenu_expanded = False
        self.param_submenu_expanded = False
        self.network_submenu_expanded = False
        self.voice_submenu_expanded = False
        
        # 模型加载参数配置
        self.model_settings = ModelSettings()

        # 清空对话外部回调
        self._on_clear_chat: Optional[Callable[[], None]] = None

        # 设置菜单防误关保护时间
        self._settings_keep_open_until = 0.0
        self._settings_b_keep_open_until = 0.0

        # 打开人设编辑弹窗回调
        self.on_open_character_editor: Optional[Callable[[], None]] = None
        
    # ==================== 上下文菜单 ====================
    def show_context_menu(self, x: int, y: int) -> None:
        has_selection = self.input_box.is_selection_active()
        clipboard_text = ClipboardService.get_text()
        items = [
            {"id": 1, "text": "复制", "enabled": has_selection},
            {"id": 2, "text": "粘贴", "enabled": bool(clipboard_text)},
            {"id": 3, "text": "剪切", "enabled": has_selection},
            {"id": "sep", "text": "", "enabled": False},
            {"id": 4, "text": "全选", "enabled": True},
        ]
        self.context_menu.clear()
        self.context_menu.update(
            {
                "visible": True,
                "x": x,
                "y": y,
                "items": items,
                "rects": [],
            }
        )
        self.hovered_menu_item = None

    def hide_context_menu(self) -> None:
        self.context_menu["visible"] = False
        self.hovered_menu_item = None

    def handle_context_menu_command(self, cmd: int) -> None:
        if cmd == 1:
            selected = self.input_box.copy_selected()
            if selected:
                ClipboardService.set_text(selected)
        elif cmd == 2:
            text = ClipboardService.get_text()
            if text:
                self.input_box.paste(text)
        elif cmd == 3:
            selected = self.input_box.cut_selected()
            if selected:
                ClipboardService.set_text(selected)
        elif cmd == 4:
            self.input_box.select_all()
        self.hide_context_menu()

    # ==================== 设置菜单A：内部状态 ====================
    def _is_model_loaded(self) -> bool:
        return bool(self.pet.model_ready and self.pet.llm_engine is not None)

    def _is_model_loading(self) -> bool:
        return bool(getattr(self.pet, "model_loading", False))

    def _can_load_model(self) -> bool:
        return not self._is_model_loaded() and not self._is_model_loading()

    def _can_change_params(self) -> bool:
        return self._can_load_model()

    # ==================== 设置菜单A：格式化 ====================
    @staticmethod
    def _format_temperature(value: Any) -> str:
        try:
            v = float(value)
        except Exception:
            return str(value)
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        return f"{v:g}"

    @staticmethod
    def _format_vram_extra(value: Any) -> str:
        try:
            v = int(value)
        except Exception:
            v = 0
        if v > 0:
            return f"+{v} MB"
        return f"{v} MB"

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _format_enable_thinking(value: Any) -> str:
        return "开启" if MenuManager._as_bool(value) else "关闭"

    @staticmethod
    def _format_mtp(value: Any) -> str:
        try:
            v = int(value)
        except Exception:
            return str(value)
        if v <= 0:
            return "关闭"
        return str(v)

    def _cycle_enable_thinking(self) -> None:
        if not self._can_change_params():
            return
        current = self._as_bool(
            self.model_settings.data.get("enable_thinking", False)
        )
        next_value = not current
        display_value = self._format_enable_thinking(next_value)
        self._save_and_announce(
            key="enable_thinking",
            value=next_value,
            label="思考",
            display_value=display_value,
        )

    def _get_int_setting(self, key: str, fallback: int) -> int:
        try:
            return int(self.model_settings.data.get(key, fallback))
        except Exception:
            return fallback

    def _get_float_setting(self, key: str, fallback: float) -> float:
        try:
            return float(self.model_settings.data.get(key, fallback))
        except Exception:
            return fallback

    # ==================== 设置菜单A：构建菜单项 ====================
    def _build_settings_items(self) -> List[Dict[str, Any]]:
        is_loaded = self._is_model_loaded()
        is_loading = self._is_model_loading()
        is_generating = getattr(self.vm, "is_generating", False)
        can_load = not is_loaded and not is_loading
        can_change_params = can_load

        if self.load_submenu_expanded and self.param_submenu_expanded:
            self.param_submenu_expanded = False
        if not can_load:
            self.load_submenu_expanded = False
        if not can_change_params:
            self.param_submenu_expanded = False

        indent = "    "
        items: List[Dict[str, Any]] = []

        # ---------- 一级菜单：加载本地模型 ----------
        load_arrow = "[-]" if self.load_submenu_expanded else "[+]"
        items.append(
            {
                "id": "load_model",
                "text": f"加载本地模型          {load_arrow}",
                "enabled": can_load,
            }
        )

        # ---------- 二级菜单：加载方式 ----------
        if self.load_submenu_expanded:
            items.extend(
                [
                    {"id": "load_gpu_mm", "text": indent + "使用显卡(多模)", "enabled": can_load},
                    {"id": "load_cpu_mm", "text": indent + "使用内存(多模)", "enabled": can_load},
                    {"id": "load_gpu_text", "text": indent + "使用显卡(文本)", "enabled": can_load},
                    {"id": "load_cpu_text", "text": indent + "使用内存(文本)", "enabled": can_load},
                    {"id": "sep_load", "text": "", "enabled": False},
                ]
            )

        # ---------- 一级菜单：模型加载参数 ----------
        param_arrow = "[-]" if self.param_submenu_expanded else "[+]"
        items.append(
            {
                "id": "model_params",
                "text": f"模型加载参数          {param_arrow}",
                "enabled": can_change_params,
            }
        )

        # ---------- 二级菜单 ----------
        if self.param_submenu_expanded:
            ctx_value = self._get_int_setting("ctx", 8192)
            kv_value = str(self.model_settings.data.get("kv_cache", "q8_0")).strip().lower()
            temp_value = self._get_float_setting("temperature", 0.7)
            vram_value = self._get_int_setting("vram_extra_mb", 0)
            thinking_value = self._as_bool(self.model_settings.data.get("enable_thinking", False))
            mtp_value = self._get_int_setting("mtp", 0)
            items.extend(
                [
                    {"id": "param_ctx", "text": indent + f"CTX: {ctx_value}", "enabled": can_change_params},
                    {"id": "param_kv", "text": indent + f"KV Cache: {kv_value}", "enabled": can_change_params},
                    {"id": "param_temp", "text": indent + f"温度: {self._format_temperature(temp_value)}", "enabled": can_change_params},
                    {"id": "param_vram", "text": indent + f"显存调整: {self._format_vram_extra(vram_value)}", "enabled": can_change_params},
                    {"id": "param_think", "text": indent + f"思考: {self._format_enable_thinking(thinking_value)}", "enabled": can_change_params},
                    {"id": "param_mtp", "text": indent + f"MTP: {self._format_mtp(mtp_value)}", "enabled": can_change_params},
                    {"id": "sep_params", "text": "", "enabled": False},
                ]
            )

        # ---------- 一级菜单：网络设置（折叠） ----------
        network_arrow = "[-]" if self.network_submenu_expanded else "[+]"
        items.append(
            {
                "id": "network_settings",
                "text": f"网络设置                 {network_arrow}",
                "enabled": True,
            }
        )

        # ---------- 二级菜单：联网搜索 / 网站和代理 ----------
        if self.network_submenu_expanded:
            try:
                from internet_settings import InternetSettings
                _web_search_on = InternetSettings.get_instance().is_enabled
            except Exception:
                _web_search_on = False
            items.extend(
                [
                    {
                        "id": "web_search_toggle",
                        "text": indent + f"联网搜索：{'开启' if _web_search_on else '关闭'}",
                        "enabled": True,
                    },
                    {
                        "id": "internet_settings_file",
                        "text": indent + "网站和代理",
                        "enabled": True,
                    },
                ]
            )
        items.append({"id": "sep_internet", "text": "", "enabled": False})
        
        # ---------- 设置项 ----------
        items.extend(
            [
                {"id": "character_file", "text": "模型人设", "enabled": can_load},
                {"id": "unload", "text": "卸载模型", "enabled": is_loaded and not is_loading},
                {"id": "sep_unload", "text": "", "enabled": False},
                {"id": "clear_chat", "text": "清空对话", "enabled": len(self.vm.messages) > 0},
                {"id": "stop_generation", "text": "停止生成", "enabled": is_loaded and not is_loading and is_generating},
            ]
        )
        return items

    # ==================== 设置菜单A：显示/隐藏 ====================
    def show_settings_menu(self, x: int, y: int) -> None:
        self.hide_settings_menu_b(force=True)
        gear_x, gear_y, gear_w, gear_h = self.get_gear_rect()
        menu_x = gear_x
        menu_y = gear_y + gear_h + int(5 * self.scale)
        self.settings_menu.clear()
        self.settings_menu.update(
            {
                "visible": True,
                "x": menu_x,
                "y": menu_y,
                "items": self._build_settings_items(),
                "rects": [],
            }
        )
        self.hovered_menu_item = None
        self._settings_keep_open_until = 0.0

    def refresh_settings_menu(self, clear_rects: bool = False) -> None:
        if not self.settings_menu.get("visible", False):
            return
        self.settings_menu["items"] = self._build_settings_items()
        if clear_rects:
            self.settings_menu["rects"] = []
        else:
            old_rects = self.settings_menu.get("rects", []) or []
            self.settings_menu["rects"] = old_rects[: len(self.settings_menu["items"])]
        self.hovered_menu_item = None
        self._settings_keep_open_until = time.time() + 0.18

    def hide_settings_menu(self, force: bool = False) -> None:
        if not force and time.time() < getattr(self, "_settings_keep_open_until", 0.0):
            return
        self.settings_menu["visible"] = False
        self.hovered_menu_item = None
        self.load_submenu_expanded = False
        self.param_submenu_expanded = False
        self.network_submenu_expanded = False

    # ==================== 延迟防抖播报 ====================
    def _schedule_announce(self, key: str, message: str) -> None:
        if key in self._announce_timers:
            self._announce_timers[key].cancel()
        
        def _do_announce():
            if hasattr(self.vm, "update_queue"):
                self.vm.update_queue.put(('announce', message))
                try:
                    glfw.post_empty_event()  # 唤醒主线程消费队列
                except Exception:
                    pass
            else:
                self.add_message("shizuku", message)

        timer = threading.Timer(0.8, _do_announce)
        self._announce_timers[key] = timer
        timer.start()

    # ==================== 设置菜单A：参数保存与提示 ====================
    def _save_and_announce(
        self,
        key: str,
        value: Any,
        label: str,
        display_value: str,
    ) -> None:
        self.model_settings.data[key] = value
        self.model_settings.save()
        self.refresh_settings_menu(clear_rects=False)
        
        message = f"{label} 已切换为 {display_value}，重新加载模型后生效。"
        self._schedule_announce(key, message)

    # ==================== 设置菜单A：参数循环切换 ====================
    def _cycle_int_option(
        self,
        key: str,
        options: List[int],
        label: str,
        fallback: int,
        formatter: Optional[Callable[[Any], str]] = None,
    ) -> None:
        if not self._can_change_params():
            return
        current = self._get_int_setting(key, fallback)
        if current in options:
            next_value = options[(options.index(current) + 1) % len(options)]
        else:
            next_value = fallback if fallback in options else options[0]
        display_value = formatter(next_value) if formatter else str(next_value)
        self._save_and_announce(key, next_value, label, display_value)

    def _cycle_float_option(
        self,
        key: str,
        options: List[float],
        label: str,
        fallback: float,
        formatter: Optional[Callable[[Any], str]] = None,
    ) -> None:
        if not self._can_change_params():
            return
        current = self._get_float_setting(key, fallback)
        current_index = None
        for i, opt in enumerate(options):
            try:
                if abs(float(opt) - current) < 1e-9:
                    current_index = i
                    break
            except Exception:
                pass
        if current_index is None:
            next_value = options[0]
            for opt in options:
                try:
                    if abs(float(opt) - float(fallback)) < 1e-9:
                        next_value = opt
                        break
                except Exception:
                    pass
        else:
            next_value = options[(current_index + 1) % len(options)]
        display_value = formatter(next_value) if formatter else str(next_value)
        self._save_and_announce(key, next_value, label, display_value)

    def _cycle_str_option(
        self,
        key: str,
        options: List[str],
        label: str,
        fallback: str,
        formatter: Optional[Callable[[Any], str]] = None,
    ) -> None:
        if not self._can_change_params():
            return
        current = str(self.model_settings.data.get(key, "")).strip().lower()
        if current in options:
            next_value = options[(options.index(current) + 1) % len(options)]
        else:
            fallback_value = str(fallback).strip().lower()
            next_value = fallback_value if fallback_value in options else options[0]
        display_value = formatter(next_value) if formatter else str(next_value)
        self._save_and_announce(key, next_value, label, display_value)

    def _cycle_ctx(self) -> None:
        self._cycle_int_option(key="ctx", options=CTX_OPTIONS, label="CTX", fallback=8192)

    def _cycle_kv_cache(self) -> None:
        self._cycle_str_option(key="kv_cache", options=KV_CACHE_OPTIONS, label="KV Cache", fallback="q8_0")

    def _cycle_temperature(self) -> None:
        self._cycle_float_option(
            key="temperature",
            options=TEMPERATURE_OPTIONS,
            label="温度",
            fallback=0.7,
            formatter=self._format_temperature,
        )

    def _cycle_vram_extra(self) -> None:
        self._cycle_int_option(
            key="vram_extra_mb",
            options=VRAM_EXTRA_OPTIONS,
            label="显存调整",
            fallback=0,
            formatter=self._format_vram_extra,
        )

    def _cycle_mtp(self) -> None:
        self._cycle_int_option(key="mtp", options=MTP_OPTIONS, label="MTP", fallback=0, formatter=self._format_mtp)

    def _apply_model_settings(self) -> None:
        self.model_settings.apply_to_env()

    def _open_character_file(self) -> None:
        path = _get_character_file_path()
        if not os.path.exists(path):
            self.add_message("shizuku", "未找到人设文件 character.json")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.add_message("shizuku", f"打开文件失败: {e}")

    # ==================== 联网搜索：开关与配置 ====================

    def _toggle_web_search(self) -> None:
        try:
            from internet_settings import InternetSettings
        except Exception as e:
            self.add_message("shizuku", f"联网设置模块不可用: {e}")
            return
        settings = InternetSettings.get_instance()
        # 重载磁盘配置，避免覆盖手动修改
        settings.reload()
        next_value = not settings.is_enabled
        settings.set_enabled(next_value)
        self.refresh_settings_menu(clear_rects=False)
        if next_value:
            names = settings.enabled_source_names()
            if not names or names == "无":
                self.add_message(
                    "shizuku",
                    "联网搜索已开启，但白名单里没有启用的来源，"
                    "请先点「网站和代理」配置。"
                )
            else:
                self.add_message(
                    "shizuku",
                    f"联网搜索已开启，下一轮对话即可使用。可用来源：{names}"
                )
        else:
            self.add_message("shizuku", "联网搜索已关闭。")

    def _open_internet_settings_file(self) -> None:
        try:
            from internet_settings import InternetSettings
        except Exception as e:
            self.add_message("shizuku", f"联网设置模块不可用: {e}")
            return
        path = InternetSettings.get_settings_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                self.add_message(
                    "shizuku",
                    f"警告：Internet_setting.json 语法有误（第{e.lineno}行：{e.msg}），"
                    f"已按上次有效配置运行。请修正后保存。"
                )
            except Exception as e:
                self.add_message("shizuku", f"警告：配置文件读取异常：{e}")
        try:
            InternetSettings.get_instance().reload()
        except Exception:
            pass
        if not os.path.exists(path):
            self.add_message("shizuku", "未找到联网配置文件。")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.add_message("shizuku", f"打开文件失败: {e}")
            return
        self.add_message("shizuku", "已打开联网配置，修改保存后下一轮对话生效。")
        
    # ==================== 设置菜单A：命令处理 ====================
    def handle_settings_command(self, cmd: str) -> None:
        # 折叠菜单：加载本地模型
        if cmd == "load_model":
            if self._can_load_model():
                if self.load_submenu_expanded:
                    self.load_submenu_expanded = False
                else:
                    self.load_submenu_expanded = True
                    self.param_submenu_expanded = False
                    self.network_submenu_expanded = False
                self.refresh_settings_menu(clear_rects=True)
            return

        # 折叠菜单：模型加载参数
        if cmd == "model_params":
            if self._can_change_params():
                if self.param_submenu_expanded:
                    self.param_submenu_expanded = False
                else:
                    self.param_submenu_expanded = True
                    self.load_submenu_expanded = False
                    self.network_submenu_expanded = False
                self.refresh_settings_menu(clear_rects=True)
            return

        # 参数切换
        if cmd == "param_ctx":
            self._cycle_ctx()
            return
        if cmd == "param_kv":
            self._cycle_kv_cache()
            return
        if cmd == "param_temp":
            self._cycle_temperature()
            return
        if cmd == "param_vram":
            self._cycle_vram_extra()
            return
        if cmd == "param_think":
            self._cycle_enable_thinking()
            return
        if cmd == "param_mtp":
            self._cycle_mtp()
            return
        if cmd == "network_settings":
            if self.network_submenu_expanded:
                self.network_submenu_expanded = False
            else:
                self.network_submenu_expanded = True
                self.load_submenu_expanded = False
                self.param_submenu_expanded = False
            self.refresh_settings_menu(clear_rects=True)
            return

        # 联网搜索：开关 / 打开配置文件
        if cmd == "web_search_toggle":
            self._toggle_web_search()
            return
        if cmd == "internet_settings_file":
            self._open_internet_settings_file()
            return
        
        if cmd == "character_file":
            if self._can_load_model():
                if self.on_open_character_editor is not None:
                    self.on_open_character_editor()
                self.hide_settings_menu(force=True)
            return
        
        # 实际加载模型
        load_actions = {
            "load_gpu_mm": (True, True, "正在加载到显卡（多模）..."),
            "load_cpu_mm": (False, True, "正在加载到内存（多模）..."),
            "load_gpu_text": (True, False, "正在加载到显卡（文本）..."),
            "load_cpu_text": (False, False, "正在加载到内存（文本）..."),
        }
        if cmd in load_actions:
            use_gpu, use_mmproj, message = load_actions[cmd]
            self._apply_model_settings()

            try:
                from prompt import clear_character_cache
                clear_character_cache()
            except Exception:
                pass

            self.add_message("shizuku", message)
            self.pet.load_llm_manually(use_gpu=use_gpu, use_mmproj=use_mmproj)
            self.hide_settings_menu(force=True)
            return
        
        # 卸载模型
        if cmd == "unload":
            self.pet.unload_llm()            
            self.pet.queue_bubble("脑子没啦，真是好轻松~")
            self.add_message("shizuku", "模型已卸载")
        # 清空对话
        elif cmd == "clear_chat":
            self.vm.clear_messages()
            if self._on_clear_chat:
                self._on_clear_chat()
        # 停止生成
        elif cmd == "stop_generation":
            if getattr(self.vm, "is_generating", False) and hasattr(self.vm, "stop_generation"):
                stopped = self.vm.stop_generation()
                if not stopped:
                    self.add_message("shizuku", "（已放弃思考）")
        self.hide_settings_menu(force=True)

    # ==================== 菜单B：语音设置 ====================
    @staticmethod
    def _format_voice_enabled(value: Any) -> str:
        return "开启" if MenuManager._as_bool(value) else "关闭"

    @staticmethod
    def _format_voice_shortcut(value: Any) -> str:
        v = str(value).strip().lower()
        if v == "wheel":
            return "滚轮"
        return "关闭"

    def _build_settings_items_b(self) -> List[Dict[str, Any]]:
        indent = "    "
        items: List[Dict[str, Any]] = []

        # 一级菜单：语音设置（可展开）
        arrow = "[-]" if self.voice_submenu_expanded else "[+]"
        items.append(
            {
                "id": "voice_settings",
                "text": f"语音设置   {arrow}",
                "enabled": True,
            }
        )

        # 二级菜单：录音开关 + 快捷键
        if self.voice_submenu_expanded:
            voice_enabled = self._as_bool(
                self.model_settings.data.get("voice_enabled", True)
            )
            voice_shortcut = str(
                self.model_settings.data.get("voice_shortcut", "wheel")
            ).strip().lower()

            items.append(
                {
                    "id": "voice_enabled",
                    "text": indent + f"录音：{self._format_voice_enabled(voice_enabled)}",
                    "enabled": True,
                }
            )
            items.append(
                {
                    "id": "voice_shortcut",
                    "text": indent + f"快捷键：{self._format_voice_shortcut(voice_shortcut)}",
                    # 录音关闭时禁用快捷键项
                    "enabled": voice_enabled,
                }
            )

        return items

    def show_settings_menu_b(self, x: int, y: int) -> None:
        """显示菜单B（语音设置）"""
        self.hide_settings_menu(force=True)
        gear_x, gear_y, gear_w, gear_h = self.get_gear_rect()
        menu_x = gear_x
        menu_y = gear_y + gear_h + int(5 * self.scale)
        self.settings_menu_b.clear()
        self.settings_menu_b.update(
            {
                "visible": True,
                "x": menu_x,
                "y": menu_y,
                "items": self._build_settings_items_b(),
                "rects": [],
            }
        )
        self.hovered_menu_item = None
        self._settings_b_keep_open_until = 0.0

    def refresh_settings_menu_b(self, clear_rects: bool = False) -> None:
        """刷新菜单B"""
        if not self.settings_menu_b.get("visible", False):
            return
        self.settings_menu_b["items"] = self._build_settings_items_b()
        if clear_rects:
            self.settings_menu_b["rects"] = []
        else:
            old_rects = self.settings_menu_b.get("rects", []) or []
            self.settings_menu_b["rects"] = old_rects[: len(self.settings_menu_b["items"])]
        self.hovered_menu_item = None
        self._settings_b_keep_open_until = time.time() + 0.18

    def hide_settings_menu_b(self, force: bool = False) -> None:
        if not force and time.time() < getattr(self, "_settings_b_keep_open_until", 0.0):
            return
        self.settings_menu_b["visible"] = False
        self.hovered_menu_item = None
        self.voice_submenu_expanded = False

    def _cycle_voice_enabled(self) -> None:
        """循环切换语音开关：开启 ↔ 关闭"""
        current = self._as_bool(self.model_settings.data.get("voice_enabled", True))
        next_value = not current
        display_value = self._format_voice_enabled(next_value)
        self.model_settings.data["voice_enabled"] = next_value
        self.model_settings.save()
        self.refresh_settings_menu_b(clear_rects=False)
        self.add_message(
            "shizuku",
            f"语音输入已{display_value}。",
        )

    def _cycle_voice_shortcut(self) -> None:
        # 仅在录音开启时允许切换
        if not self._as_bool(self.model_settings.data.get("voice_enabled", True)):
            return
        current = str(self.model_settings.data.get("voice_shortcut", "wheel")).strip().lower()
        options = ["wheel", "off"]
        if current in options:
            next_value = options[(options.index(current) + 1) % len(options)]
        else:
            next_value = "wheel"
        display_value = self._format_voice_shortcut(next_value)
        self.model_settings.data["voice_shortcut"] = next_value
        self.model_settings.save()
        self.refresh_settings_menu_b(clear_rects=False)
        self.add_message(
            "shizuku",
            f"录音快捷键已切换为 {display_value}。",
        )

    def handle_settings_menu_b_command(self, cmd: str) -> None:
        # 折叠菜单：语音设置
        if cmd == "voice_settings":
            if self.voice_submenu_expanded:
                self.voice_submenu_expanded = False
            else:
                self.voice_submenu_expanded = True
            self.refresh_settings_menu_b(clear_rects=True)
            return

        # 录音开关
        if cmd == "voice_enabled":
            self._cycle_voice_enabled()
            return

        # 快捷键切换
        if cmd == "voice_shortcut":
            self._cycle_voice_shortcut()
            return

    # ==================== 悬停更新 ====================
    def set_hovered_item(self, menu_type: str, item_id: Any) -> None:
        self.hovered_menu_item = (menu_type, item_id) if item_id is not None else None

    def clear_hover(self) -> None:
        self.hovered_menu_item = None

    # ==================== 菜单可见性判断 ====================
    def is_context_menu_visible(self) -> bool:
        return self.context_menu.get("visible", False)

    def is_settings_menu_visible(self) -> bool:
        return self.settings_menu.get("visible", False)

    def is_settings_menu_b_visible(self) -> bool:
        return self.settings_menu_b.get("visible", False)

    # ==================== 注册清空对话的外部回调 ====================
    def set_on_clear_chat(self, callback: Callable[[], None]) -> None:
        self._on_clear_chat = callback
