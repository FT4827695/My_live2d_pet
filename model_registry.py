# -*- coding: utf-8 -*-
"""
model_registry.py
"""
import os
import json
import random
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

DEFAULT_IGNORE_PART_KEYWORDS = ["CORE", "BACKGROUND", "FACESHADOW", "SHADOW"]
DEFAULT_VETO_PART_KEYWORDS = ["BACKGROUND"]

DEFAULT_IGNORE_KEYWORDS = ["CORE", "BACKGROUND", "FACESHADOW"]
DEFAULT_CHARACTER_NAME = "雫"
DEFAULT_CHARACTER_DESC = "你是桌面助手，一个可爱的二次元少女角色。"

def _guess_semantic_motions(motion_groups: Dict[str, List[int]]) -> Dict[str, str]:
    groups = list(motion_groups.keys())
    norm = {g: g.strip().lower() for g in groups}
    empty = next((g for g in groups if norm[g] == ""), None)

    def pick(*cands):
        for c in cands:                      # 精确
            for g in groups:
                if norm[g] == c:
                    return g
        for c in cands:                      # 包含
            if not c:
                continue
            for g in groups:
                if c in norm[g]:
                    return g
        return None

    m: Dict[str, str] = {}
    g_idle  = pick("idle")
    g_head  = pick("flick_head", "tap_head", "head")
    g_body  = pick("tap_body", "body")
    g_shake = pick("shake")
    g_pi    = pick("pinch_in")
    g_po    = pick("pinch_out")
    if g_idle is not None:  m["idle"] = g_idle
    if g_head is not None:  m["tap_head"] = g_head
    elif empty is not None: m["tap_head"] = empty
    if g_body is not None:  m["tap_body"] = g_body
    elif empty is not None: m["tap_body"] = empty
    if g_shake is not None: m["shake"] = g_shake
    if g_pi is not None:    m["pinch_in"] = g_pi
    if g_po is not None:    m["pinch_out"] = g_po
    return m

_EYE_EXCLUDE_TOKENS = ("BALL", "SMILE", "FORM", "SIZE", "HI", "KIRAKIRA", "WHITE")


def _is_eye_open_param(name: str) -> bool:
    n = name.upper()
    if "EYE" not in n or "OPEN" not in n:
        return False
    tokens = n.split("_")
    return not any(t in tokens for t in _EYE_EXCLUDE_TOKENS)


def _is_eye_smile_param(name: str) -> bool:
    n = name.upper()
    return "EYE" in n and "SMILE" in n and "BALL" not in n.split("_")


def _parse_mtn_motion(path: str) -> dict:
    fps = 30.0
    max_len = 0
    open_last, open_const, smile_vals = {}, {}, []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("$"):
                    k, _, v = line[1:].partition("=")
                    if k.strip().lower() == "fps":
                        try:
                            fps = float(v.strip())
                        except ValueError:
                            pass
                    continue
                name, sep, values = line.partition("=")
                if not sep:
                    continue
                values = values.strip().rstrip(",")
                if not values:
                    continue
                n = values.count(",") + 1
                if n > max_len:
                    max_len = n
                name = name.strip()
                is_open = _is_eye_open_param(name)
                is_smile = _is_eye_smile_param(name)
                if not (is_open or is_smile):
                    continue
                try:
                    vals = [float(x) for x in values.split(",") if x.strip()]
                except ValueError:
                    continue
                if not vals:
                    continue
                if is_open:
                    open_last[name] = vals[-1]
                    open_const[name] = (max(vals) - min(vals)) <= 1e-6
                else:
                    smile_vals.append(vals[-1])
    except Exception:
        return {"duration": 0.0, "eye": None}
    duration = (max_len - 1) / fps if (max_len > 1 and fps > 0) else 0.0
    eye = None
    if open_last:
        eye = {
            "open_last": open_last,
            "open_const": open_const,
            "smile_max": max(smile_vals) if smile_vals else 0.0,
        }
    return {"duration": duration, "eye": eye}

