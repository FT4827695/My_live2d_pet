# -*- coding: utf-8 -*-
"""
model_registry.py
"""

import os
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    from settings_store import BASE_DIR
except Exception:
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(_this_dir, "live2d_models")):
        BASE_DIR = _this_dir
    else:
        BASE_DIR = os.path.dirname(_this_dir)

DEFAULT_HEAD_KEYWORDS = [
    "HAIR", "FACE", "EYE", "BROW", "MOUTH", "NOSE", "EAR", "HEAD",
]
DEFAULT_BODY_KEYWORDS = [
    "UNIFORM", "BODY", "PAJAMA", "ARM", "NECK",
    "IRAIRA", "ASE", "KIRAKIRA", "SKETCH",
]
DEFAULT_CHARACTER_NAME = "雫"
DEFAULT_CHARACTER_DESC = "你是桌面助手，一个可爱的二次元少女角色。"


# ==================================================================
# ModelContext：单个模型的完整上下文
# ==================================================================
@dataclass
class ModelContext:
    # ---- 基础标识与路径 ----
    model_id: str                                     # 文件夹名
    model_dir: str                                    # 模型目录绝对路径
    model_json_path: str                              # model.json 绝对路径
    model_json_dict: dict                             # 解析后的 model.json 原始字典

    # ---- 从 model.json 自动识别 ----
    physics_path: Optional[str] = None                              # 物理文件（可为 None）
    motion_groups: Dict[str, List[int]] = field(default_factory=dict)  # {组名: [索引]}
    expressions: List[str] = field(default_factory=list)            # 表情名列表

    # ---- 从 profile.json 读取（缺失时用默认值） ----
    semantic_motions: Dict[str, str] = field(default_factory=dict)  # 语义 -> 实际组名
    motion_durations: Dict[str, float] = field(default_factory=dict)  # 组名 -> 时长(秒)
    head_keywords: List[str] = field(default_factory=list)
    body_keywords: List[str] = field(default_factory=list)
    sound_map: Dict[str, str] = field(default_factory=dict)         # 语义 -> 音效前缀
    sound_dialog: Dict[str, str] = field(default_factory=dict)      # 音效key -> 台词
    messages: Dict[str, List[str]] = field(default_factory=dict)    # 场景 -> 台词列表

    # ---- 模型自带人设  ----
    profile_name: Optional[str] = None
    profile_description: Optional[str] = None

    # ---- 音效目录（约定为模型目录下 snd/） ----
    sound_dir: Optional[str] = None

    # ---------------- 便捷方法 ----------------
    def get_semantic_group(self, semantic_name: str) -> str:
        return self.semantic_motions.get(semantic_name, semantic_name)

    def get_motion_duration(self, group: str, default: float = 3.0) -> float:
        return self.motion_durations.get(group, default)

    def get_messages(self, scene: str) -> List[str]:
        return self.messages.get(scene, [])

    def has_semantic(self, semantic_name: str) -> bool:
        if semantic_name in self.semantic_motions:
            return True
        return semantic_name in self.motion_groups

    hit_mode: str = "parts"    # "parts"=纯部件 | "hybrid"=部件+分区兜底
    head_zones: List[tuple[float, float, float, float]] = field(default_factory=list)
    body_zones: List[tuple[float, float, float, float]] = field(default_factory=list)

# ==================================================================
# 人设合并
# ==================================================================
def resolve_character(model_ctx: Optional[ModelContext],
                      global_name: Optional[str] = None,
                      global_desc: Optional[str] = None):
    name = model_ctx.profile_name if model_ctx else None
    desc = model_ctx.profile_description if model_ctx else None
    name = name or global_name or DEFAULT_CHARACTER_NAME
    desc = desc or global_desc or DEFAULT_CHARACTER_DESC
    return name, desc


