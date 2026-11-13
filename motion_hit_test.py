# -*- coding: utf-8 -*-
"""
motion_hit_test.py —— 独立动作预览 + 点击命中测试工具（不依赖主程序）
放置位置: 与 Live2D_Pet.py 同目录
用法:
    python motion_hit_test.py            # 默认 koharu
    python motion_hit_test.py shizuku    # 指定其他模型
操作:
    数字键 0~9 : 播放第 N 个动作（映射表见启动打印 / 报告文件）
    ← / →     : 上一个 / 下一个动作（超过10个动作时用这个）
    空格       : 重播上一个动作
    E         : 循环切换表情（若模型定义了表情）
    鼠标左键   : 打印命中部件（全部层 / 仅顶层 / 旧逻辑判定）
    ESC       : 退出并保存报告
说明:
    - 不做鼠标跟踪、不自动播 idle，动作观察不受干扰
    - 自动加载物理，与主程序观感一致
"""
import os
import sys
import json
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "Lib"))

import glfw
import live2d.v2 as live2d

# 与 model_registry.py 默认值保持一致（用于复现旧逻辑判定）
HEAD_KW = ["HAIR", "FACE", "EYE", "BROW", "MOUTH", "NOSE", "EAR", "HEAD"]
BODY_KW = ["UNIFORM", "BODY", "PAJAMA", "ARM", "NECK",
           "IRAIRA", "ASE", "KIRAKIRA", "SKETCH"]


# ==================== 模型定位 ====================
def locate_model(want=None):
    root = None
    for cand in (os.path.join(BASE_DIR, "live2d_models"),
                 os.path.join(os.path.dirname(BASE_DIR), "live2d_models")):
        if os.path.isdir(cand):
            root = cand
            break
    if root is None:
        sys.exit("[错误] 找不到 live2d_models 目录")
    candidates = []
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(".model.json"):
                candidates.append((entry, d, os.path.join(d, f)))
                break
    if not candidates:
        sys.exit("[错误] 未发现任何模型")
    if want:
        for c in candidates:
            if c[0].lower() == want.lower():
                return c
        sys.exit(f"[错误] 未找到模型 {want}，可用: {[c[0] for c in candidates]}")
    for c in candidates:
        if c[0].lower() == "koharu":
            return c
    return candidates[0]


# ==================== mtn 文本解析 ====================
def parse_mtn(path):
    info = {"file": os.path.basename(path), "ok": False, "fps": 30.0,
            "loop": False, "duration": 0.0, "meta": {},
            "params": {}, "visible": {}}
    if not os.path.exists(path):
        info["error"] = "文件不存在"
        return info
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        info["error"] = str(e)
        return info
    max_len = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):
            k, _, v = line[1:].partition("=")
            k, v = k.strip().lower(), v.strip()
            info["meta"][k] = v
            if k == "fps":
                try: info["fps"] = float(v)
                except ValueError: pass
            elif k == "loop":
                info["loop"] = (v.lower() == "true")
            continue
        if "=" not in line:
            continue
        name, _, values = line.partition("=")
        name = name.strip()
        try:
            vals = [float(x) for x in values.split(",") if x.strip() != ""]
        except ValueError:
            continue
        if not vals:
            continue
        max_len = max(max_len, len(vals))
        entry = {"n": len(vals), "min": min(vals), "max": max(vals),
                 "range": max(vals) - min(vals),
                 "first": vals[0], "last": vals[-1]}
        if name.upper().startswith("VISIBLE"):
            info["visible"][name] = entry
        else:
            info["params"][name] = entry
    if info["params"] or info["visible"]:
        info["ok"] = True
        if max_len > 1 and info["fps"] > 0:
            info["duration"] = (max_len - 1) / info["fps"]
    else:
        info["error"] = "未解析到参数曲线（可能非文本格式）"
    return info


def bucket_of(name):
    n = name.upper()
    if "EYE_BALL" in n or "EYEBALL" in n: return "眼球"
    if "EYE" in n:      return "眼睛"
    if "MOUTH" in n:    return "嘴巴"
    if "BROW" in n:     return "眉毛"
    if "CHEEK" in n:    return "脸颊"
    if "ARM" in n or "HAND" in n: return "手臂"
    if "HAIR" in n or "KAMIYURE" in n: return "头发"
    if any(k in n for k in ("BODY", "BREAST", "SKIRT", "NECKTIE")): return "躯干"
    if n.startswith("PARAM_ANGLE"): return "头部角度"
    if "BREATH" in n:   return "呼吸"
    return "其他"


def summarize(info):
    """按部位桶汇总活动量（幅度求和），并给出幅度最大的参数"""
    buckets = {}
    for name, e in info["params"].items():
        if e["range"] <= 1e-6:
            continue
        b = bucket_of(name)
        agg = buckets.setdefault(b, {"sum": 0.0, "count": 0})
        agg["sum"] += e["range"]
        agg["count"] += 1
    top = sorted(info["params"].items(),
                 key=lambda kv: kv[1]["range"], reverse=True)[:10]
    vis_changed = {k: v for k, v in info["visible"].items() if v["range"] > 1e-6}
    return buckets, top, vis_changed