# ==================================================================
# ModelContext：单个模型的完整上下文
# ==================================================================
@dataclass
class ModelContext:
    model_id: str
    model_dir: str
    model_json_path: str
    model_json_dict: dict

    physics_path: Optional[str] = None
    motion_groups: Dict[str, List[int]] = field(default_factory=dict)
    expressions: List[str] = field(default_factory=list)

    semantic_motions: Dict[str, str] = field(default_factory=dict)
    motion_durations: Dict[str, float] = field(default_factory=dict)
    # ---- 派生数据 ----
    motion_durations_auto: Dict[str, List[float]] = field(default_factory=dict)
    motion_files: Dict[str, List[str]] = field(default_factory=dict)
    motion_sounds: Dict[str, List[Optional[str]]] = field(default_factory=dict) 
    motion_eye: Dict[str, List[Optional[dict]]] = field(default_factory=dict)
    eye_open_params: List[str] = field(default_factory=list)
    eye_neutral: float = 1.0 
    head_keywords: List[str] = field(default_factory=list)
    body_keywords: List[str] = field(default_factory=list)
    sound_map: Dict[str, str] = field(default_factory=dict)
    sound_dialog: Dict[str, str] = field(default_factory=dict)
    messages: Dict[str, List[str]] = field(default_factory=dict)

    profile_name: Optional[str] = None
    profile_description: Optional[str] = None
    sound_dir: Optional[str] = None

    # ---- 部位交互 ----
    hit_mode: str = "parts"
    head_zones: List[tuple] = field(default_factory=list)
    body_zones: List[tuple] = field(default_factory=list)
    hit_rules: List[dict] = field(default_factory=list)
    ignore_part_keywords: List[str] = field(default_factory=list)
    veto_part_keywords: List[str] = field(default_factory=list)
    # 解析后的部位候选: {part: [(group, index, weight), ...]}
    part_motions: Dict[str, list] = field(default_factory=dict)
    idle_candidates: List[tuple] = field(default_factory=list)
    # 自动解析: {group: [每个动作的时长]}，{规范化文件路径: (group, index)}
    motion_duration_map: Dict[str, List[float]] = field(default_factory=dict)
    motion_file_map: Dict[str, tuple] = field(default_factory=dict)

    _derived_loaded: bool = field(default=False, init=False, repr=False)

    # ---------------- 便捷方法 ----------------
    def get_semantic_group(self, semantic_name: str) -> str:
        return self.semantic_motions.get(semantic_name, semantic_name)

    def ensure_derived(self):
        if self._derived_loaded:
            return
        self._derived_loaded = True
        motions = self.model_json_dict.get("motions")
        if not isinstance(motions, dict):
            return
        for group, mlist in motions.items():
            if not isinstance(mlist, list):
                continue
            durs: List[float] = []
            files: List[str] = []
            sounds: List[Optional[str]] = []
            eyes: List[Optional[dict]] = []
            for item in mlist:
                rel = item.get("file", "") if isinstance(item, dict) else ""
                files.append(rel)
                if rel:
                    info = _parse_mtn_motion(os.path.join(self.model_dir, rel))
                else:
                    info = {"duration": 0.0, "eye": None}
                durs.append(info["duration"])
                eyes.append(info["eye"])
                sounds.append(self._resolve_motion_sound(item, rel))
            self.motion_durations_auto[group] = durs
            self.motion_files[group] = files
            self.motion_sounds[group] = sounds
            self.motion_eye[group] = eyes

        names = set()
        for eyes in self.motion_eye.values():
            for e in eyes:
                if e:
                    names.update(e["open_last"].keys())
        self.eye_open_params = sorted(names)

        self.eye_neutral = 1.0
        idle_eyes = self.motion_eye.get(self.get_semantic_group("idle")) or []
        if idle_eyes and idle_eyes[0]:
            vals = list(idle_eyes[0]["open_last"].values())
            if vals and 0.5 <= vals[0] <= 2.0:
                self.eye_neutral = vals[0]
                
    def _resolve_motion_sound(self, item, motion_rel: str) -> Optional[str]:
        if isinstance(item, dict):
            rel = item.get("sound")
            if isinstance(rel, str) and rel:
                p = os.path.join(self.model_dir, rel)
                if os.path.exists(p):
                    return p
        if motion_rel and self.sound_dir:
            stem = os.path.splitext(os.path.basename(motion_rel))[0]
            for ext in (".mp3", ".wav", ".ogg"):
                p = os.path.join(self.sound_dir, stem + ext)
                if os.path.exists(p):
                    return p
        return None

    def has_any_sound(self) -> bool:
        self.ensure_derived()
        return any(any(lst) for lst in self.motion_sounds.values())

    def release_derived(self):
        self.motion_durations_auto.clear()
        self.motion_files.clear()
        self.motion_sounds.clear()
        self._derived_loaded = False
        self.motion_eye.clear()
        self.eye_open_params = []
        self.eye_neutral = 1.0
        
    def get_motion_duration(self, group: str, index: Optional[int] = None,
                            default: float = 3.0) -> float:
        self.ensure_derived()
        arr = self.motion_durations_auto.get(group)
        # 1) 优先按动作实测时长
        if index is not None and arr and 0 <= index < len(arr) and arr[index] > 0:
            return arr[index]
        # 2) 回退 profile 的组级配置
        if group in self.motion_durations:
            return self.motion_durations[group]
        # 3) 回退组内最长
        if arr:
            valid = [d for d in arr if d > 0]
            if valid:
                return max(valid)
        return default

    def get_eye_recovery(self, group: str, index: int):
        self.ensure_derived()
        eyes = self.motion_eye.get(group)
        if not eyes or not (0 <= index < len(eyes)) or eyes[index] is None:
            return False, {}
        e = eyes[index]
        if e.get("smile_max", 0.0) > 0.5:
            return False, {}
        open_last = e["open_last"]
        if all(e["open_const"].get(k, False) for k in open_last) and \
           all(v <= 0.05 for v in open_last.values()):
            return False, {}
        if all(abs(v - self.eye_neutral) <= 0.05 for v in open_last.values()):
            return False, {}
        return True, dict(open_last)
    
    def get_messages(self, scene: str) -> List[str]:
        return self.messages.get(scene, [])

    def has_semantic(self, semantic_name: str) -> bool:
        if semantic_name in self.semantic_motions:
            return True
        return semantic_name in self.motion_groups

    def get_part_motion(self, part: str):
        return self.part_motions.get(part)

    def pick_part_motion(self, part: str):
        cfg = self.part_motions.get(part)
        if not cfg:
            return None
        cands = cfg.get("candidates") or []
        if not cands:
            return None
        g, i, _w = random.choices(cands, weights=[c[2] for c in cands], k=1)[0]
        return (g, i)

    def pick_idle(self):
        if not self.idle_candidates:
            return None
        g, i, _w = random.choices(self.idle_candidates,
                                  weights=[c[2] for c in self.idle_candidates], k=1)[0]
        return (g, i)


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
        try:
            with open(model_json_path, "r", encoding="utf-8") as f:
                model_json_dict = json.load(f)
        except Exception as e:
            print(f"[ModelRegistry] 读取 model.json 失败 ({model_id}): {e}")
            return None
        if not isinstance(model_json_dict, dict):
            print(f"[ModelRegistry] model.json 格式异常 ({model_id})")
            return None

        profile = self._load_profile(model_dir)
        physics_path = self._resolve_physics(model_json_dict, model_dir, model_id)
        motion_groups = self._parse_motion_groups(model_json_dict)
        expressions = self._parse_expressions(model_json_dict)

        semantic = _guess_semantic_motions(motion_groups)
        semantic.update(profile.get("semantic_motions") or {})
        
        # 动作文件索引 + 逐动作时长（自动解析 mtn）
        file_map, duration_map = self._build_motion_index(model_json_dict, model_dir)

        # 部位候选 / idle 候选（文件名 -> 组+序号）
        resolved_parts = {}
        for part, cfg in (profile.get("part_motions") or {}).items():
            if not isinstance(cfg, dict):
                continue
            cands = self._resolve_candidates(cfg.get("candidates"), file_map, model_id)
            if cands:
                resolved_parts[part] = {"candidates": cands}
        idle_cands = self._resolve_candidates(profile.get("idle_candidates"), file_map, model_id)

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
            semantic_motions=semantic,
            motion_durations=profile.get("motion_durations") or {},
            head_keywords=profile.get("head_keywords") or DEFAULT_HEAD_KEYWORDS,
            body_keywords=profile.get("body_keywords") or DEFAULT_BODY_KEYWORDS,
            sound_map=profile.get("sound_map") or {},
            sound_dialog=profile.get("sound_dialog") or {},
            messages=profile.get("messages") or {},
            profile_name=profile.get("name"),
            profile_description=profile.get("description"),
            sound_dir=sound_dir,
            hit_mode=profile.get("hit_mode") or "parts",
            head_zones=[tuple(z) for z in (profile.get("head_zones") or [])],
            body_zones=[tuple(z) for z in (profile.get("body_zones") or [])],
            hit_rules=profile.get("hit_rules") or [],
            ignore_part_keywords=profile.get("ignore_part_keywords") or DEFAULT_IGNORE_KEYWORDS,
            veto_part_keywords=profile.get("veto_part_keywords") or DEFAULT_VETO_PART_KEYWORDS,
            part_motions=resolved_parts,
            idle_candidates=idle_cands,
            motion_duration_map=duration_map,
            motion_file_map=file_map,
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
        physics_rel = model_json_dict.get("physics")
        if isinstance(physics_rel, str) and physics_rel:
            candidate = os.path.join(model_dir, physics_rel)
            if os.path.exists(candidate):
                return candidate
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

    # ---------------- 动作索引 / 时长（新增） ----------------
    @staticmethod
    def _norm_path(rel: str) -> str:
        return (rel or "").replace("\\", "/").strip().lower()

    def _build_motion_index(self, model_json_dict, model_dir):
        file_map: Dict[str, tuple] = {}
        duration_map: Dict[str, List[float]] = {}
        motions = model_json_dict.get("motions", {})
        if not isinstance(motions, dict):
            return file_map, duration_map
        for group, mlist in motions.items():
            if not isinstance(mlist, list):
                continue
            durs = []
            for i, m in enumerate(mlist):
                if not isinstance(m, dict):
                    durs.append(0.0)
                    continue
                rel = m.get("file", "")
                file_map[self._norm_path(rel)] = (group, i)
                durs.append(self._read_mtn_duration(os.path.join(model_dir, rel)))
            duration_map[group] = durs
        return file_map, duration_map

    @staticmethod
    def _read_mtn_duration(path: str) -> float:
        """解析 Cubism2 .mtn 文本，返回时长(秒)；失败返回 0。"""
        try:
            fps = 30.0
            max_len = 0
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("$"):
                        k, _, v = line[1:].partition("=")
                        if k.strip().lower() == "fps":
                            try:
                                fps = float(v)
                            except ValueError:
                                pass
                        continue
                    if "=" not in line:
                        continue
                    _, _, vals = line.partition("=")
                    n = sum(1 for x in vals.split(",") if x.strip())
                    if n > max_len:
                        max_len = n
            if max_len > 1 and fps > 0:
                return (max_len - 1) / fps
        except Exception:
            pass
        return 0.0

    def _resolve_candidates(self, raw, file_map, model_id):
        out = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if isinstance(item, str):
                f, w = item, 1.0
            elif isinstance(item, dict):
                f = item.get("file", "")
                try:
                    w = float(item.get("weight", 1.0))
                except (TypeError, ValueError):
                    w = 1.0
            else:
                continue
            key = self._norm_path(f)
            if key in file_map:
                g, i = file_map[key]
                out.append((g, i, w))
            else:
                print(f"[ModelRegistry] {model_id}: 动作文件未找到 {f}")
        return out

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
