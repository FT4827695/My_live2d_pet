import os
import base64
import re
import threading
import sys
import copy
import glob
from PIL import Image
import multiprocessing as mp
import queue

from capabilities import match_capabilities
from response_stats import (
    inject_stream_options,
    capture_usage_from_chunk,
    probe_kv_cache_usage,
)


def _get_temperature_from_env() -> float:
    try:
        return float(os.environ.get("XLLAMA_TEMPERATURE", "0.7"))
    except Exception:
        return 0.7


def _get_enable_thinking_from_env() -> bool:
    value = os.environ.get("XLLAMA_ENABLE_THINKING", "0").strip().lower()
    return value in ("1", "true", "yes", "on")


_GGML_TYPE_MAP = {
    "f32": 0, "f16": 1, "q4_0": 2, "q4_1": 3, "q5_0": 6, "q5_1": 7,
    "q8_0": 8, "q8_1": 9, "q2_k": 10, "q3_k": 11, "q4_k": 12, "q5_k": 13,
    "q6_k": 14, "q8_k": 15, "iq4_nl": 20,
}


def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


def get_available_vram_mb():
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            free_mb = max(g.memoryFree for g in gpus)
            return free_mb, "NVIDIA (GPUtil 实时)"
    except Exception:
        pass

    try:
        import subprocess, csv, io
        cmd = ['wmic', 'path', 'win32_VideoController',
               'get', 'Name,AdapterRAM', '/format:csv']
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        raw = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, startupinfo=si)

        text = None
        for enc in ('utf-8-sig', 'utf-8', 'gbk', 'latin-1'):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if text is None:
            text = raw.decode('utf-8', errors='ignore')

        has_dedicated = False
        max_vram_mb = 0

        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get('Name') or '').lower()
            if any(k in name for k in
                   ["nvidia", "radeon", "geforce", "rtx", "gtx",
                    "rx", "arc", "quadro"]):
                has_dedicated = True

            try:
                v = int(row.get('AdapterRAM', 0))
                if v > 0:
                    max_vram_mb = max(max_vram_mb, v / (1024 ** 2))
            except (ValueError, TypeError):
                pass

        if has_dedicated:
            if max_vram_mb == 0 or (4000 <= max_vram_mb <= 4300):
                max_vram_mb = _read_vram_from_registry() or 8192
            return max_vram_mb * 0.70, "WMI 独显估算"

    except Exception as e:
        print(f"[显存探测] WMI 异常: {e}")

    try:
        import psutil
        free_ram_mb = psutil.virtual_memory().available / (1024 ** 2)
        return free_ram_mb * 0.50, "核显/APU 系统内存折算"
    except Exception:
        pass

    return 2048, "兜底 2GB"


def _read_vram_from_registry():
    try:
        import winreg
        base = (r"SYSTEM\CurrentControlSet\Control\Class"
                r"{4d36e968-e325-11ce-bfc1-08002be10318}")
        best = 0

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as cls:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, i)
                    i += 1
                except OSError:
                    break

                try:
                    with winreg.OpenKey(cls, sub) as sk:
                        for vname in ("HardwareInformation.qwMemorySize",
                                      "QWMemorySize"):
                            try:
                                val, typ = winreg.QueryValueEx(sk, vname)
                                if typ == winreg.REG_QWORD and val > 0:
                                    best = max(best, val / (1024 ** 2))
                                    break
                            except FileNotFoundError:
                                continue
                except OSError:
                    continue

        return best if best > 0 else None
    except Exception:
        return None