# ==================== 旧逻辑判定复现 ====================
def old_verdict(hit_parts):
    for p in hit_parts:
        if any(k in p.upper() for k in HEAD_KW):
            return "头部(tap_head)"
    for p in hit_parts:
        if any(k in p.upper() for k in BODY_KW):
            return "身体(tap_body)"
    return "未命中→拖拽"


# ==================== 测试主程序 ====================
class MotionHitTest:
    def __init__(self, model_id, model_dir, json_path):
        self.model_id = model_id
        self.model_dir = model_dir
        with open(json_path, "r", encoding="utf-8") as f:
            self.mj = json.load(f)
        # 扁平动作表: [(组名, 组内index, 相对路径, 绝对路径)]
        self.flat = []
        for group, mlist in self.mj.get("motions", {}).items():
            for i, m in enumerate(mlist):
                rel = m.get("file", "")
                self.flat.append((group, i, rel, os.path.join(model_dir, rel)))
        self.parsed = [parse_mtn(p) for (_, _, _, p) in self.flat]
        self.expressions = [e.get("name") for e in self.mj.get("expressions", [])
                            if isinstance(e, dict) and e.get("name")]
        self.expr_idx = 0
        self.last_played = None
        self.play_start = 0.0
        self.hits = []
        self.finishes = []
        self.txt_path = os.path.join(BASE_DIR, f"motion_report_{model_id}.txt")
        self.json_path = os.path.join(BASE_DIR, f"motion_report_{model_id}.json")
        self.model = None
        self.window = None

    # ---------- 报告 ----------
    def write_report(self):
        L = []
        L.append("================ 动作分析报告 ================")
        L.append(f"模型: {self.model_id}   目录: {self.model_dir}")
        L.append(f"动作总数: {len(self.flat)}")
        L.append("")
        L.append("---- 按键映射表 ----")
        for i, (g, idx, rel, _) in enumerate(self.flat):
            key = str(i) if i < 10 else "←/→"
            L.append(f"  [{i:2d}] 键{key:>3}  组={g!r} index={idx}  {rel}")
        L.append("")
        for i, ((g, idx, rel, _), info) in enumerate(zip(self.flat, self.parsed)):
            L.append(f"---- [{i}] 组={g!r} index={idx}  {rel} ----")
            if not info["ok"]:
                L.append(f"  解析失败: {info.get('error')}")
                L.append("")
                continue
            L.append(f"  时长≈{info['duration']:.2f}s  fps={info['fps']:g}  "
                     f"loop={info['loop']}  活动参数数="
                     f"{sum(1 for e in info['params'].values() if e['range'] > 1e-6)}")
            buckets, top, vis = summarize(info)
            if buckets:
                bs = sorted(buckets.items(), key=lambda kv: kv[1]["sum"], reverse=True)
                L.append("  部位活动量: " + "  ".join(
                    f"{b}:{a['sum']:.1f}({a['count']}项)" for b, a in bs))
            L.append("  幅度Top参数:")
            for name, e in top:
                if e["range"] <= 1e-6:
                    continue
                L.append(f"    {name:<28} {e['min']:g} → {e['max']:g}   幅度 {e['range']:g}")
            if vis:
                L.append("  部位可见性切换: " + ", ".join(
                    f"{k}({v['first']:g}→{v['last']:g})" for k, v in vis.items()))
            L.append("")
        L.append("================ 人工观察记录（请填写后发回） ================")
        for i, (g, idx, rel, _) in enumerate(self.flat):
            L.append(f"  [{i:2d}] {g!r}/{idx} {os.path.basename(rel)} : ")
        L.append("")
        L.append("================ 点击命中日志（自动追加） ================")
        with open(self.txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        self.write_json()
        print(f"[报告] 已生成: {self.txt_path}")

    def write_json(self):
        data = {
            "model_id": self.model_id,
            "motions": [
                {"flat": i, "group": g, "index": idx, "file": rel, "analysis": info}
                for i, ((g, idx, rel, _), info) in enumerate(zip(self.flat, self.parsed))
            ],
            "hits": self.hits,
            "finishes": self.finishes,
        }
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def append_txt(self, text):
        with open(self.txt_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    # ---------- 播放 ----------
    def play(self, flat_idx):
        if not (0 <= flat_idx < len(self.flat)):
            return
        g, idx, rel, _ = self.flat[flat_idx]
        try:
            self.model.StartMotion(g, idx, priority=3,
                                   onFinishMotionHandler=self.on_finish)
            self.last_played = flat_idx
            self.play_start = time.time()
            msg = f"[播放] 键{flat_idx}  组={g!r} index={idx}  {rel}"
            print(msg)
            glfw.set_window_title(self.window,
                                  f"动作测试 {self.model_id} - {os.path.basename(rel)}")
        except Exception as e:
            print(f"[播放失败] {g!r}/{idx}: {e}")

    def on_finish(self, group, no):
        elapsed = time.time() - self.play_start
        self.finishes.append({"group": group, "index": no,
                              "elapsed": round(elapsed, 2)})
        print(f"[完成] 组={group!r} index={no}  实际播放 {elapsed:.2f}s")

    # ---------- 回调 ----------
    def on_key(self, window, key, scancode, action, mods):
        if action != glfw.PRESS:
            return
        if key == glfw.KEY_ESCAPE:
            glfw.set_window_should_close(window, True)
        elif glfw.KEY_0 <= key <= glfw.KEY_9:
            self.play(key - glfw.KEY_0)
        elif key == glfw.KEY_SPACE and self.last_played is not None:
            self.play(self.last_played)
        elif key == glfw.KEY_RIGHT:
            self.play(0 if self.last_played is None
                      else (self.last_played + 1) % len(self.flat))
        elif key == glfw.KEY_LEFT:
            self.play(len(self.flat) - 1 if self.last_played is None
                      else (self.last_played - 1) % len(self.flat))
        elif key == glfw.KEY_E:
            if not self.expressions:
                print("[表情] 该模型未定义表情")
                return
            name = self.expressions[self.expr_idx % len(self.expressions)]
            self.expr_idx += 1
            try:
                self.model.SetExpression(name)
                print(f"[表情] {name}")
            except Exception as e:
                print(f"[表情失败] {e}")

    def on_mouse(self, window, button, action, mods):
        if button != glfw.MOUSE_BUTTON_LEFT or action != glfw.PRESS:
            return
        x, y = glfw.get_cursor_pos(window)
        try:
            all_hits = self.model.HitPart(x, y, topOnly=False) or []
            top_hits = self.model.HitPart(x, y, topOnly=True) or []
        except Exception as e:
            print(f"[命中检测异常] {e}")
            return
        verdict = old_verdict(all_hits)
        rec = {"t": time.strftime("%H:%M:%S"), "x": round(x, 1), "y": round(y, 1),
               "all": list(all_hits), "top": list(top_hits), "old": verdict}
        self.hits.append(rec)
        line = (f"[点击] ({x:.0f},{y:.0f})  全部命中={list(all_hits)}  "
                f"仅顶层={list(top_hits)}  旧逻辑判定={verdict}")
        print(line)
        self.append_txt(line)

    def on_resize(self, window, w, h):
        if self.model and w > 0 and h > 0:
            self.model.Resize(w, h)

    # ---------- 运行 ----------
    def run(self):
        self.write_report()
        print("\n---- 按键映射 ----")
        for i, (g, idx, rel, _) in enumerate(self.flat):
            key = str(i) if i < 10 else "←/→"
            print(f"  [{i:2d}] 键{key:>3}  组={g!r} index={idx}  {rel}")
        print("  数字键播放 | ←/→切换 | 空格重播 | E表情 | 左键命中测试 | ESC退出\n")

        live2d.init()
        if not glfw.init():
            print("[错误] GLFW 初始化失败")
            return
        self.window = glfw.create_window(520, 680, f"动作测试 - {self.model_id}",
                                         None, None)
        if not self.window:
            glfw.terminate()
            return
        glfw.make_context_current(self.window)
        glfw.swap_interval(1)
        live2d.glInit()
        glfw.set_key_callback(self.window, self.on_key)
        glfw.set_mouse_button_callback(self.window, self.on_mouse)
        glfw.set_window_size_callback(self.window, self.on_resize)

        self.model = live2d.LAppModel()
        json_path = os.path.join(self.model_dir,
                                 next(f for f in os.listdir(self.model_dir)
                                      if f.lower().endswith(".model.json")))
        self.model.LoadModelJson(json_path)
        phys = self.mj.get("physics")
        if phys:
            p = os.path.join(self.model_dir, phys)
            if os.path.exists(p):
                self.model.loadPhysics(p)
                print("[物理] 已加载")
        try:
            import texture_premultiply
            texture_premultiply.premultiply_model_textures(
                self.model, self.mj, self.model_dir, force_reload=False)
        except Exception as e:
            print(f"[提示] 未启用纹理预乘（不影响动作测试）: {e}")
        try:
            self.model.SetAutoBlinkEnable(True)
            self.model.SetAutoBreathEnable(True)
        except Exception:
            pass
        self.model.Resize(520, 680)

        while not glfw.window_should_close(self.window):
            glfw.poll_events()
            live2d.clearBuffer(0.12, 0.12, 0.16, 1.0)
            self.model.Update()
            self.model.Draw()
            glfw.swap_buffers(self.window)

        # 退出: 保存实际时长 + 完整 JSON
        if self.finishes:
            self.append_txt("\n================ 实际播放时长（完成回调） ================")
            for fin in self.finishes:
                self.append_txt(f"  组={fin['group']!r} index={fin['index']}  "
                                f"{fin['elapsed']}s")
        self.write_json()
        print(f"\n[保存] {self.txt_path}")
        print(f"[保存] {self.json_path}")
        live2d.dispose()
        glfw.terminate()


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else None
    mid, mdir, jpath = locate_model(want)
    print(f"[模型] {mid}  →  {jpath}")
    MotionHitTest(mid, mdir, jpath).run()
