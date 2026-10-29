import os
import sys
import re
import io
import json
import ast
import subprocess
import contextlib
import pandas as pd
import win32clipboard as wc
import win32con
import math
import numpy as np
from typing import Union, Any
from rag_utils import AttachmentCachePool
import time

from prompt import get_character_name
from response_stats import ResponseStats, estimate_text_tokens, estimate_messages_tokens

ASSISTANT_NAME = get_character_name()
ASSISTANT_PREFIX = ASSISTANT_NAME + ": "


# ================== 0. 代码执行安全预检 =================

_PANDAS_IO_BLOCKLIST = {
    "read_csv", "read_json", "read_html", "read_fwf",
    "read_clipboard", "read_table", "read_xml", "read_sql",
    "to_csv", "to_json", "to_pickle", "to_xml",
}


class _PandasIOVisitor(ast.NodeVisitor):
    def __init__(self):
        self.violations = []

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _PANDAS_IO_BLOCKLIST:
            if isinstance(func.value, ast.Name) and func.value.id in (
                "pd", "df", "df1", "df2", "dfs"
            ):
                if node.args:
                    arg0 = node.args[0]
                    if not isinstance(arg0, ast.Constant):
                        self.violations.append(
                            f"第 {node.lineno} 行：{func.attr} 的第一个参数不是字面量，"
                            f"禁止动态构造路径/URL。"
                        )
                    elif isinstance(arg0.value, str):
                        v = arg0.value
                        if "://" in v or (len(v) >= 2 and v[1] == ":"):
                            self.violations.append(
                                f"第 {node.lineno} 行：{func.attr} 不允许接收 URL 或绝对路径。"
                            )
        self.generic_visit(node)


# ================== 1. 剪贴板服务 =================
class ClipboardService:

    @staticmethod
    def get_text() -> str:
        try:
            wc.OpenClipboard()
            data = wc.GetClipboardData(win32con.CF_UNICODETEXT)
            wc.CloseClipboard()
            return data if data else ""
        except Exception:
            return ""

    @staticmethod
    def set_text(text: str):
        try:
            wc.OpenClipboard()
            wc.EmptyClipboard()
            wc.SetClipboardText(text)
            wc.CloseClipboard()
        except Exception:
            pass

    @staticmethod
    def get_files() -> list:
        try:
            if not wc.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return []
            wc.OpenClipboard()
            try:
                data = wc.GetClipboardData(win32con.CF_HDROP)
            finally:
                wc.CloseClipboard()
            if not data:
                return []
            return [str(p) for p in data if p]
        except Exception:
            return []

# ================== 2. 文本工具 =================
class TextUtils:

    @staticmethod
    def clean_text(text: str) -> str:
        if not text:
            return ""

        text = text.encode('utf-8', 'replace').decode('utf-8')
        text = re.sub(
            r'[\x00-\x09\x0B-\x1F\x7F-\x9F\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff\u3000]',
            '',
            text
        )

        return text.replace('​', '').replace('□', '').replace('\r', '')

    @staticmethod
    def extract_code_block(text: str):
        if not text:
            return None

        match = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)

        if not match:
            match = re.search(r"```\s*(.*?)```", text, re.DOTALL)

        if match:
            code = match.group(1).strip()
            return code if code else None

        return None

    @staticmethod
    def wrap_text_to_lines(text: str, max_width: float, get_width_func) -> list:
        if not text:
            return [""]

        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = []

        for para in text.split('\n'):
            if not para:
                lines.append("")
                continue

            cur = ""

            for ch in para:
                test = cur + ch

                if get_width_func(test) > max_width and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur = test

            if cur:
                lines.append(cur)

        return lines if lines else [""]

    @staticmethod
    def get_message_lines(sender, text, max_width, get_width_func):
        if sender == "user":
            prefix = "我: "
        elif sender == "stats":
            prefix = ""
        else:
            prefix = ASSISTANT_PREFIX

        text = TextUtils.clean_text(text).replace('\r\n', '\n').replace('\r', '\n')
        full = prefix + text
        lines = []
        last_was_empty = False

        for para in full.split('\n'):
            is_empty = (para.strip() == "")

            if is_empty:
                if not last_was_empty:
                    lines.append("")
                last_was_empty = True
                continue

            last_was_empty = False
            cur = ""

            for ch in para:
                test = cur + ch

                if get_width_func(test) > max_width and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur = test

            if cur:
                lines.append(cur)

        return lines

    @staticmethod
    def clean_math_delimiters(text: str) -> str:
        if not text:
            return text
        pattern_block = re.compile(r'\$\$([\s\S]*?)\$\$', re.DOTALL)
        return pattern_block.sub(r'\1', text)