# ================================================================
# 子进程引擎
# ================================================================
class _RealXllamaEngine:

    def __init__(self, abort_event):
        self.abort_event = abort_event
        self.base_dir = get_base_dir()
        self.models_dir = os.path.join(self.base_dir, "models")
        self.model_path = None
        self.mmproj_path = None
        self.server = None
        self.lock = threading.Lock()

        self._auto_detect_models()

        if self.model_path is None:
            print("[Worker] 未找到主模型文件")

    # ----------------------------------------------------------
    def _auto_detect_models(self):
        if not os.path.isdir(self.models_dir):
            os.makedirs(self.models_dir, exist_ok=True)
            return

        gguf_files = glob.glob(os.path.join(self.models_dir, "*.gguf"))
        if not gguf_files:
            return

        mmproj_candidates = []
        main_candidates = []

        for f in gguf_files:
            if "mmproj" in os.path.basename(f).lower():
                mmproj_candidates.append(f)
            else:
                main_candidates.append(f)

        if main_candidates:
            self.model_path = max(main_candidates, key=os.path.getsize)
        else:
            remaining = [f for f in gguf_files if f not in mmproj_candidates]
            if remaining:
                self.model_path = max(remaining, key=os.path.getsize)

        if mmproj_candidates:
            self.mmproj_path = max(mmproj_candidates, key=os.path.getsize)

        if self.model_path:
            print(f"[Worker] 主模型：{os.path.basename(self.model_path)}")
        if self.mmproj_path:
            print(f"[Worker] 视觉模块：{os.path.basename(self.mmproj_path)}")

    # ----------------------------------------------------------
    def start_server(self, use_gpu=False, use_mmproj=True):
        n_ctx = int(os.environ.get("XLLAMA_CTX", 8192))
        self.max_gen_tokens = max(256, int(n_ctx * 0.4))

        if self.model_path is None or not os.path.exists(self.model_path):
            return False

        if not use_mmproj:
            self.mmproj_path = None
            print("[Worker] 纯文本模式：跳过视觉模块 (mmproj)")
        elif self.mmproj_path and not os.path.exists(self.mmproj_path):
            self.mmproj_path = None

        try:
            import xllamacpp as xlc
        except ImportError as e:
            print(f"[Worker] xllamacpp 未安装: {e}")
            return False

        try:
            params = self._build_params(xlc, use_gpu=use_gpu)
            self.server = xlc.Server(params)

            if params.n_gpu_layers == 0:
                mode_desc = "CPU"
            elif params.n_gpu_layers == -1:
                mode_desc = "GPU (全量)"
            else:
                mode_desc = f"GPU (智能 {params.n_gpu_layers} 层)"

            mm_desc = "多模态" if self.mmproj_path else "纯文本"

            print(f"[Worker] 模型加载成功！({mode_desc} / {mm_desc}, "
                  f"CTX={params.n_ctx})")
            return True

        except Exception as e:
            print(f"[Worker] 模型加载失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ----------------------------------------------------------
    def _build_params(self, xlc, use_gpu):
        media_dir = self.base_dir
        if not os.path.exists(media_dir):
            os.makedirs(media_dir, exist_ok=True)

        params = xlc.CommonParams()
        params.verbosity = 1
        params.model.path = self.model_path

        if hasattr(params, "use_jinja"):
            params.use_jinja = True
            print("[优化] 已开启 Jinja 模板引擎")

        params.n_ctx = int(os.environ.get("XLLAMA_CTX", 8192))
        params.n_batch = int(os.environ.get("XLLAMA_BATCH", 512))

        if hasattr(params, "cache_prompt"):
            params.cache_prompt = True
            print("[优化] Prefix Caching 已显式开启")

        spec_mode = os.environ.get("XLLAMA_SPEC_MODE", "off").strip().lower()

        if spec_mode != "off":
            n_parallel = 1
            print(f"[并行] 投机解码已开启 ({spec_mode})，parallel = 1")
        else:
            try:
                n_parallel = max(1, int(os.environ.get("XLLAMA_PARALLEL", "1")))
            except Exception:
                n_parallel = 1
            print(f"[并行] 并行 slot = {n_parallel}")

        if hasattr(params, "n_parallel"):
            params.n_parallel = n_parallel

        # ==========================================================
        # KV Cache 压缩设置
        # ==========================================================
        kv_cache_str = os.environ.get("XLLAMA_KV_CACHE", "q8_0").strip().lower()
        kv_int = _GGML_TYPE_MAP.get(kv_cache_str)
        _VULKAN_UNSUPPORTED_KV = {"q2_k", "q3_k", "q4_k", "q5_k", "q6_k", "q8_k"}

        if kv_cache_str == "q8_iq4":
            kv_int_k = _GGML_TYPE_MAP.get("q8_0")     # 8
            kv_int_v = _GGML_TYPE_MAP.get("iq4_nl")   # 20
        else:
            if kv_cache_str in _VULKAN_UNSUPPORTED_KV:
                print(f"[KV] {kv_cache_str} 在 Vulkan 后端不支持 SET_ROWS，"
                      f"自动回退到 q8_0")
                kv_cache_str = "q8_0"
            kv_int_k = kv_int_v = _GGML_TYPE_MAP.get(kv_cache_str)

        if kv_int_k is not None and kv_int_v is not None:
            try:
                params.cache_type_k = kv_int_k
                params.cache_type_v = kv_int_v

                if kv_cache_str == "q8_iq4":
                    print("[KV] KV Cache 压缩格式: 混合模式 (K=q8_0, V=iq4_nl)")
                else:
                    print(f"[KV] KV Cache 压缩格式: {kv_cache_str}")

            except Exception as e:
                print(f"[KV] 设置 KV Cache 失败: {e}，回退默认 f16")
                params.cache_type_k = 1
                params.cache_type_v = 1
                kv_int_k = kv_int_v = 1
        else:
            print(f"[KV] 未知的 KV Cache 类型: {kv_cache_str}，使用默认 f16 (1)")
            params.cache_type_k = 1
            params.cache_type_v = 1
            kv_int_k = kv_int_v = 1

        # ==========================================================
        # Flash Attention 优化设置
        # ==========================================================
        flash_attn_env = os.environ.get("XLLAMA_FLASH_ATTN", "1").strip().lower()
        enable_flash_attn = flash_attn_env in ("1", "true", "yes", "on")

        if enable_flash_attn:
            if hasattr(params, "flash_attn_type"):
                try:
                    flash_attn_type = int(
                        os.environ.get("XLLAMA_FLASH_ATTN_TYPE", "-1").strip()
                    )
                except Exception:
                    flash_attn_type = -1

                try:
                    params.flash_attn_type = flash_attn_type
                    print(f"[优化] 已开启 Flash Attention (flash_attn_type={flash_attn_type})")
                except Exception as e:
                    print(f"[优化] 设置 flash_attn_type 失败: {e}")
            else:
                print("[优化] 未找到 flash_attn_type 字段，跳过 Flash Attention")
        else:
            if hasattr(params, "flash_attn_type"):
                try:
                    params.flash_attn_type = 0
                    print("[优化] Flash Attention 已手动关闭")
                except Exception:
                    pass

        if use_gpu:
            # ============ 智能分层逻辑 ============
            try:
                from xllamacpp import estimate_gpu_layers, get_device_info

                try:
                    gpu_devices = get_device_info()
                except Exception:
                    gpu_devices = []

                if not isinstance(gpu_devices, list) or not gpu_devices:
                    gpu_devices = [{"name": "Vulkan0"}]

                free_mb, source = get_available_vram_mb()
                mem_free_bytes = int(free_mb * 1024 * 1024)

                reserved_bytes = 256 * 1024 * 1024

                try:
                    extra_mb = int(os.environ.get("XLLAMA_VRAM_EXTRA_MB", "0"))
                except Exception:
                    extra_mb = 0
                extra_bytes = extra_mb * 1024 * 1024

                actual_free = max(
                    0, mem_free_bytes - reserved_bytes + extra_bytes
                )

                device_name = gpu_devices[0].get("name", "Vulkan0")
                devices = [{
                    "name": device_name,
                    "memory_free": actual_free,
                    "memory_min": 256 * 1024 * 1024,
                }]

                projectors = [self.mmproj_path] if self.mmproj_path else []

                print(f"[智能加载] 显存来源: {source} | "
                      f"可用: {free_mb:.0f} MB，正在计算最优层数...")

                _KV_ESTIMATE_MAP = {
                    1: "f16",
                    8: "q8_0",
                    2: "q4_0",
                    20: "q4_0",
                }

                kv_estimate_type = _KV_ESTIMATE_MAP.get(kv_int_k, "f16")
                if kv_cache_str == "q8_iq4":
                    kv_estimate_type = "q4_0"

                est_layers = 0
                est_vram = 0

                est = estimate_gpu_layers(
                    devices, self.model_path,
                    projectors, context_length=params.n_ctx,
                    batch_size=params.n_batch, num_parallel=n_parallel,
                    kv_cache_type=kv_estimate_type,
                )

                est_layers = est.layers
                est_vram = est.vram_size

                if est_layers > 0:
                    params.n_gpu_layers = est_layers
                    vram_gb = est_vram / (1024 ** 3)
                else:
                    print("[智能加载] 层数为 0，显存严重不足，"
                          "自动降级为纯 CPU。")
                    params.n_gpu_layers = 0

            except ImportError:
                print("[智能加载] estimate_gpu_layers 不可用，"
                      "回退到全量 GPU (-1)")
                params.n_gpu_layers = -1
            except Exception as e:
                print(f"[智能加载] 估算异常 ({e})，"
                      f"安全兜底：回退到全量 GPU (-1)")
                params.n_gpu_layers = -1
            # ======================================
        else:
            params.n_gpu_layers = 0
            print("[加载] 纯 CPU 模式 (n_gpu_layers = 0)")

        # ===== 视觉模块联动 =====
        if self.mmproj_path:
            params.mmproj.path = self.mmproj_path
            params.mmproj_use_gpu = (params.n_gpu_layers != 0)
            params.image_min_tokens = 1024

        params.media_path = os.path.join(media_dir, '')
        params.n_predict = -1

        if spec_mode != "off" and hasattr(params, "speculative"):
            spec = params.speculative

            try:
                n_draft = int(os.environ.get("XLLAMA_SPEC_DRAFT", "1"))

                if spec_mode == "mtp":
                    if hasattr(xlc, "common_speculative_type"):
                        mtp_type = getattr(
                            xlc.common_speculative_type,
                            "COMMON_SPECULATIVE_TYPE_DRAFT_MTP",
                            3
                        )
                        spec.types = [mtp_type]

                    if hasattr(spec, "draft"):
                        spec.draft.n_max = n_draft
                        spec.draft.n_min = 0

                    draft_kv = os.environ.get("XLLAMA_SPEC_DRAFT_KV", "q8_0").strip().lower()

                    if draft_kv in _VULKAN_UNSUPPORTED_KV:
                        print(f"[投机解码] draft KV {draft_kv} 在 Vulkan 后端"
                              f"不支持 SET_ROWS，回退 q8_0")
                        draft_kv = "q8_0"

                    draft_kv_int = _GGML_TYPE_MAP.get(draft_kv)

                    if draft_kv_int is not None and hasattr(spec.draft, "cache_type_k"):
                        try:
                            spec.draft.cache_type_k = draft_kv_int
                            spec.draft.cache_type_v = draft_kv_int
                            print(f"[投机解码] Draft KV Cache 压缩格式: {draft_kv}")
                        except Exception as e:
                            print(f"[投机解码] 设置 draft KV 类型失败: {e}")

                    print(f"[投机解码] 已开启内置 MTP 模式 (draft={n_draft})")

            except Exception as e:
                print(f"[投机解码] 配置失败: {e}")
                import traceback
                traceback.print_exc()

        return params

    # ----------------------------------------------------------
    def stop_server(self):
        if self.server is not None:
            self.server = None
            import gc
            gc.collect()
            print("[Worker] 模型已卸载")

    # ----------------------------------------------------------
    def _resize_image(self, img, max_size=1024):
        if max(img.size) <= max_size:
            return img

        ratio = max_size / max(img.size)

        return img.resize(
            (int(img.size[0] * ratio), int(img.size[1] * ratio)),
            Image.Resampling.LANCZOS
        )

    def _encode_pil_image(self, pil_img, max_size=1024):
        from io import BytesIO

        pil_img = self._resize_image(pil_img, max_size)
        converted = pil_img.convert("RGB")
        buffer = BytesIO()

        try:
            converted.save(buffer, format="JPEG", quality=85)
            data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{data}"
        finally:
            buffer.close()
            if converted is not pil_img:
                converted.close()

    def _build_multimodal_content(self, text, image_paths):
        if not image_paths:
            return text

        if isinstance(image_paths, (str, Image.Image)):
            image_paths = [image_paths]

        content = []

        for img in image_paths:
            try:
                if isinstance(img, str):
                    if not os.path.exists(img):
                        continue
                    with Image.open(img) as pil_img:
                        data_uri = self._encode_pil_image(pil_img)
                elif isinstance(img, Image.Image):
                    data_uri = self._encode_pil_image(img)
                else:
                    continue

                content.append({
                    "type": "image_url",
                    "image_url": {"url": data_uri}
                })

            except Exception as e:
                print(f"[Worker] 图片处理失败: {e}")
                continue

        if not content:
            return text

        content.append({"type": "text", "text": text})
        return content

    def _clean_reply(self, raw_text):
        if not raw_text:
            return ""

        return re.sub(
            r'(srv\s+.*?update_slots.*|srv\s+.*?params.*|'
            r'llama_.*?timings.*|slot\s+\d+.*)',
            '', raw_text, flags=re.IGNORECASE | re.MULTILINE
        ).strip()

    # ----------------------------------------------------------
    # enable_thinking 兼容处理
    # ----------------------------------------------------------
    def _build_chat_payload(self, msgs, enable_thinking, include_thinking_kwarg, tools=None):
        payload = {
            "messages": msgs,
            "max_tokens": getattr(self, 'max_gen_tokens', 2048),
            "temperature": _get_temperature_from_env(),
            "stream": True,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False

        if include_thinking_kwarg:
            payload["chat_template_kwargs"] = {
                "enable_thinking": bool(enable_thinking)
            }

        inject_stream_options(payload)
        return payload

    def _build_thinking_fallback_payloads(self, msgs, tools=None):
        enable = _get_enable_thinking_from_env()

        payloads = [
            self._build_chat_payload(msgs, enable, True, tools),
            self._build_chat_payload(msgs, enable, False, tools),
            self._build_chat_payload(msgs, not enable, True, tools),
        ]

        return payloads

    def _run_chat_completion_with_fallback(self, msgs, tools=None):
        payloads = self._build_thinking_fallback_payloads(msgs, tools)

        for idx, payload in enumerate(payloads):
            if self.abort_event.is_set():
                return {"content": "", "reasoning_content": "", "tool_calls": None}, True

            collected_text = []
            collected_reasoning = []
            tool_calls_map = {}
            abort_triggered = False
            usage_holder = {}

            def stream_callback(chunk):
                nonlocal abort_triggered

                if self.abort_event.is_set():
                    abort_triggered = True
                    return True

                capture_usage_from_chunk(chunk, usage_holder)

                try:
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})

                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            collected_reasoning.append(reasoning)

                        content = delta.get("content", "")
                        if content:
                            collected_text.append(content)

                        delta_tool_calls = delta.get("tool_calls")
                        if delta_tool_calls:
                            for tc_chunk in delta_tool_calls:
                                tc_idx = tc_chunk.get("index", 0)

                                if tc_idx not in tool_calls_map:
                                    tool_calls_map[tc_idx] = {
                                        "id": tc_chunk.get("id", f"call_{tc_idx}"),
                                        "type": "function",
                                        "function": {
                                            "name": tc_chunk.get("function", {}).get("name", "") or "",
                                            "arguments": tc_chunk.get("function", {}).get("arguments", "") or ""
                                        }
                                    }
                                else:
                                    func_chunk = tc_chunk.get("function", {})

                                    if func_chunk.get("name"):
                                        tool_calls_map[tc_idx]["function"]["name"] += func_chunk["name"]

                                    if func_chunk.get("arguments"):
                                        tool_calls_map[tc_idx]["function"]["arguments"] += func_chunk["arguments"]

                except Exception:
                    pass

                return False

            try:
                with self.lock:
                    self.server.handle_chat_completions(payload, stream_callback)

                result = {
                    "content": "".join(collected_text).strip(),
                    "reasoning_content": "".join(collected_reasoning).strip(),
                    "tool_calls": list(tool_calls_map.values()) if tool_calls_map else None,
                    "usage": usage_holder.get("usage"),
                }

                return result, abort_triggered

            except Exception as e:
                if collected_text or abort_triggered or self.abort_event.is_set():
                    result = {
                        "content": "".join(collected_text).strip(),
                        "reasoning_content": "".join(collected_reasoning).strip(),
                        "tool_calls": list(tool_calls_map.values()) if tool_calls_map else None,
                        "usage": usage_holder.get("usage"),
                    }
                    return result, abort_triggered

                if idx < len(payloads) - 1:
                    print(f"[Worker] enable_thinking 兼容回退 {idx + 1}: {e}")
                    continue

                print(f"[Worker] 推理请求失败: {e}")

        return {"content": "", "reasoning_content": "", "tool_calls": None, "usage": None}, False

    def _run_stream_completion_with_fallback(self, req_id, res_queue, msgs):
        payloads = self._build_thinking_fallback_payloads(msgs)
        usage_holder = {}

        for idx, payload in enumerate(payloads):
            if self.abort_event.is_set():
                return usage_holder.get("usage")

            emitted = False

            def stream_callback(chunk):
                nonlocal emitted

                if self.abort_event.is_set():
                    return True

                capture_usage_from_chunk(chunk, usage_holder)

                try:
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            res_queue.put((req_id, "chunk", content))
                            emitted = True
                except Exception:
                    pass

                return False

            try:
                with self.lock:
                    self.server.handle_chat_completions(payload, stream_callback)

                return usage_holder.get("usage")

            except Exception as e:
                if emitted or self.abort_event.is_set():
                    return usage_holder.get("usage")

                if idx < len(payloads) - 1:
                    print(f"[Worker] enable_thinking 兼容回退 {idx + 1}: {e}")
                    continue

                print(f"[Worker] 流式推理请求失败: {e}")
                return usage_holder.get("usage")

    # ----------------------------------------------------------
    def generate(self, messages, image_paths=None, tools=None, return_raw_dict=False):
        if self.server is None:
            return None

        msgs = copy.deepcopy(messages)

        if image_paths and self.mmproj_path:
            if isinstance(image_paths, str):
                image_paths = [image_paths]

            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    original_content = msgs[i].get("content")

                    if isinstance(original_content, str):
                        multimodal_content = self._build_multimodal_content(
                            original_content, image_paths
                        )

                        if isinstance(multimodal_content, list):
                            msgs[i]["content"] = multimodal_content

                    break

        elif image_paths and not self.mmproj_path:
            print("[Worker] 当前模型不支持视觉输入，已忽略图片")

        self.abort_event.clear()

        resp_dict, abort_triggered = self._run_chat_completion_with_fallback(msgs, tools)

        resp_dict["content"] = self._clean_reply(resp_dict.get("content", ""))
        resp_dict["kv_cache_used"] = probe_kv_cache_usage(self.server)

        if abort_triggered:
            if return_raw_dict:
                resp_dict["content"] = resp_dict["content"] if resp_dict["content"] else "（放弃思考）"
                return resp_dict
            return resp_dict["content"] if resp_dict["content"] else "（放弃思考）"

        if not resp_dict["content"] and not resp_dict.get("tool_calls"):
            if return_raw_dict:
                resp_dict["content"] = "（模型未返回内容）"
                return resp_dict
            return "（模型未返回内容）"

        if return_raw_dict:
            return resp_dict

        return resp_dict["content"]

    def generate_stream_to_queue(self, req_id, res_queue, messages, image_paths=None):
        if self.server is None:
            res_queue.put((req_id, "done", ""))
            return

        msgs = copy.deepcopy(messages)

        if image_paths and self.mmproj_path:
            if isinstance(image_paths, str):
                image_paths = [image_paths]

            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    original_content = msgs[i].get("content")

                    if isinstance(original_content, str):
                        multimodal_content = self._build_multimodal_content(
                            original_content, image_paths
                        )

                        if isinstance(multimodal_content, list):
                            msgs[i]["content"] = multimodal_content

                    break

        elif image_paths and not self.mmproj_path:
            print("[Worker] 当前模型不支持视觉输入，已忽略图片")

        self.abort_event.clear()

        usage = None

        try:
            usage = self._run_stream_completion_with_fallback(
                req_id,
                res_queue,
                msgs,
            )
        except Exception as e:
            print(f"[Worker] 流式推理错误: {e}")
        finally:
            kv = probe_kv_cache_usage(self.server)
            res_queue.put((req_id, "usage", {"usage": usage, "kv_cache_used": kv}))
            res_queue.put((req_id, "done", ""))