# ==================================================================
# ModelRegistry：模型注册表
# ==================================================================
class ModelRegistry:
    def __init__(self, models_root: Optional[str] = None):
        if models_root is None:
            models_root = os.path.join(BASE_DIR, "live2d_models")
        self.models_root = models_root
        self._contexts: Dict[str, ModelContext] = {}

    # ---------------- 扫描 ----------------
    def scan(self) -> Dict[str, ModelContext]:
        self._contexts.clear()
        if not os.path.isdir(self.models_root):
            print(f"[ModelRegistry] 模型目录不存在: {self.models_root}")
            return self._contexts

        for entry in sorted(os.listdir(self.models_root)):
            model_dir = os.path.join(self.models_root, entry)
            if not os.path.isdir(model_dir):
                continue
            model_json_path = self._find_model_json(model_dir)
            if model_json_path is None:
                continue
            ctx = self._build_context(entry, model_dir, model_json_path)
            if ctx is not None:
                self._contexts[entry] = ctx
                print(f"[ModelRegistry] 发现模型: {entry}")

        if not self._contexts:
            print(f"[ModelRegistry] 未在 {self.models_root} 发现任何有效模型")
        return self._contexts

    def _find_model_json(self, model_dir: str) -> Optional[str]:
        try:
            for fname in os.listdir(model_dir):
                lower = fname.lower()
                if lower.endswith(".model.json") or lower.endswith(".model3.json"):
                    return os.path.join(model_dir, fname)
        except Exception as e:
            print(f"[ModelRegistry] 列举目录失败 {model_dir}: {e}")
        return None

    # ---------------- 构建 ----------------
    def _build_context(self, model_id, model_dir, model_json_path) -> Optional[ModelContext]:
        # 1) 读取 model.json
        try:
            with open(model_json_path, "r", encoding="utf-8") as f:
                model_json_dict = json.load(f)
        except Exception as e:
            print(f"[ModelRegistry] 读取 model.json 失败 ({model_id}): {e}")
            return None
        if not isinstance(model_json_dict, dict):
            print(f"[ModelRegistry] model.json 格式异常 ({model_id})")
            return None

        # 2) 读取可选的 profile.json
        profile = self._load_profile(model_dir)

        # 3) 从 model.json 自动识别
        physics_path = self._resolve_physics(model_json_dict, model_dir, model_id)
        motion_groups = self._parse_motion_groups(model_json_dict)
        expressions = self._parse_expressions(model_json_dict)

        # 4) 音效目录（约定为模型目录下的 snd/）
        sound_dir = os.path.join(model_dir, "snd")
        if not os.path.isdir(sound_dir):
            sound_dir = None

        return ModelContext(
            model_id=model_id,
            model_dir=model_dir,
            model_json_path=model_json_path,
            model_json_dict=model_json_dict,
            physics_path=physics_path,
            motion_groups=motion_groups,
            expressions=expressions,
            semantic_motions=profile.get("semantic_motions") or {},
            motion_durations=profile.get("motion_durations") or {},
            head_keywords=profile.get("head_keywords") or DEFAULT_HEAD_KEYWORDS,
            body_keywords=profile.get("body_keywords") or DEFAULT_BODY_KEYWORDS,
            sound_map=profile.get("sound_map") or {},
            sound_dialog=profile.get("sound_dialog") or {},
            messages=profile.get("messages") or {},
            profile_name=profile.get("name"),
            profile_description=profile.get("description"),
            sound_dir=sound_dir,
        )

    def _load_profile(self, model_dir: str) -> dict:
        profile_path = os.path.join(model_dir, "profile.json")
        if not os.path.exists(profile_path):
            return {}
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            print(f"[ModelRegistry] profile.json 不是字典: {profile_path}")
        except Exception as e:
            print(f"[ModelRegistry] 读取 profile.json 失败: {e}")
        return {}

    def _resolve_physics(self, model_json_dict, model_dir, model_id) -> Optional[str]:
        # 优先 model.json 的 physics 字段
        physics_rel = model_json_dict.get("physics")
        if isinstance(physics_rel, str) and physics_rel:
            candidate = os.path.join(model_dir, physics_rel)
            if os.path.exists(candidate):
                return candidate
        # 兜底：常见命名
        for name in (f"{model_id}.physics.json", "physics.json"):
            candidate = os.path.join(model_dir, name)
            if os.path.exists(candidate):
                return candidate
        return None

    def _parse_motion_groups(self, model_json_dict) -> Dict[str, List[int]]:
        groups: Dict[str, List[int]] = {}
        motions = model_json_dict.get("motions", {})
        if isinstance(motions, dict):
            for group, motion_list in motions.items():
                if isinstance(motion_list, list):
                    groups[group] = list(range(len(motion_list)))
        return groups

    def _parse_expressions(self, model_json_dict) -> List[str]:
        expressions: List[str] = []
        expr_list = model_json_dict.get("expressions", [])
        if isinstance(expr_list, list):
            for expr in expr_list:
                if isinstance(expr, dict):
                    name = expr.get("name") or expr.get("Name")
                    if name:
                        expressions.append(str(name))
        return expressions

    # ---------------- 查询接口 ----------------
    def get(self, model_id: str) -> Optional[ModelContext]:
        return self._contexts.get(model_id)

    def list_model_ids(self) -> List[str]:
        return list(self._contexts.keys())

    def get_first(self) -> Optional[ModelContext]:
        if self._contexts:
            return next(iter(self._contexts.values()))
        return None

    def resolve(self, preferred_id: Optional[str] = None) -> Optional[ModelContext]:
        if preferred_id and preferred_id in self._contexts:
            return self._contexts[preferred_id]
        return self.get_first()

    def __len__(self):
        return len(self._contexts)