# ================== 3. 统一代码执行引擎 =================

_SANDBOX_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_runner.py")


class CodeExecutor:

    @staticmethod
    def _convert_numpy_types(obj: Any) -> Any:
        if isinstance(obj, (np.float64, np.float32)):
            return float(obj)

        if isinstance(obj, (np.int64, np.int32)):
            return int(obj)

        if isinstance(obj, dict):
            return {k: CodeExecutor._convert_numpy_types(v) for k, v in obj.items()}

        if isinstance(obj, (list, tuple)):
            return [CodeExecutor._convert_numpy_types(item) for item in obj]

        return obj

    @staticmethod
    def _sanitize_json_value(value):
        if value is None or isinstance(value, (str, bool, int)):
            return value

        if isinstance(value, float):
            return value if math.isfinite(value) else None

        if isinstance(value, np.integer):
            return int(value)

        if isinstance(value, np.floating):
            value = float(value)
            return value if math.isfinite(value) else None

        if isinstance(value, np.bool_):
            return bool(value)

        if isinstance(value, np.ndarray):
            return [CodeExecutor._sanitize_json_value(x) for x in value.tolist()]

        if isinstance(value, (pd.Timestamp, pd.Period, pd.Interval, pd.Timedelta)):
            return str(value)

        if isinstance(value, dict):
            return {
                str(k): CodeExecutor._sanitize_json_value(v)
                for k, v in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [CodeExecutor._sanitize_json_value(x) for x in value]

        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        return str(value)

    @staticmethod
    def _df_to_json_payload(d: pd.DataFrame):
        if d is None:
            return None

        d = d.copy(deep=True)
        d.columns = [str(c) for c in d.columns]

        data = d.to_dict(orient="split")
        return CodeExecutor._sanitize_json_value(data)
    
    @staticmethod
    def execute_code(code_str: str, df: pd.DataFrame = None, dfs: list = None):
        success, output, _ = CodeExecutor.execute_code_with_raw(code_str, df=df, dfs=dfs)
        return success, output

    @staticmethod
    def execute_code_with_raw(
        code_str: str,
        df: pd.DataFrame = None,
        dfs: list = None
    ):
        # ===== 1. 数据预处理 =====
        if df is not None:
            df = df.copy(deep=True)
            df.columns = [str(c) for c in df.columns]

        if dfs is not None:
            dfs = [d.copy(deep=True) if d is not None else None for d in dfs]
            for d in dfs:
                if d is not None:
                    d.columns = [str(c) for c in d.columns]

        # ===== 2. 剥离白名单内的 import 语句（前置清理） =====
        allowed_imports = r'(pandas|numpy|math|re|json|datetime|collections|itertools|statistics)'
        code_str = re.sub(rf'^\s*import\s+{allowed_imports}\b.*$', '', code_str, flags=re.MULTILINE)
        code_str = re.sub(rf'^\s*from\s+{allowed_imports}\s+import\b.*$', '', code_str, flags=re.MULTILINE)

        # ===== 3. regex 第一道过滤 =====
        if re.search(r'read_excel|read_csv|open\(|load\s*\(|from\s+file', code_str):
            return False, "代码中禁止读取外部文件，直接使用已有的 df 或 dfs 变量。", None

        # ===== 4. AST 预检 =====
        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, f"代码语法错误: {e}", None

        visitor = _PandasIOVisitor()
        visitor.visit(tree)
        if visitor.violations:
            return False, "代码包含被禁止的 I/O 操作：\n" + "\n".join(visitor.violations), None

        # ===== 5. 序列化数据并调用子进程 =====
        payload = {
            "code": code_str,
            "df": CodeExecutor._df_to_json_payload(df),
            "dfs": [CodeExecutor._df_to_json_payload(d) for d in dfs] if dfs else None,
        }
        
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = subprocess.Popen(
                [sys.executable, _SANDBOX_RUNNER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env, 
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                payload_text = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False
                )
            except Exception as e:
                return False, f"表格数据无法安全序列化为 JSON：{e}", None

            stdout, stderr = proc.communicate(
                payload_text,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
            proc.communicate()
            return False, "代码执行超时（超过600秒），子进程已终止。", None
        except Exception as e:
            return False, f"子进程启动失败: {e}", None

        if not stdout.strip():
            return False, f"子进程无输出。stderr: {stderr[:2000]}", None
        if not stdout.strip():
            return False, f"子进程无有效输出。stderr: {(stderr or '')[:2000]}", None

        try:
            res = json.loads(stdout)
        except json.JSONDecodeError:
            return False, f"子进程返回非 JSON 内容: {stdout[:2000]}", None

        if not res.get("success"):
            return False, res.get("output", "子进程执行失败"), None

        result = res.get("result")
        output = res.get("output", "")

        if output:
            return True, output, result

        if result is not None:
            if isinstance(result, pd.DataFrame):
                display = result.to_csv(index=False)
            elif isinstance(result, pd.Series):
                display = result.to_string(index=True)
            else:
                display = str(result)
            return True, display, result

        return False, "代码执行成功但没有输出。务必在代码末尾使用 `result = ...` 或 `print(...)` 来输出结果。", None


# ================== 4. 消息处理中枢 =================
class MessageProcessor:

    def __init__(self, pet_instance):
        self.pet = pet_instance
        self.uploader = getattr(pet_instance, "docs_uploader", None)
        self.cache_pool = AttachmentCachePool()
        self.last_exportable = None
        self.last_stats = None

        self.emoji_mapping = {
            "😊": "(^_^)", "😄": "(^▽^)", "😃": "(✧▽✧)", "😀": "(◠‿◠)", "😁": "(￣▽￣)", "😆": "(≧∇≦)", "✨": "☆*",
            "😅": "(￣▽￣*)ゞ", "😂": "(T_T)", "🤣": "(≧∇≦)", "😉": "(^_-)", "😗": "(￣ε￣)", "😙": "(￣3￣)",
            "🙂": "(^-^)", "🙃": "(⇀‸↼‶)", "😇": "(◕‿◕✿)", "😌": "(￣ω￣)", "😏": "(￣ー￣)", "😒": "(￣へ￣)",
            "💢": "(≧皿≦)", "💪": "(ง •̀_•́)ง", "👌": "(OK)", "✌️": "V(＾－＾)V", "❌": "✖", "✅": "✔", "❗": "!",
        }

    def emoji_to_kaomoji(self, text: str) -> str:
        if not text:
            return text

        for emoji, kaomoji in self.emoji_mapping.items():
            text = text.replace(emoji, kaomoji)

        return re.sub(r'[^\u0000-\uFFFF]', '', text)

    def _is_thinking_mode(self) -> bool:
        env_value = os.environ.get("XLLAMA_ENABLE_THINKING", "0").strip().lower()
        env_enabled = env_value in ("1", "true", "yes", "on")

        engine = getattr(self.pet, "llm_engine", None)
        capabilities = getattr(engine, "capabilities", {}) or {}
        supports_thinking = capabilities.get("thinking", None)

        if supports_thinking is False:
            return False

        return env_enabled

    # ---------- 失败提示 ----------
    def _is_vision_available(self) -> bool:
        pet = self.pet

        if not pet or not pet.model_ready:
            return False

        engine = getattr(pet, "llm_engine", None)

        if engine is None:
            return False

        mmproj = getattr(engine, "mmproj_path", None)

        return mmproj is not None and os.path.exists(mmproj)

    def _make_failure_reply(self, fail_code, entries):
        if entries and len(entries) == 1:
            meta = entries[0].get("meta") or {}
            target = meta.get("spoken_label", "文件")
        elif entries and len(entries) > 1:
            target = "文件"
        else:
            target = "内容"

        if fail_code == "timeout":
            base = f"刚才处理{target}时模型响应超时了"
        elif fail_code == "empty":
            base = f"刚才处理{target}时模型没有给出有效结果"
        elif fail_code == "model_not_ready":
            base = "我的模型好像还没准备好"
        elif fail_code == "pdf_parse":
            base = "这个PDF转成图片失败了"
        elif fail_code == "excel_parse":
            base = "这个表格读取失败了"
        elif fail_code == "txt_parse":
            base = "这个文本读取失败了"
        elif fail_code == "exception":
            base = "刚才处理时出了点意外"
        else:
            base = "刚才处理失败了"

        return f"{base}，要重试一次吗？"

    def _detect_reply_failure(self, reply):
        if reply is None:
            return "timeout"

        if reply == "（模型未加载）":
            return "model_not_ready"

        if reply == "（模型响应超时）":
            return "timeout"

        if reply in (
            "（模型无回复）",
            "（模型未返回内容）",
            "（模型未输出有效内容）",
            "（模型没有生成有效回复）",
            "（处理轮数达到上限）",
            "（代码执行失败，请检查代码）",
            "（已停止）",
        ):
            return "empty"

        return None

    # ---------- 缓存池 ----------
    def cache_attachments(self, attachments):
        if not attachments:
            return

        for att in attachments:
            if not att:
                continue

            file_no = att.get("file_no")

            if file_no in (None, 0):
                continue

            key = str(file_no)

            if self.cache_pool.has(key):
                continue

            att_type = att.get("type", "")
            normalized_type = att_type

            if normalized_type in ("txt", "pdf"):
                normalized_type = "markdown"

            scan_info = att.get("pdf_scan_info") or {}

            meta = {
                "filename": att.get("filename", ""),
                "type": normalized_type,
                "group": att.get("group", ""),
                "group_no": att.get("group_no"),
                "type_no": att.get("type_no"),
                "spoken_label": att.get("spoken_label", ""),
                "exact_label": att.get("exact_label", ""),
                "source_format": att.get("source_format", ""),
                "pdf_scan_info": scan_info,
            }

            if normalized_type == "markdown" and scan_info.get("has_scanned_pages"):
                base_label = meta.get("spoken_label") or "文档"
                if "扫描" not in base_label:
                    meta["spoken_label"] = f"{base_label}（含扫描页）"

            ttl = att.get("ttl_turns", 30)

            if normalized_type == "excel":
                self.cache_pool.put(key, "excel", att.get("dataframe"),
                                    meta, file_no=file_no, ttl_turns=ttl)
            elif normalized_type == "image":
                self.cache_pool.put(key, "image", att.get("path"),
                                    meta, file_no=file_no, ttl_turns=ttl)
            elif normalized_type == "markdown":
                self.cache_pool.put(key, "markdown", att.get("content") or "",
                                    meta, file_no=file_no, ttl_turns=ttl)

        print(f"[CachePool] 上传入库完成，当前缓存 {self.cache_pool.size} 个文件")

    def force_clear_cache(self):
        if hasattr(self, 'cache_pool'):
            self.cache_pool.clear()

    # ---------- Agent 核心 ----------
    def _record_tool_result(self, raw_result, output):
        if raw_result is None and not output:
            return

        if raw_result is not None:
            if isinstance(raw_result, (pd.DataFrame, pd.Series)):
                kind, data = "table", raw_result
            elif isinstance(raw_result, (dict, list)):
                kind, data = "auto_struct", raw_result
            else:
                kind, data = "text", str(raw_result)
        else:
            kind, data = "text", output

        self.last_exportable = {
            "kind": kind, "data": data,
            "filename_base": "计算结果", "source_label": "计算结果",
        }


    def _safe_append_history_agent(self, u_msg, a_msg, generation_id, used_fc=False):
        if generation_id is None or self.pet.is_generation_current(generation_id):
            self.pet.conversation_history.append({"role": "user", "content": u_msg})

            if used_fc:
                tagged_msg = f"[基于工具调用结果]\n{a_msg}"
            else:
                tagged_msg = a_msg

            self.pet.conversation_history.append({"role": "assistant", "content": tagged_msg})

            max_history = getattr(self.pet, "max_history", 12)

            if len(self.pet.conversation_history) > max_history * 2:
                self.pet.conversation_history = self.pet.conversation_history[-(max_history * 2):]

    def _reply_agent_mode(self, user_msg, file_nos, entries, generation_id=None, stream_callback=None, stats=None):
        if not self.pet.model_ready or not self.pet.llm_engine:
            if getattr(self.pet, "model_loading", False):
                return "咦？我的脑子呢？", None

            fallback = getattr(self.pet, "_fallback_reply", None)

            if fallback:
                return fallback(user_msg), None

            return "（模型未加载）", None

        # ===== 提取请求中的图片路径 =====
        image_paths = []

        supports_fc = self.pet.llm_engine.supports_function_calling()
        thinking_mode = self._is_thinking_mode()
        from prompt import (
            get_tool_agent_system_prompt,
            get_legacy_code_agent_system_prompt,
        )
        # ===== 联网搜索开关与白名单来源 =====
        web_search_enabled = False
        web_search_source_names = ""
        if supports_fc:
            try:
                from internet_settings import InternetSettings
                _internet = InternetSettings.get_instance()
                if _internet.is_enabled and (
                    _internet.tavily_enabled or bool(_internet.enabled_sources())
                ):
                    web_search_enabled = True
                    web_search_source_names = _internet.enabled_source_names()
            except Exception as e:
                print(f"[MessageProcessor] 读取联网设置失败: {e}")
                web_search_enabled = False
        if supports_fc:
            system_prompt = get_tool_agent_system_prompt(
                thinking_mode=thinking_mode,
                web_search_enabled=web_search_enabled
            )
        else:
            system_prompt = get_legacy_code_agent_system_prompt(
                thinking_mode=thinking_mode
            )

        # ===== index_hint =====
        index_hint = ""

        if self.cache_pool.size > 0:
            excel_entries = []
            total_markdown = 0
            total_image = 0

            focus_set = set(file_nos) if file_nos else set()
            has_focus = len(focus_set) > 0

            raw_desc_items = []

            for key in self.cache_pool.keys():
                entry = self.cache_pool.get(key)

                if not entry:
                    continue

                ftype = entry.get("type")
                meta = entry.get("meta") or {}
                fname = meta.get("filename", "") or key
                file_no = entry.get("file_no", "?")
                is_focus = (not has_focus) or (file_no in focus_set)

                read_status = entry.get("read_status", "unread")
                status_map = {
                    "unread": "[未读取]",
                    "read": "[已读取]",
                    "mounted": "[已挂载]",
                }
                status_label = status_map.get(read_status, "[未读取]")

                if ftype == "excel":
                    excel_entries.append((entry, is_focus, status_label))

                elif ftype == "markdown":
                    total_markdown += 1
                    if supports_fc:
                        spoken_label = meta.get("spoken_label") or "文档"
                        disclosed = bool(entry.get("filename_disclosed", True))

                        scan_info = meta.get("pdf_scan_info") or {}
                        scan_note = ""

                        if scan_info.get("has_scanned_pages"):
                            pages = scan_info.get("scanned_pages") or []
                            count = int(scan_info.get("scanned_page_count") or len(pages))

                            if pages:
                                pages_text = "、".join(str(p) for p in pages[:10])
                                if count > len(pages) or scan_info.get("scanned_pages_truncated"):
                                    scan_note = f" [含扫描页：第{pages_text}等共{count}页]"
                                else:
                                    scan_note = f" [含扫描页：第{pages_text}页]"
                            else:
                                scan_note = f" [含扫描页：共{count}页]" if count else " [含扫描页]"

                            scan_note += (
                                " 扫描页文本无效；当前未接入视觉提取，"
                                "禁止描述这些页内容。"
                            )

                        if disclosed:
                            desc = (
                                f"- {file_no}号文件 ({fname}) {status_label} [{spoken_label}]，"
                                f"可通过 read_cached_file(file_no={file_no}) 读取内容"
                                f"{scan_note}"
                            )
                        else:
                            desc = (
                                f"- 文件{file_no}（{spoken_label}）{status_label}，"
                                f"可通过 read_cached_file(file_no={file_no}) 读取内容"
                                f"{scan_note}"
                            )

                        raw_desc_items.append((is_focus, desc))

                elif ftype == "image":
                    total_image += 1
                    if supports_fc:
                        spoken_label = meta.get("spoken_label", fname)
                        vision_ok = self._is_vision_available()

                        if not vision_ok:
                            desc = (
                                f"- 文件{file_no}（{spoken_label}）[不可用]："
                                f"当前视觉模块不可用，禁止声称能看到图片。"
                            )
                        else:
                            desc = (
                                f"- 文件{file_no}（{spoken_label}）[未注入]："
                                f"当前主对话没有看到该图片。"
                                f"如果用户询问图片内容，必须调用 view_cached_image(file_no={file_no})。"
                                f"调用成功后，该图片会进入下一轮视觉输入；当前轮禁止描述图片内容。"
                            )

                        raw_desc_items.append((is_focus, desc))

            total_excel = len(excel_entries)

            if supports_fc:
                for i, (entry, is_focus, status_label) in enumerate(excel_entries):
                    var_name = "df" if total_excel == 1 else f"df{i + 1}"
                    desc = self._describe_excel_entry(entry, var_name, status_label)
                    raw_desc_items.append((is_focus, desc))

                if total_excel > 1:
                    raw_desc_items.append((
                        True,
                        "- 所有表格还可通过 `dfs` 列表访问：dfs[0], dfs[1], …（顺序与 df1, df2, … 一一对应）"
                    ))

                raw_desc_items.sort(key=lambda x: not x[0])
                var_desc_lines = [item[1] for item in raw_desc_items]

                fact_statement = (
                    "[系统规则]：\n"
                    "- 文件编号代表上传的顺序。\n"
                    "- 标记为 [未读取] 的文件，上下文中不存在该文件的任何实质内容。\n"
                    "- 标记为 [未注入] 的图片，当前视觉输入中没有图片，禁止声称已看到图片；"
                    "必须调用 view_cached_image。调用成功后，下一轮才能基于图片回答。\n"
                    "- 标记为 [已挂载] 的表格数据已在 df/dfs 中，必须调用 execute_python_code 进行计算。\n"
                    "- 标记为 [已读取] 只表示该文件曾经被读取过，不代表当前上下文仍保留原文。"
                    "若用户追问 [已读取] 文件的细节，如果不确定，必须再次调用 read_cached_file。\n"
                    "- 标记为 [含扫描页] 的文档，只能通过视觉模块提取内容。\n"
                    "- 未读取文档的真实文件名不可见，禁止根据用户描述猜测文档内容。\n"
                    "- 跳过 [未读取] 或 [未注入] 文件直接回答，视为违规。\n"
                )
                index_hint = (
                    fact_statement
                    + "[当前缓存池文件清单（仅供需要时查阅）：\n"
                    + "\n".join(var_desc_lines) + "\n]\n\n"
                )

            else:
                legacy_lines = []

                for i, (entry, is_focus, status_label) in enumerate(excel_entries):
                    var_name = "df" if total_excel == 1 else f"df{i + 1}"
                    meta = entry.get("meta") or {}
                    label = meta.get("spoken_label", "表格")
                    file_no = entry.get("file_no", "?")
                    fname = meta.get("filename", "")

                    legacy_lines.append(
                        f"- {file_no}号文件 ({fname}) [{label}] 已挂载为 {var_name}"
                    )

                if total_excel > 1:
                    legacy_lines.append("- 所有表格还可通过 `dfs` 列表访问：dfs[0], dfs[1], …")

                if total_markdown > 0:
                    legacy_lines.append(f"- 当前有 {total_markdown} 个文档。")

                if total_image > 0:
                    legacy_lines.append(
                        f"- 当前有 {total_image} 个图片，但旧模式不支持按需查看图片，禁止声称看到图片内容。"
                    )

                index_hint = (
                    "[当前为旧模式：不支持工具调用]\n"
                    "- 文件编号代表上传顺序，编号越小上传越早。\n"
                    "- 需要计算表格时，输出一个 ```python 代码块。\n"
                    "- 代码只能使用 df / df1 / df2 / dfs 变量，禁止 import。\n"
                )

                if legacy_lines:
                    index_hint += "\n".join(legacy_lines) + "\n\n"
                else:
                    index_hint += "\n"

        aug_msg = index_hint + user_msg

        messages = self._build_messages_with_history(
            aug_msg,
            system_override=system_prompt,
            image_count=len(image_paths)
        )

        tools = None
        final_reply = None
        raw_result = None
        output = ""
        used_fc = False

        if supports_fc:
            from tools import (
                ToolLoopDriver,
                get_execute_code_tool_schema,
                get_list_files_tool_schema,
                get_read_file_tool_schema,
                get_export_result_tool_schema,
                get_view_image_tool_schema,
                get_web_search_tool_schema,
                get_read_web_article_tool_schema,
            )

            tools = [
                get_execute_code_tool_schema(),
                get_list_files_tool_schema(),
                get_read_file_tool_schema(),
                get_export_result_tool_schema(),
                get_view_image_tool_schema(),
            ]

            if web_search_enabled:
                tools.append(get_web_search_tool_schema(web_search_source_names))
                tools.append(get_read_web_article_tool_schema())
            driver = ToolLoopDriver(self.pet)

            final_reply, raw_result, output, used_fc = driver.run(
                messages,
                tools,
                cache_pool=self.cache_pool,
                max_loops=8,
                generation_id=generation_id,
                message_processor=self,
                stats=stats,
                image_paths=[],
            )
        else:
            from legacy_tools import LegacyCodeBlockDriver

            legacy_driver = LegacyCodeBlockDriver(self.pet)

            final_reply, raw_result, output, used_fc = legacy_driver.run(
                messages,
                cache_pool=self.cache_pool,
                max_loops=3,
                generation_id=generation_id,
                message_processor=self,
                stats=stats,
                image_paths=[],
            )

        # ============ 防幻觉拦截  ============
        if (supports_fc and not used_fc and web_search_enabled
                and final_reply and isinstance(final_reply, str)):
            fact_patterns = [
                r"20\d{2}\s*年",
                r"\d{1,2}\s*月\s*\d{1,2}\s*日",
            ]
            has_specific_date = any(re.search(p, final_reply) for p in fact_patterns)
            has_quote_source = ("来源：" in final_reply or "来源:" in final_reply
                                or "http://" in final_reply or "https://" in final_reply)

            if has_specific_date or has_quote_source:
                print("[MessageProcessor] 检测到未验证的时效性事实，强制重试并调用 web_search")
                reflect_messages = messages + [
                    {"role": "assistant", "content": final_reply},
                    {"role": "user", "content": (
                        "[系统校验] 你的回复包含具体的时效性事实（年份/日期/来源），"
                        "但本轮没有调用任何工具。\n"
                        "现在必须调用 web_search 验证这些信息，禁止基于记忆作答。"
                        "搜索后，若结果与你的记忆冲突，以搜索结果为准。"
                    )},
                ]
                from tools import ToolLoopDriver
                driver = ToolLoopDriver(self.pet)
                retry_reply, raw_result, output, retry_used_fc = driver.run(
                    reflect_messages, tools,
                    cache_pool=self.cache_pool,
                    max_loops=6,
                    generation_id=generation_id,
                    message_processor=self,
                    stats=stats,
                    image_paths=[],
                )
                if retry_reply and retry_used_fc:
                    final_reply = retry_reply
                    used_fc = True

        # ==================================================

        fail_code = self._detect_reply_failure(final_reply)

        if fail_code:
            failure_msg = self._make_failure_reply(fail_code, entries)
            self._safe_append_history_agent(user_msg, final_reply, generation_id)
            return final_reply, fail_code

        self._safe_append_history_agent(user_msg, final_reply, generation_id)

        has_substantive_result = (raw_result is not None) or bool(output)

        if has_substantive_result:
            base = "计算结果"

            if entries:
                if len(entries) == 1:
                    meta = entries[0].get("meta") or {}
                    filename = meta.get("filename", "")
                    base = os.path.splitext(filename)[0] if filename else "文件结果"
                else:
                    base = "文件处理结果"

            if raw_result is not None:
                if isinstance(raw_result, (pd.DataFrame, pd.Series)):
                    kind, data = "table", raw_result
                elif isinstance(raw_result, (dict, list)):
                    kind, data = "auto_struct", raw_result
                else:
                    kind, data = "text", str(raw_result)
            else:
                kind, data = "text", output

            self.last_exportable = {
                "kind": kind, "data": data,
                "filename_base": base, "source_label": base,
            }

        elif not self.last_exportable and final_reply:
            self.last_exportable = {
                "kind": "text", "data": final_reply,
                "filename_base": "对话记录", "source_label": "对话记录",
            }

        if stream_callback and final_reply:
            chunk_size = 8

            for i in range(0, len(final_reply), chunk_size):
                if generation_id is not None and not self.pet.is_generation_current(generation_id):
                    break

                try:
                    stream_callback(final_reply[i:i + chunk_size])
                except Exception:
                    pass

                time.sleep(0.04)

        return final_reply, None

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        if not text or not isinstance(text, str):
            return text

        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL | re.IGNORECASE)

        return text.strip()

    # ---------- 主入口 ----------
    def get_reply(
        self,
        user_msg: str,
        file_nos=None,
        generation_id=None,
        stream_callback=None
    ) -> str:
        file_nos = file_nos or []
        stats = ResponseStats()
        self.last_stats = stats

        entries = []

        for fn in file_nos:
            entry = self.cache_pool.get(str(fn))

            if entry is not None:
                entries.append(entry)

        final_reply, fail_code = self._reply_agent_mode(
            user_msg, file_nos, entries,
            generation_id=generation_id,
            stream_callback=stream_callback,
            stats=stats
        )

        if fail_code:
            stats.finish()
            return self.emoji_to_kaomoji(self._make_failure_reply(fail_code, entries))

        if not final_reply:
            final_reply = "（模型没有生成有效回复）"

        final_reply = self._strip_think_tags(final_reply)
        final_reply = TextUtils.clean_math_delimiters(final_reply)

        stats.finish()

        return final_reply

    def _trim_history_to_fit(self, history, system_tokens, current_user_tokens,
                             max_prompt_tokens, image_tokens=0):
        fixed_tokens = system_tokens + current_user_tokens + image_tokens
        remaining = max_prompt_tokens - fixed_tokens

        if remaining <= 0:
            return []

        trimmed = list(history)
        total_history_tokens = estimate_messages_tokens(trimmed)

        while total_history_tokens > remaining and len(trimmed) > 0:
            if len(trimmed) >= 2:
                first_role = trimmed[0].get("role")
                second_role = trimmed[1].get("role")

                if (
                    (first_role == "user" and second_role == "assistant")
                    or (first_role == "assistant" and second_role == "user")
                ):
                    removed1 = trimmed.pop(0)
                    removed2 = trimmed.pop(0)
                    total_history_tokens -= estimate_messages_tokens([removed1])
                    total_history_tokens -= estimate_messages_tokens([removed2])
                    continue

            removed = trimmed.pop(0)
            total_history_tokens -= estimate_messages_tokens([removed])

        return trimmed

    # ---------- 辅助：构建带历史的 messages ----------
    def _build_messages_with_history(self, user_msg, system_override=None, image_count=0):
        system = system_override or getattr(self.pet, "system_prompt", "")
        max_history = getattr(self.pet, "max_history", 12)
        raw_history = getattr(self.pet, "conversation_history", [])[-(max_history * 2):]

        current_time = time.localtime()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        time_context = f"<sys_time>{time.strftime('%Y-%m-%d %H:%M', current_time)} {weekdays[current_time.tm_wday]}</sys_time>"

        user_content = f"{time_context}\n{user_msg}"

        n_ctx = int(os.environ.get("XLLAMA_CTX", 8192))
        max_gen_tokens = max(256, int(n_ctx * 0.4))
        max_prompt_tokens = n_ctx - max_gen_tokens

        system_tokens = estimate_text_tokens(system)
        current_user_tokens = estimate_text_tokens(user_content)
        image_tokens = image_count * 1536

        safe_history = self._trim_history_to_fit(
            raw_history,
            system_tokens=system_tokens,
            current_user_tokens=current_user_tokens,
            max_prompt_tokens=max_prompt_tokens,
            image_tokens=image_tokens,
        )

        return [
            {"role": "system", "content": system},
            *safe_history,
            {"role": "user", "content": user_content}
        ]

    def _describe_excel_entry(self, entry, var_name: str, status_label: str) -> str:
        meta = entry.get("meta") or {}
        label = meta.get("spoken_label", "表格")
        filename = meta.get("filename", "")
        file_no = entry.get("file_no", "?")
        df = entry.get("data")

        if df is None or not hasattr(df, "columns"):
            return f"- {file_no}号文件 ({filename}) {status_label} [{label}] → 变量 `{var_name}`（DataFrame，数据为空）"

        try:
            shape = df.shape
            dtypes_str = ", ".join(f"{c}({df[c].dtype})" for c in df.columns)

            return (
                f"- {file_no}号文件 ({filename}) {status_label} [{label}] → 变量 `{var_name}` (pandas DataFrame)\n"
                f"    结构：{shape[0]} 行 × {shape[1]} 列\n"
                f"    列名及类型：{dtypes_str}"
            )

        except Exception:
            cols = [str(c) for c in df.columns]
            return f"- {file_no}号文件 ({filename}) {status_label} [{label}] → 变量 `{var_name}` (pandas DataFrame)，列名：{cols}"

    def clear_cache(self):
        self.last_exportable = None