# ================================================================
# 子进程入口
# ================================================================
def _engine_worker_process(cmd_queue, res_queue,
                           use_gpu, use_mmproj, abort_event):
    try:
        engine = _RealXllamaEngine(abort_event)
        success = engine.start_server(use_gpu=use_gpu, use_mmproj=use_mmproj)
        mmproj_path = engine.mmproj_path if success else None

        capabilities = {}

        if success and engine.model_path:
            model_name = os.path.basename(engine.model_path)
            capabilities = match_capabilities(model_name)
            print(f"[Worker] 模型能力: {capabilities}")

        res_queue.put((0, {
            "success": success,
            "mmproj_path": mmproj_path,
            "capabilities": capabilities,
        }))

        if not success:
            return

        while True:
            try:
                task = cmd_queue.get()

                if task is None:
                    break

                cmd = task[0]

                if cmd == 'generate':
                    req_id = task[1]
                    args = task[2:]
                    res = engine.generate(*args)
                    res_queue.put((req_id, res))

                elif cmd == 'generate_stream':
                    req_id = task[1]
                    args = task[2:]
                    engine.generate_stream_to_queue(req_id, res_queue, *args)

                elif cmd == 'stop':
                    engine.stop_server()
                    break

            except Exception as e:
                print(f"[Worker] 任务异常: {e}")
                res_queue.put(
                    (task[1] if len(task) > 1 else 0, None))

    except Exception as e:
        print(f"[Worker] 致命异常: {e}")

        try:
            res_queue.put((0, False))
        except Exception:
            pass


