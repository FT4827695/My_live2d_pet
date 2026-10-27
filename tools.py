# -*- coding: utf-8 -*-

import json
import os

from services import CodeExecutor


# ==================== 1. 工具 Schema 定义 ====================

def get_execute_code_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "execute_python_code",
            "strict": True,
            "description": (
                "执行 Python 代码进行精确计算。"
                "当缓存池中存在表格时，环境内置 pd, np, math, re, json, datetime, collections, itertools, statistics。"
                "以及 df（单表）/ df1, df2, dfs（多表）变量，可直接使用。禁止使用 import 语句。"
                "表格文件名和列名仅用于定位与写代码，禁止据此推断数据内容。"
                "所有数值、统计、筛选、排名、汇总结论必须来自代码输出。"
                "结果赋值给 result 变量或用 print() 输出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "description": "要执行的纯 Python 代码，不要包含 ```python 标记。"
                    }
                },
                "required": ["code"],
                "additionalProperties": False
            }
        }
    }


def get_list_files_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "list_cached_files",
            "strict": True,
            "description": (
                "列出当前缓存池中的文件编号、可见名称、类型和状态。"
                "未读取文档不会返回真实文件名。"
                "该工具仅返回文件清单，不是文件内容来源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False
            }
        }
    }


def get_read_file_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "read_cached_file",
            "strict": True,
            "description": (
                "读取指定编号的文档内容。"
                "图片不能通过此工具读取。"
                "未读取文档没有内容，禁止根据用户描述猜测内容。"
                "读取成功后，才会披露真实文件名。"
                "获取文件内容的唯一可靠途径，回答追问时可再次调用此工具。"
                "表格可通过此工具查看结构，但表格名和列名仅用于定位，不能代替代码计算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_no": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "要读取的文件编号，必须是正整数。"
                    }
                },
                "required": ["file_no"],
                "additionalProperties": False
            }
        }
    }


def get_view_image_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "view_cached_image",
            "strict": True,
            "description": (
                "请求查看缓存池中的图片。"
                "调用成功后，该图片会加入下一轮主模型的视觉输入。"
                "当前轮仍不能声称已经看到图片；必须在下一轮基于图片内容回答。"
                "如果用户询问图片内容，优先调用此工具，而不是猜测。"
                "如果不确定图片编号，先调用 list_cached_files。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_no": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "要查看的图片文件编号，必须是正整数。"
                    }
                },
                "required": ["file_no"],
                "additionalProperties": False
            }
        }
    }

def get_export_result_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "export_result",
            "strict": True,
            "description": (
                "将最近一次分析或计算得到的结果保存为本地文件（保存到桌面）。"
                "当用户明确要求保存、导出、存成文件时才调用此工具。"
                "必须通过调用此工具来完成用户的保存请求。"
                "没有工具调用，文件就没有保存。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["auto", "txt", "csv", "json", "excel"],
                        "description": "保存格式。auto 表示根据结果类型自动选择。"
                    }
                },
                "required": [],
                "additionalProperties": False
            }
        }
    }


def get_web_search_tool_schema(source_names: str):
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "strict": True,
            "description": (
                "在指定的白名单网站中搜索时效性信息或你不确定的事实。"
                f"当前允许搜索的来源仅限：{source_names}。禁止搜索白名单以外的网站。"
                "仅当问题涉及实时资讯、知识截止后的事实、或用户明确要求联网查证时才调用；"
                "本地文件分析、数值计算禁止使用本工具。"
                "返回带编号的结果摘要列表；如需某条结果的正文，再调用 read_web_article。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": "搜索关键词，简洁准确，必要时使用中文。"
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "description": "每个来源返回的最大结果数，默认3。"
                    }
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }
    }


def get_read_web_article_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "read_web_article",
            "strict": True,
            "description": (
                "读取 web_search 返回的某条结果的正文内容。"
                "参数 identifier 传入搜索结果中的 ID（如 wiki_zh_3），"
                "或白名单域名内的完整链接。"
                "仅当摘要不足以回答问题时才调用；禁止用于白名单以外的网址。"
                "部分动态渲染来源（如百度百科、知乎）不支持读取正文，"
                "此时应依据摘要作答，不要重复尝试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "minLength": 1,
                        "description": "web_search 结果中的引用 ID，或白名单域名内的 http/https 链接。"
                    }
                },
                "required": ["identifier"],
                "additionalProperties": False
            }
        }
    }

# ==================== 2. Tool Loop 驱动器 ====================

class ToolLoopDriver:

    MAX_ACTIVE_IMAGES = 10
    
    _SUCCESS_BODY_FORMATTERS = {
        "list_cached_files": lambda data: (
            f"当前缓存池文件列表：\n"
            f"{json.dumps(data, ensure_ascii=False)}"
        ),
        "execute_python_code": lambda data: f"代码输出结果：\n{data}",
        "web_search": lambda data: f"联网搜索结果：\n{data}",
        "read_web_article": lambda data: f"网页正文内容：\n{data}",
    }

    def __init__(self, pet_instance):
        self.pet = pet_instance

    @staticmethod
    def _normalize_file_no(value):
        if isinstance(value, bool):
            return None, "参数 'file_no' 必须是正整数，不能是布尔值。"

        if isinstance(value, int):
            file_no = value

        elif isinstance(value, float):
            if value.is_integer():
                file_no = int(value)
            else:
                return None, f"参数 'file_no' 必须是正整数，当前值为 {value}。"

        elif isinstance(value, str):
            s = value.strip()

            if s.isdigit():
                file_no = int(s)
            else:
                try:
                    f = float(s)
                except (ValueError, TypeError):
                    return None, f"参数 'file_no' 必须是正整数，当前值为 '{value}'。"

                if f.is_integer():
                    file_no = int(f)
                else:
                    return None, f"参数 'file_no' 必须是正整数，当前值为 '{value}'。"

        else:
            return None, f"参数 'file_no' 必须是正整数，当前类型为 {type(value).__name__}。"

        if file_no < 1:
            return None, f"参数 'file_no' 必须大于等于 1，当前值为 {file_no}。"

        return file_no, None

    def _validate_tool_args(self, func_name, args_str):
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {str(e)}。请确保 arguments 是合法的 JSON 格式，不要包含多余的解释文字或注释。"

        if not isinstance(args, dict):
            return None, "参数必须是 JSON 对象 (object)。"

        if func_name == "execute_python_code":
            code = args.get("code")

            if not isinstance(code, str) or not code.strip():
                return None, "缺少必填参数 'code'，或 'code' 为空字符串。"

            code = code.strip()

            if code.startswith("```python"):
                code = code[9:]
            elif code.startswith("```"):
                code = code[3:]

            if code.endswith("```"):
                code = code[:-3]

            args["code"] = code.strip()
            return args, None

        elif func_name == "list_cached_files":
            return {}, None

        elif func_name == "read_cached_file":
            file_no = args.get("file_no")
            if file_no is None:
                return None, "缺少必填参数 'file_no'。"
            file_no, err = ToolLoopDriver._normalize_file_no(file_no)
            if err is not None:
                return None, err
            return {"file_no": file_no}, None

        elif func_name == "view_cached_image":
            file_no = args.get("file_no")
            if file_no is None:
                return None, "缺少必填参数 'file_no'。"
            file_no, err = ToolLoopDriver._normalize_file_no(file_no)
            if err is not None:
                return None, err
            return {"file_no": file_no}, None

        elif func_name == "export_result":
            fmt = args.get("format")

            if fmt is not None:
                if not isinstance(fmt, str):
                    return None, f"参数 'format' 必须是字符串，当前类型为 {type(fmt).__name__}。"

                fmt = fmt.strip().lower()

                if fmt not in ("auto", "txt", "csv", "json", "excel"):
                    return None, f"参数 'format' 的值 '{fmt}' 不合法。只允许: auto, txt, csv, json, excel。"

                return {"format": fmt}, None

            return {}, None

        elif func_name == "web_search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return None, "缺少必填参数 'query'，或 'query' 为空。"
            query = query.strip()
            if len(query) > 100:
                return None, "参数 'query' 过长，精简到100字以内再搜索。"
            max_results = args.get("max_results")
            if max_results is None:
                return {"query": query}, None
            if isinstance(max_results, bool):
                return None, "参数 'max_results' 必须是1到8之间的整数。"
            try:
                max_results = int(float(max_results))
            except (ValueError, TypeError):
                return None, f"参数 'max_results' 必须是1到8之间的整数，当前值为 '{max_results}'。"
            if max_results < 1 or max_results > 8:
                return None, f"参数 'max_results' 必须在1到8之间，当前值为 {max_results}。"
            return {"query": query, "max_results": max_results}, None
        elif func_name == "read_web_article":
            identifier = args.get("identifier")
            if not isinstance(identifier, str) or not identifier.strip():
                return None, "缺少必填参数 'identifier'，或 'identifier' 为空。"
            return {"identifier": identifier.strip()}, None
        
        else:
            return None, f"未知工具: {func_name}"

    def _format_tool_result(self, result_dict: dict) -> str:
        tool_name = result_dict.get("tool_name", "unknown")

        if result_dict.get("ok", False):
            data = result_dict.get("data", "")
            formatter = self._SUCCESS_BODY_FORMATTERS.get(tool_name)
            body = formatter(data) if formatter else data
            return f"[工具执行成功: {tool_name}]\n{body}"

        error_type = result_dict.get("error_type", "unknown_error")
        error_msg = result_dict.get("error", "未知错误")
        next_action = result_dict.get(
            "next_action", "请检查参数或工具调用是否正确。"
        )

        return (
            f"[工具执行失败: {tool_name}]\n"
            f"错误类型: {error_type}\n"
            f"原因: {error_msg}\n"
            f"下一步建议: {next_action}"
        )

    def run(
        self,
        messages,
        tools,
        df=None,
        dfs=None,
        cache_pool=None,
        max_loops=5,
        generation_id=None,
        message_processor=None,
        stats=None,
        image_paths=None
    ):
        engine = self.pet.llm_engine
        current_messages = list(messages)
        used_fc = False

        searched_queries = set()

        active_image_paths = []

        if df is None and dfs is None and cache_pool:
            excel_dfs = []

            for key in cache_pool.keys():
                entry = cache_pool.get(key)

                if entry and entry.get("type") == "excel" and entry.get("data") is not None:
                    excel_dfs.append(entry["data"])

            if len(excel_dfs) == 1:
                df = excel_dfs[0]
            elif len(excel_dfs) > 1:
                dfs = excel_dfs

        last_raw_result = None
        last_output = ""

        def _aborted():
            if generation_id is None or self.pet is None:
                return False

            try:
                return not self.pet.is_generation_current(generation_id)
            except Exception:
                return False

        def _record(raw, out):
            if message_processor is not None and hasattr(message_processor, "_record_tool_result"):
                try:
                    message_processor._record_tool_result(raw, out)
                except Exception:
                    pass

        for loop_idx in range(max_loops):
            if _aborted():
                return "（已停止）", last_raw_result, last_output, used_fc

            resp = engine.generate(
                current_messages,
                image_paths=active_image_paths,
                tools=tools,
                return_raw_dict=True
            )

            if _aborted():
                return "（已停止）", last_raw_result, last_output, used_fc

            if resp is None:
                return None, None, "", used_fc

            if stats is not None:
                stats.record_raw_dict(resp, messages=current_messages)

            content = resp.get("content", "")
            tool_calls = resp.get("tool_calls")
            reasoning = resp.get("reasoning_content", "")

            if tool_calls:
                used_fc = True

                assistant_msg = {"role": "assistant", "content": content or ""}

                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning

                assistant_msg["tool_calls"] = tool_calls
                current_messages.append(assistant_msg)

                for tc in tool_calls:
                    if _aborted():
                        return "（已停止）", last_raw_result, last_output, used_fc
                    
                    func_name = tc.get("function", {}).get("name")
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    tc_id = tc.get("id", "call_0")

                    args, validation_error = self._validate_tool_args(func_name, args_str)

                    tool_result = ""

                    if validation_error:
                        tool_result = (
                            f"【系统拦截】工具 {func_name} 参数校验失败，未执行。\n"
                            f"错误详情：{validation_error}\n"
                            f"修正要求：请检查参数类型、必填项和枚举值，确保完全符合 JSON Schema，然后重新调用该工具。"
                        )
                    else:
                        structured_result = {"tool_name": func_name, "ok": False}

                        if func_name == "execute_python_code":
                            code = args["code"]

                            success, output, raw_result = CodeExecutor.execute_code_with_raw(
                                code,
                                df=df,
                                dfs=dfs
                            )

                            if success:
                                structured_result["ok"] = True
                                structured_result["data"] = output if output else "(代码执行成功，但无标准输出。结果已保存在 result 变量中)"

                                if raw_result is not None:
                                    last_raw_result = raw_result

                                if output:
                                    last_output = output

                                _record(raw_result, output)

                            else:
                                structured_result["error_type"] = "code_execution_error"
                                structured_result["error"] = output
                                structured_result["next_action"] = (
                                    "请修正代码中的语法或逻辑错误，确保只使用 df/dfs 且不使用 import，然后重新调用。"
                                )

                        elif func_name == "list_cached_files":
                            structured_result["ok"] = True
                            structured_result["data"] = cache_pool.list_index() if cache_pool else []

                        elif func_name == "read_cached_file":
                            file_no = args["file_no"]

                            if cache_pool and file_no is not None:
                                res = cache_pool.retrieve(file_no)

                                if res.get("ok"):
                                    if res.get("type") == "image":
                                        structured_result["error_type"] = "wrong_tool_for_image"
                                        structured_result["error"] = f"文件 {file_no} 是图片，不能通过文本读取。"
                                        structured_result["next_action"] = (
                                            f"请改用 view_cached_image(file_no={file_no})，调用后下一轮即可直接查看该图片。"
                                        )
                                    else:
                                        cache_pool.mark_as_read(str(file_no))
                                        structured_result["ok"] = True
                                        structured_result["data"] = f"文件名：{res['display_name']}\n内容：\n{res['content']}"
                                else:
                                    structured_result["error_type"] = "file_retrieve_failed"
                                    structured_result["error"] = res.get("error", "未知读取错误")
                                    structured_result["next_action"] = "请检查 file_no 是否正确，或调用 list_cached_files 确认文件编号。"
                            else:
                                structured_result["error_type"] = "invalid_environment"
                                structured_result["error"] = "缓存池不可用或未提供有效的文件编号。"
                                structured_result["next_action"] = "请确认当前环境是否有文件，并提供正确的 file_no。"

                        elif func_name == "view_cached_image":
                            view_res = self._request_view_cached_image(
                                engine=engine,
                                cache_pool=cache_pool,
                                file_no=args["file_no"],
                                active_image_paths=active_image_paths
                            )
                            structured_result.update(view_res)

                            if not structured_result.get("ok"):
                                structured_result.setdefault(
                                    "next_action",
                                    "请检查 file_no 是否对应图片文件，或确认当前视觉模块是否可用。"
                                )

                        elif func_name == "export_result":
                            exp_res = self._run_export(args, message_processor)
                            structured_result.update(exp_res)

                            if not structured_result.get("ok"):
                                structured_result.setdefault(
                                    "next_action",
                                    "请确认是否有可导出的结果（需先进行计算或分析），或检查 format 参数是否合法。"
                                )

                        elif func_name == "web_search":
                            q = str(args.get("query", "")).strip().lower()
                            if q in searched_queries:
                                structured_result["ok"] = False
                                structured_result["error_type"] = "duplicate_query"
                                structured_result["error"] = (
                                    f"query '{args.get('query')}' 已经搜过，结果重复。"
                                )
                                structured_result["next_action"] = (
                                    "不要重复搜索同一个词。"
                                    "请从已有结果里选 1-2 条调用 read_web_article 读正文，"
                                    "或换一个明显不同且更短的关键词。"
                                )
                            else:
                                searched_queries.add(q)
                                sr = self._run_web_search(args, _aborted)
                                structured_result.update(sr)
                                if not structured_result.get("ok"):
                                    structured_result.setdefault(
                                        "next_action",
                                        "可更换关键词重试一次，仍失败则如实告知用户。"
                                    )

                        else:
                            structured_result["error_type"] = "unknown_tool"
                            structured_result["error"] = f"未知工具：{func_name}"
                            structured_result["next_action"] = "请检查工具名称拼写，只允许使用已定义的工具。"

                        tool_result = self._format_tool_result(structured_result)

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result
                    })

                continue

            return content, last_raw_result, last_output, used_fc

        return "（这...做不来了）", last_raw_result, last_output, used_fc

    def _request_view_cached_image(self, engine, cache_pool, file_no, active_image_paths):
        if cache_pool is None:
            return {
                "ok": False,
                "error_type": "env_error",
                "error": "缓存池不可用。"
            }

        if file_no is None:
            return {
                "ok": False,
                "error_type": "missing_arg",
                "error": "缺少 file_no。"
            }

        key = str(file_no)
        entry = cache_pool.get(key)

        if entry is None:
            return {
                "ok": False,
                "error_type": "file_not_found",
                "error": f"文件{file_no}不在缓存池中。"
            }

        if entry.get("type") != "image":
            return {
                "ok": False,
                "error_type": "wrong_type",
                "error": f"文件{file_no}不是图片，无法使用 view_cached_image。"
            }

        img_path = entry.get("data")

        if not img_path or not isinstance(img_path, str) or not os.path.exists(img_path):
            return {
                "ok": False,
                "error_type": "file_not_found",
                "error": f"文件{file_no}的图片路径无效或不存在。"
            }

        mmproj = getattr(engine, "mmproj_path", None)
        if not mmproj or not os.path.exists(mmproj):
            return {
                "ok": False,
                "error_type": "vision_unavailable",
                "error": "当前模型没有加载视觉模块，无法查看图片。"
            }

        if img_path in active_image_paths:
            return {
                "ok": True,
                "data": (
                    f"图片{file_no}已在当前视觉上下文中。"
                    f"请直接基于图片内容回答，不要重复调用 view_cached_image。"
                )
            }

        if len(active_image_paths) >= self.MAX_ACTIVE_IMAGES:
            return {
                "ok": False,
                "error_type": "too_many_images",
                "error": (
                    f"当前同时查看的图片数量已达上限（{self.MAX_ACTIVE_IMAGES}张）。"
                ),
                "next_action": (
                    "请先基于已查看的图片回答，或减少同时查看的图片数量。"
                )
            }

        active_image_paths.append(img_path)

        try:
            cache_pool.mark_as_read(str(file_no))
        except Exception:
            pass

        return {
            "ok": True,
            "data": (
                f"图片{file_no}已加入下一轮视觉输入。"
                f"当前轮禁止声称已经看到图片；下一轮请直接基于图片内容回答。"
            )
        }

    def _run_export(self, args, message_processor):
        try:
            import file_exporter
        except Exception as e:
            return {"ok": False, "error_type": "module_error", "error": f"导出模块不可用：{e}"}

        fmt = str(args.get("format") or "auto").strip().lower()
        requested = None if fmt in ("", "auto") else fmt

        payload = getattr(message_processor, "last_exportable", None) if message_processor else None

        if not payload:
            return {
                "ok": False,
                "error_type": "no_payload",
                "error": "当前还没有可以保存的结果，请先进行分析或计算。"
            }

        try:
            res = file_exporter.save_exportable(payload, requested)
        except Exception as e:
            return {"ok": False, "error_type": "save_exception", "error": f"保存过程异常：{e}"}

        if res.get("success"):
            try:
                if self.pet is not None:
                    kind = res.get("bubble_kind", "doc")
                    self.pet.queue_bubble("表格保存好啦" if kind == "table" else "文档保存好啦")
            except Exception:
                pass

            return {
                "ok": True,
                "data": f"已成功保存到桌面：{os.path.basename(res.get('path', ''))}"
            }

        err = res.get("error_code", "unknown")

        if err == "desktop_unavailable":
            return {"ok": False, "error_type": "path_error", "error": "找不到桌面路径。"}

        return {"ok": False, "error_type": "save_failed", "error": f"保存失败（{err}）。"}

    def _run_web_search(self, args, aborted):
        try:
            from internet_settings import InternetSettings
            from web_search import run_web_search, format_search_results
        except Exception as e:
            return {
                "ok": False,
                "error_type": "module_error",
                "error": f"联网搜索模块不可用：{e}",
                "next_action": "请告知用户联网搜索功能暂不可用，依据已有知识作答。"
            }
        if not InternetSettings.get_instance().is_enabled:
            return {
                "ok": False,
                "error_type": "feature_disabled",
                "error": "联网搜索当前未开启。",
                "next_action": "请告知用户需在设置中开启联网搜索，本轮依据已有知识作答。"
            }
        try:
            res = run_web_search(args["query"], args.get("max_results"), aborted=aborted)
            titles = [r.get("title", "") for r in res.get("results", [])]
        except Exception as e:
            return {
                "ok": False,
                "error_type": "search_exception",
                "error": f"搜索过程异常：{e}",
                "next_action": "可更换关键词重试一次，仍失败则如实告知用户。"
            }
        if not res.get("ok"):
            return {
                "ok": False,
                "error_type": res.get("error_type", "search_failed"),
                "error": res.get("error", "搜索失败"),
                "next_action": res.get("next_action", "可更换关键词重试一次。")
            }
        data = format_search_results(res["results"], res.get("failed_sources"))
        return {"ok": True, "data": data}

    def _run_read_web_article(self, args):
        try:
            from internet_settings import InternetSettings
            from web_search import run_read_article
        except Exception as e:
            return {
                "ok": False,
                "error_type": "module_error",
                "error": f"联网搜索模块不可用：{e}",
                "next_action": "请告知用户联网搜索功能暂不可用，依据搜索摘要作答。"
            }
        if not InternetSettings.get_instance().is_enabled:
            return {
                "ok": False,
                "error_type": "feature_disabled",
                "error": "联网搜索当前未开启。",
                "next_action": "请告知用户需在设置中开启联网搜索，依据已有知识作答。"
            }
        try:
            res = run_read_article(args["identifier"])
        except Exception as e:
            return {
                "ok": False,
                "error_type": "fetch_exception",
                "error": f"正文读取异常：{e}",
                "next_action": "请依据已有搜索摘要作答。"
            }
        if not res.get("ok"):
            return {
                "ok": False,
                "error_type": res.get("error_type", "fetch_failed"),
                "error": res.get("error", "正文读取失败"),
                "next_action": res.get("next_action", "请依据搜索摘要作答，不要重复重试。")
            }
        data = (
            f"标题：{res.get('title', '')}\n"
            f"来源：{res.get('source_name', '')}\n"
            f"链接：{res.get('url', '')}\n"
            f"正文：\n{res.get('text', '')}"
        )
        return {"ok": True, "data": data}
