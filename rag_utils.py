import os
import re
import pandas as pd
import time


class AttachmentCachePool:
    TYPE_LABELS = {
        "excel": "表格",
        "markdown": "文档",
        "txt": "文档",
        "pdf": "文档",
        "image": "图片",
    }

    _MATCH_MIN_SCORE = 60
    _MATCH_SHORT_BASE_LEN = 2

    def __init__(self):
        self._entries = {}  # key -> entry dict

    def _is_doc_type(self, att_type: str) -> bool:
        return att_type in ("markdown", "txt", "pdf")

    def _status_label(self, entry: dict) -> str:
        status_map = {
            "unread": "[未读取]",
            "read": "[已读取]",
            "mounted": "[已挂载]",
        }
        return status_map.get(entry.get("read_status", "unread"), "[未读取]")

    def _is_undisclosed_doc(self, entry: dict) -> bool:
        if not entry:
            return False

        att_type = entry.get("type", "")

        if not self._is_doc_type(att_type):
            return False

        return not entry.get("filename_disclosed", True)

    def _display_name(self, entry: dict, file_no) -> str:
        meta = entry.get("meta") or {}
        filename = meta.get("filename", "")
        att_type = entry.get("type", "")
        type_label = self.TYPE_LABELS.get(att_type, att_type)

        if self._is_doc_type(att_type) and not entry.get("filename_disclosed", True):
            return f"文件{file_no}（{type_label}）"

        return f"{file_no}-{filename}-{type_label}"

    def _format_pdf_scan_notice(self, scan_info: dict) -> str:
        if not scan_info or not scan_info.get("has_scanned_pages"):
            return ""

        pages = scan_info.get("scanned_pages") or []
        count = int(scan_info.get("scanned_page_count") or len(pages))

        if pages:
            pages_text = "、".join(str(p) for p in pages[:20])
            if count > len(pages) or scan_info.get("scanned_pages_truncated"):
                page_desc = f"第{pages_text}等共{count}页"
            else:
                page_desc = f"第{pages_text}页"
        else:
            page_desc = f"共{count}页" if count else "部分页面"

        if scan_info.get("scan_mode") == "full":
            base = f"[系统标记] 本PDF疑似整份扫描件（{page_desc}疑似扫描页）。"
        else:
            base = f"[系统标记] 本PDF检测到扫描页：{page_desc}。"

        extra = "当前未接入视觉提取，禁止基于文本猜测扫描页内容。"

        if scan_info.get("partial_analysis"):
            extra += f"（本次仅自动分析前{scan_info.get('analyzed_pages', 0)}页/时间受限）"

        return f"{base} {extra}"
    
    @staticmethod
    def _tokenize_for_match(text: str) -> set:
        text = str(text or "").lower()
        tokens = set()

        # 英文 / 数字词
        for m in re.finditer(r"[a-z0-9]+", text):
            token = m.group(0)
            if len(token) >= 2:
                tokens.add(token)

        # 中文连续片段
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            if len(run) >= 2:
                tokens.add(run)

                for i in range(len(run) - 1):
                    tokens.add(run[i:i + 2])
    
        return tokens

    # ==================== 写入 ====================

    def put(self, key: str, att_type: str, data, meta: dict = None,
            file_no=None, ttl_turns=30):
        if key in self._entries:
            return

        if file_no is None:
            try:
                file_no = int(key)
            except (ValueError, TypeError):
                file_no = None

        if att_type == "excel":
            read_status = "mounted"
            filename_disclosed = True
        elif self._is_doc_type(att_type):
            read_status = "unread"
            filename_disclosed = False
        else:
            read_status = "unread"
            filename_disclosed = True

        now = time.time()

        entry = {
            "type": att_type,
            "data": data,
            "meta": meta or {},
            "created_at": now,
            "uploaded_at": now,
            "file_no": file_no,
            "ttl_turns": ttl_turns,
            "read_status": read_status,
            "filename_disclosed": filename_disclosed,
        }

        self._entries[key] = entry
        
    def mark_as_read(self, key: str) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return False

        if entry["type"] != "excel":
            entry["read_status"] = "read"

            if self._is_doc_type(entry["type"]):
                entry["filename_disclosed"] = True

            return True

        return False
    
    # ==================== 读取 ====================

    def get(self, key: str):
        return self._entries.get(key)

    def has(self, key: str) -> bool:
        return key in self._entries

    def get_meta(self, key: str) -> dict:
        entry = self._entries.get(key)
        if entry is None:
            return {}
        return entry.get("meta") or {}

    def get_data(self, key: str):
        entry = self._entries.get(key)
        if entry is None:
            return None
        return entry["data"]

    # ==================== 索引 ====================
    def list_index(self) -> list:
        result = []

        for key, entry in self._entries.items():
            meta = entry.get("meta") or {}
            file_no = entry.get("file_no")
            show_no = file_no if file_no is not None else key
            filename = meta.get("filename", "")
            type_label = self.TYPE_LABELS.get(entry["type"], entry["type"])
            status_label = self._status_label(entry)
            disclosed = bool(entry.get("filename_disclosed", True))

            if self._is_doc_type(entry["type"]) and not disclosed:
                display = f"文件{show_no}（{type_label}）"
                visible_filename = ""
            else:
                display = f"{show_no}-{filename}-{type_label}"
                visible_filename = filename

            timestamp = entry.get("uploaded_at") or entry.get("created_at")
            uploaded_at = ""

            if timestamp:
                try:
                    uploaded_at = time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(timestamp)
                    )
                except Exception:
                    uploaded_at = ""

            scan_info = meta.get("pdf_scan_info") or {}

            result.append({
                "file_no": file_no,
                "filename": visible_filename,
                "type": entry["type"],
                "status": status_label,
                "display_name": display,
                "group": meta.get("group"),
                "group_no": meta.get("group_no"),
                "spoken_label": meta.get("spoken_label"),
                "upload_order": file_no,
                "created_at": entry.get("created_at"),
                "uploaded_at": uploaded_at,
                "has_scanned_pages": bool(scan_info.get("has_scanned_pages", False)),
                "scanned_pages": (scan_info.get("scanned_pages") or [])[:20],
                "scan_mode": scan_info.get("scan_mode", ""),
            })
        
        result.sort(key=lambda x: x.get("file_no") or 0)
        return result
    
    # ==================== 关键词检索 ====================

    def search_by_keywords(self, query: str, top_k: int = 2) -> list:
        query_terms = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', query.lower()))
        scored = []
        for key, entry in self._entries.items():
            meta = entry.get("meta") or {}
            filename = meta.get("filename", "").lower()
            att_type = entry["type"]
            file_terms = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', filename))
            name_score = len(query_terms & file_terms)
            content_score = 0
            if att_type == "markdown":
                data = entry.get("data", "")
                if isinstance(data, str):
                    for term in query_terms:
                        content_score += data.lower().count(term)
            total_score = name_score * 10 + content_score
            if total_score > 0:
                scored.append((key, total_score, entry))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in scored[:top_k]]

    # ==================== 按编号读取 ====================

    def retrieve(self, file_no) -> dict:
        key = str(file_no)
        entry = self._entries.get(key)

        if entry is None:
            return {"ok": False, "error": f"{file_no}号文件不在缓存池中", "file_no": file_no}

        meta = entry.get("meta") or {}
        att_type = entry["type"]

        if att_type in ("txt", "pdf"):
            att_type = "markdown"

        if att_type == "excel":
            data = entry.get("data")

            if data is None:
                return {"ok": False, "error": "该表格无可用数据", "file_no": file_no}

            if not hasattr(data, "columns"):
                return {"ok": False, "error": "该表格数据格式异常", "file_no": file_no}

            columns = [str(c) for c in data.columns]
            shape = data.shape
            dtypes = {str(c): str(data[c].dtype) for c in data.columns}

            content = (
                f"表格结构：{shape[0]}行 × {shape[1]}列\n"
                f"列名：{columns}\n"
                f"数据类型：{dtypes}\n"
                f"（完整数据已加载至 df 变量，可通过 execute_python_code 进行全量分析）"
            )

            return {
                "ok": True,
                "type": "excel",
                "display_name": self._display_name(entry, file_no),
                "content": content,
                "file_no": file_no,
                "error": None,
            }

        elif att_type == "markdown":
            data = entry.get("data")
            scan_info = meta.get("pdf_scan_info") or {}
            scan_notice = self._format_pdf_scan_notice(scan_info)

            if not isinstance(data, str) or not data.strip():
                if scan_notice:
                    data = scan_notice
                else:
                    return {"ok": False, "error": "该文档内容为空", "file_no": file_no}

            entry["read_status"] = "read"
            if self._is_doc_type(entry.get("type", "markdown")):
                entry["filename_disclosed"] = True

            content = data

            if scan_notice and scan_notice not in content:
                content += "\n\n" + scan_notice

            return {
                "ok": True,
               "type": "markdown",
                "display_name": self._display_name(entry, file_no),
                "content": content,
               "file_no": file_no,
                "error": None,
            }
    
        elif att_type == "image":
            path = entry.get("data")

            if not path or not isinstance(path, str):
                return {"ok": False, "error": "该图片无有效路径", "file_no": file_no}

            if not os.path.exists(path):
                return {"ok": False, "error": "图片文件已不存在", "file_no": file_no}

            return {
                "ok": True,
                "type": "image",
                "display_name": self._display_name(entry, file_no),
                "content": (
                    "该文件是图片。必须调用 view_cached_image，"
                    "使主模型在下一轮直接查看该图片。"
                ),
                "path": path,
                "file_no": file_no,
                "error": None,
            }
        else:
            return {
                "ok": False,
                "error": f"不支持的文件类型：{att_type}",
                "file_no": file_no,
            }

    # ==================== 用户引用解析 ====================

    def _parse_number(self, s):
        if s is None:
            return None
        s = str(s).strip()
        if s.isdigit():
            return int(s)
        cn_digits = {
            "零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
            "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        }
        if s in cn_digits:
            return cn_digits[s]
        if s == "十":
            return 10
        if "十" in s:
            parts = s.split("十")
            left = parts[0].strip()
            right = parts[1].strip() if len(parts) > 1 else ""
            tens = cn_digits.get(left, 1) if left else 1
            ones = cn_digits.get(right, 0) if right else 0
            return tens * 10 + ones
        return None

    def _extract_count(self, text: str, suffix_pattern: str):
        num_pat = r"([0-9]+|[零一二两三四五六七八九十]+)"
        pat = rf"{num_pat}\s*[个张份]\s*(?:{suffix_pattern})"
        m = re.search(pat, text)
        if m:
            n = self._parse_number(m.group(1))
            if n is not None and n >= 2:
                return n
        return None

    def resolve_file_nos(self, user_msg: str) -> list:
        if not user_msg or self.size == 0:
            return []

        # 1. 精确引用
        refs = self._extract_references(user_msg)
        if refs:
            resolved = []
            for ref in refs:
                file_no = self._resolve_single_ref(ref)
                if file_no is not None and file_no not in resolved:
                    resolved.append(file_no)
            if resolved:
                return resolved

        # 2. 集合引用
        collection = self._extract_collection_references(user_msg)
        if collection:
            resolved = self._resolve_collection(collection)
            if resolved:
                return resolved

        # 3. 文件名模糊匹配
        matched = self._match_by_filename(user_msg)
        if matched:
            return matched

        return []

    def _extract_references(self, text):
        if not text:
            return []
        text_lower = text.lower()
        refs = []
        num_pat = r"([0-9]+|[零一二两三四五六七八九十]+)"

        # 文件编号：文件2 / 2号文件 / 第2个文件
        file_patterns = [
            rf"文件\s*{num_pat}",
            rf"{num_pat}\s*号文件",
            rf"第\s*{num_pat}\s*个\s*文件",
        ]
        for pat in file_patterns:
            for m in re.finditer(pat, text_lower):
                raw_num = None
                for g in m.groups():
                    if g:
                        raw_num = g
                        break
                n = self._parse_number(raw_num)
                if n is not None:
                    refs.append({"kind": "file_no", "num": n})

        # 类型/分组编号：第1张图片 / 第2个表格 / 第3个文档
        type_pattern = rf"第\s*{num_pat}\s*[张个份]\s*(图片|图|表格|excel|文档|pdf|文本)"
        for m in re.finditer(type_pattern, text_lower):
            raw_num = m.group(1)
            word = m.group(2)
            n = self._parse_number(raw_num)
            if n is None:
                continue
            if word in ("图片", "图"):
                refs.append({"kind": "group", "group": "image", "num": n})
            elif word in ("表格", "excel"):
                refs.append({"kind": "group", "group": "table", "num": n})
            elif word == "文档":
                refs.append({"kind": "group", "group": "doc", "num": n})
            elif word == "pdf":
                refs.append({"kind": "type", "type": "pdf", "num": n})
            elif word == "文本":
                refs.append({"kind": "type", "type": "txt", "num": n})

        # 去重
        cleaned = []
        seen = set()
        for ref in refs:
            key = str(sorted(ref.items()))
            if key not in seen:
                seen.add(key)
                cleaned.append(ref)
        return cleaned

    def _extract_collection_references(self, text):
        if not text:
            return None
        text_lower = text.lower()

        if any(k in text_lower for k in ["所有文件", "全部文件", "所有缓存", "全部缓存", "这些文件"]):
            return {"kind": "all_files"}
        if any(k in text_lower for k in ["所有表格", "全部表格", "这些表格"]):
            return {"kind": "all_group", "group": "table"}
        if any(k in text_lower for k in ["所有文档", "全部文档", "这些文档"]):
            return {"kind": "all_group", "group": "doc"}
        if any(k in text_lower for k in ["所有图片", "全部图片", "这些图片"]):
            return {"kind": "all_group", "group": "image"}

        count = self._extract_count(text_lower, "表格|excel")
        if count:
            return {"kind": "latest_group", "group": "table", "count": count}

        count = self._extract_count(text_lower, "pdf")
        if count:
            return {"kind": "latest_type", "type": "pdf", "count": count}

        count = self._extract_count(text_lower, "图片|图")
        if count:
            return {"kind": "latest_group", "group": "image", "count": count}

        count = self._extract_count(text_lower, "文档|文本|txt")
        if count:
            return {"kind": "latest_group", "group": "doc", "count": count}

        return None

    def _resolve_single_ref(self, ref):
        """解析单个引用 → file_no"""
        kind = ref.get("kind")
        if kind == "file_no":
            fn = ref.get("num")
            if self.has(str(fn)):
                return fn
            return None
        elif kind == "group":
            group = ref.get("group")
            num = ref.get("num")
            for key, entry in self._entries.items():
                meta = entry.get("meta") or {}
                if meta.get("group") == group and meta.get("group_no") == num:
                    return entry.get("file_no")
            return None
        elif kind == "type":
            typ = ref.get("type")
            num = ref.get("num")
            for key, entry in self._entries.items():
                meta = entry.get("meta") or {}
                if meta.get("source_format") == typ and meta.get("type_no") == num:
                    return entry.get("file_no")
            return None
        return None

    def _resolve_collection(self, collection):
        """解析集合引用 → [file_no, ...]"""
        kind = collection.get("kind")
        if kind == "all_files":
            return [e.get("file_no") for e in self._entries.values() if e.get("file_no") is not None]
        if kind == "all_group":
            group = collection.get("group")
            return [
                e.get("file_no") for e in self._entries.values()
                if (e.get("meta") or {}).get("group") == group and e.get("file_no") is not None
            ]
        if kind == "all_type":
            typ = collection.get("type")
            return [
                e.get("file_no") for e in self._entries.values()
                if (e.get("meta") or {}).get("source_format") == typ and e.get("file_no") is not None
            ]
        if kind == "latest_group":
            group = collection.get("group")
            count = collection.get("count", 2)
            entries = sorted(
                [e for e in self._entries.values() if (e.get("meta") or {}).get("group") == group],
                key=lambda e: e.get("created_at", 0), reverse=True
            )
            return [e.get("file_no") for e in entries[:count] if e.get("file_no") is not None]
        if kind == "latest_type":
            typ = collection.get("type")
            count = collection.get("count", 2)
            entries = sorted(
                [e for e in self._entries.values() if (e.get("meta") or {}).get("source_format") == typ],
                key=lambda e: e.get("created_at", 0), reverse=True
            )
            return [e.get("file_no") for e in entries[:count] if e.get("file_no") is not None]
        return []

    def _match_by_filename(self, user_msg):
        """文件名评分匹配"""
        if not user_msg or self.size == 0:
            return []

        user_lower = str(user_msg).lower()
        user_tokens = self._tokenize_for_match(user_lower)

        scored = []

        for key, entry in self._entries.items():
            file_no = entry.get("file_no")

            if file_no is None:
                continue

            score = self._score_filename_candidate(
                user_lower=user_lower,
                user_tokens=user_tokens,
                entry=entry,
            )

            if score >= self._MATCH_MIN_SCORE:
                scored.append((score, file_no))

        if not scored:
            return []

        if len(scored) > 1:
            return []

        return [scored[0][1]]

    def _score_filename_candidate(self, user_lower: str, user_tokens: set, entry: dict) -> int:
        meta = entry.get("meta") or {}
        filename = (meta.get("filename") or "").lower().strip()

        if not filename:
            return 0

        base_name = os.path.splitext(filename)[0].lower().strip()

        if not base_name:
            return 0

        if self._is_undisclosed_doc(entry):

            if len(base_name) <= self._MATCH_SHORT_BASE_LEN:
                if filename in user_lower:
                    return 95
                return 0

            if filename in user_lower or base_name in user_lower:
                return 95

            return 0

        if filename in user_lower:
            return 100

        # 主名过短：禁止模糊匹配
        if len(base_name) <= self._MATCH_SHORT_BASE_LEN:
            return 0

        # 完整主名出现
        if base_name in user_lower:
            return 90

        # 关键词评分
        file_tokens = self._tokenize_for_match(base_name)

        if not file_tokens:
            return 0

        meaningful_tokens = {
            token for token in file_tokens
            if len(token) >= 2
        }

        if not meaningful_tokens:
            return 0

        common_tokens = meaningful_tokens & user_tokens

        if not common_tokens:
            return 0

        coverage = len(common_tokens) / len(meaningful_tokens)

        if len(common_tokens) < 2 and coverage < 0.8:
            return 0

        return int(coverage * 60)

    # ==================== 生命周期 ====================

    def remove(self, key: str):
        self._entries.pop(key, None)

    def remove_by_file_no(self, file_no):
        """按编号精确删除缓存池条目"""
        if file_no is not None:
            self._entries.pop(str(file_no), None)
        
    def tick(self):
        """每轮对话结束后调用：递减所有条目的 ttl，移除过期条目。"""
        expired_keys = []
        for key, entry in self._entries.items():
            entry["ttl_turns"] -= 1
            if entry["ttl_turns"] <= 0:
                expired_keys.append(key)
        for key in expired_keys:
            del self._entries[key]
        if expired_keys:
            print(f"[CachePool] TTL 过期，移除 {len(expired_keys)} 个条目，"
                  f"剩余 {len(self._entries)} 个")

    def clear(self):
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    def keys(self):
        return list(self._entries.keys())