# ================================================================
# 主进程代理
# ================================================================
class XllamaEngine:

    def __init__(self):
        self.ctx = mp.get_context('spawn')
        self.cmd_queue = self.ctx.Queue()
        self.res_queue = self.ctx.Queue()
        self.abort_event = self.ctx.Event()
        self.process = None
        self._is_ready = False
        self._req_lock = threading.Lock()
        self._req_counter = 0
        self.capabilities = {}
        self.last_stream_usage = None

    @property
    def server(self):
        return "ProxyServer" if self._is_ready else None

    def start_server(self, use_gpu=False, use_mmproj=True):
        if self.process is not None and self.process.is_alive():
            return True

        self.cmd_queue = self.ctx.Queue()
        self.res_queue = self.ctx.Queue()
        self.abort_event = self.ctx.Event()

        self.process = self.ctx.Process(
            target=_engine_worker_process,
            args=(self.cmd_queue, self.res_queue,
                  use_gpu, use_mmproj, self.abort_event),
            daemon=True,
        )
        self.process.start()

        try:
            item = self.res_queue.get(timeout=300)

            if isinstance(item, tuple) and len(item) >= 2:
                data = item[1]

                if isinstance(data, dict):
                    success = data.get("success", False)
                    self.mmproj_path = data.get("mmproj_path")
                    self.capabilities = data.get("capabilities", {}) or {}
                else:
                    success = bool(data)
                    self.mmproj_path = None
                    self.capabilities = {}
            else:
                success = False
                self.mmproj_path = None
                self.capabilities = {}

            self._is_ready = bool(success)

            if self._is_ready:
                print(f"[XllamaEngine] 能力标志: {self.capabilities}")

            return self._is_ready

        except queue.Empty:
            print("[XllamaEngine] 子进程加载超时")
            self.stop_server()
            return False

        except Exception as e:
            print(f"[XllamaEngine] 等待子进程异常: {e}")
            return False

    def generate(self, messages, image_paths=None, tools=None, return_raw_dict=False):
        if not self._is_ready:
            return None

        with self._req_lock:
            self._req_counter += 1
            req_id = self._req_counter

            self.cmd_queue.put(('generate', req_id, messages, image_paths, tools, return_raw_dict))

            while True:
                try:
                    item = self.res_queue.get(timeout=120)

                    if item is None:
                        continue

                    if len(item) == 3:
                        continue

                    res_req_id, result = item

                    if res_req_id == req_id:
                        return result

                except queue.Empty:
                    return None

    def generate_stream(self, messages, image_paths=None):
        if not self._is_ready:
            return

        self.last_stream_usage = None

        with self._req_lock:
            self._req_counter += 1
            req_id = self._req_counter

        self.cmd_queue.put(('generate_stream', req_id, messages, image_paths))

        while True:
            try:
                item = self.res_queue.get(timeout=120)
            except queue.Empty:
                break

            if item is None:
                continue

            if len(item) != 3:
                continue

            res_req_id, msg_type, data = item

            if res_req_id != req_id:
                continue

            if msg_type == 'chunk':
                yield data
            elif msg_type == 'usage':
                self.last_stream_usage = data
            elif msg_type == 'done':
                break

    def abort_generation(self):
        if self._is_ready:
            self.abort_event.set()

    def supports_function_calling(self) -> bool:
        """
        当前策略：默认所有模型支持 Function Calling。
        只有 capabilities 明确返回 function_calling=False 时才禁用。
        """
        return bool(self.capabilities.get("function_calling", True))

    def stop_server(self):
        self._is_ready = False
        self.capabilities = {}

        proc = self.process
        cmd_q = self.cmd_queue
        res_q = self.res_queue
        self.process = None

        if proc is not None and proc.is_alive():
            try:
                cmd_q.put(('stop', 0, None))
            except Exception:
                pass

            threading.Thread(
                target=self._async_cleanup,
                args=(proc, cmd_q, res_q),
                daemon=True,
            ).start()
        else:
            self._drain_queue(cmd_q)
            self._drain_queue(res_q)

    def _async_cleanup(self, proc, cmd_q, res_q):
        try:
            proc.join(timeout=10)

            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3)

        except Exception:
            pass

        self._drain_queue(cmd_q)
        self._drain_queue(res_q)

    @staticmethod
    def _drain_queue(q):
        try:
            while not q.empty():
                q.get_nowait()
        except Exception:
            pass

    @staticmethod
    def _clean_reply(raw_text):
        if not raw_text:
            return ""

        return re.sub(
            r'(srv\s+.*?update_slots.*|srv\s+.*?params.*|'
            r'llama_.*?timings.*|slot\s+\d+.*)',
            '', raw_text, flags=re.IGNORECASE | re.MULTILINE
        ).strip()
