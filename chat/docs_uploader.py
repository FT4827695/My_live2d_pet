import os
import threading
import time
import queue
import win32ui
import win32con
import pandas as pd
import re
import shutil
from PIL import Image
import fitz  # PyMuPDF
from markitdown import MarkItDown


class DocsUploader:
    DEFAULT_FILE_CACHE_TURNS = 30
    MAX_FILE_SIZE = 100 * 1024 * 1024
    PREPROCESS_TIMEOUT = 60

    GROUP_MAP = {
        "image": "image",
        "excel": "table",
        "markdown": "doc",
    }
    GROUP_UNIT = {
        "image": "张图片",
        "table": "个表格",
        "doc": "个文档",
    }
    TYPE_UNIT = {
        "image": "张图片",
        "excel": "个表格",
        "markdown": "个文档",
    }

    def __init__(self, upload_btn_rect, clear_btn_size=24, font_provider=None,
                 scale=1.0, font_sizes=None):
        self.upload_rect = upload_btn_rect
        self.clear_btn_size = clear_btn_size
        self.font_provider = font_provider or self._default_font
        self.scale = scale
        self.font_sizes = font_sizes or {}
        self.clear_rect = None
        self.last_dir = None
        self._clear_hover = False
        self.badge_rect = None
        self._result_queue = queue.Queue()
        self._thread = None

        # ===== UI 暂存区（存轻量元数据）=====
        self.file_cache = []
        
        # 全局编号
        self.next_file_no = 1
        self.group_counters = {"image": 0, "table": 0, "doc": 0}
        self.type_counters = {"image": 0, "excel": 0, "markdown": 0}

        self._preprocessing = False
        self._preprocess_start_time = None

        # 拖拽 / 粘贴文件排队
        self._pending_batches = queue.Queue()
        self._drain_lock = threading.RLock()
        self._drain_thread = None

    def _default_font(self, size):
        from PIL import ImageFont
        try:
            return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
        except Exception:
            return ImageFont.load_default()

    # ===================== 异步文件选择 =====================

    def select_file_async(self):
        if self._thread and self._thread.is_alive():
            return
        if self._preprocessing:
            return
        self._result_queue = queue.Queue()
        self._thread = threading.Thread(target=self._file_dialog_thread, daemon=True)
        self._thread.start()

    def _file_dialog_thread(self):
        try:
            filter_str = (
                "图片文件 (*.jpg;*.jpeg;*.png;*.bmp;*.gif)|*.jpg;*.jpeg;*.png;*.bmp;*.gif|"
                "Excel文件 (*.xlsx;*.xls)|*.xlsx;*.xls|"
                "文档文件 (*.txt;*.pdf;*.docx;*.pptx;*.csv;*.html)|*.txt;*.pdf;*.docx;*.pptx;*.csv;*.html|"
                "所有文件 (*.*)|*.*||"
            )
            flags = 0
            for name, value in (
                ("OFN_ALLOWMULTISELECT", 0x00000200),
                ("OFN_FILEMUSTEXIST", 0x00001000),
                ("OFN_PATHMUSTEXIST", 0x00000800),
                ("OFN_EXPLORER", 0x00080000),
            ):
                flags |= getattr(win32con, name, value)
            dlg = win32ui.CreateFileDialog(1, None, None, flags, filter_str, None)
            if dlg.DoModal() == win32con.IDOK:
                paths = self._get_selected_paths(dlg)
                if not paths:
                    self._result_queue.put(None)
                    return
                self.last_dir = os.path.dirname(paths[0])
                self._preprocessing = True
                self._preprocess_start_time = time.time()
                results = []
                for file_path in paths:
                    result = self._process_single_file(file_path)
                    results.append(result)
                self._preprocessing = False
                self._preprocess_start_time = None
                self._result_queue.put(results)
            else:
                self._result_queue.put(None)
        except Exception as e:
            print(f"[DocsUploader] 文件对话框异常: {e}")
            self._preprocessing = False
            self._preprocess_start_time = None
            self._result_queue.put(None)

    def start_upload_paths(self, paths):
        paths = list(paths or [])
        files = []
        dirs = []
        for p in paths:
            if os.path.isdir(p):
                dirs.append(p)
            elif os.path.isfile(p):
                files.append(p)

        if files:
            self._pending_batches.put(files)
            self._ensure_drain_thread()

        return {"accepted": len(files), "dirs": dirs}
    
    def _ensure_drain_thread(self):
        with self._drain_lock:
            if self._drain_thread is not None and self._drain_thread.is_alive():
                return
            if self._pending_batches.empty():
                return
            self._drain_thread = threading.Thread(
                target=self._drain_pending_batches, daemon=True
            )
            self._drain_thread.start()

    def _drain_pending_batches(self):
        try:
            self._preprocessing = True
            self._preprocess_start_time = time.time()

            while True:
                try:
                    paths = self._pending_batches.get_nowait()
                except queue.Empty:
                    break

                try:
                    results = [self._process_single_file(p) for p in paths]
                except Exception as e:
                    print(f"[DocsUploader] 批次处理异常: {e}")
                    results = [
                        {
                            "error": True,
                            "reason_code": "preprocess_error",
                            "reason": f"批次处理异常: {e}",
                            "path": p,
                        }
                        for p in paths
                    ]

                self._result_queue.put(results)
        except Exception as e:
            print(f"[DocsUploader] 排队处理异常: {e}")
        finally:
            self._preprocessing = False
            self._preprocess_start_time = None
            with self._drain_lock:
                self._drain_thread = None
            if not self._pending_batches.empty():
                self._ensure_drain_thread()            

    def _get_selected_paths(self, dlg):
        try:
            raw_paths = list(dlg.GetPathNames())
        except Exception:
            try:
                one_path = dlg.GetPathName()
                raw_paths = [one_path] if one_path else []
            except Exception:
                raw_paths = []
        paths = []
        for p in raw_paths:
            if p:
                p = str(p).strip()
                if p:
                    paths.append(p)
        if len(paths) >= 2 and os.path.isdir(paths[0]):
            base_dir = paths[0]
            merged_paths = []
            for p in paths[1:]:
                if os.path.isabs(p):
                    merged_paths.append(p)
                else:
                    merged_paths.append(os.path.join(base_dir, p))
            paths = merged_paths
        return paths

    def _process_single_file(self, file_path):
        try:
            if os.path.getsize(file_path) > self.MAX_FILE_SIZE:
                return {
                    "error": True,
                    "reason_code": "file_too_large",
                    "reason": f"{os.path.basename(file_path)} 体积超限（100MB），上传失败。",
                }
        except OSError:
            pass
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._process_file, file_path)
                return future.result(timeout=self.PREPROCESS_TIMEOUT)
        except FuturesTimeout:
            return {
                "error": True,
                "reason_code": "preprocess_timeout",
                "reason": f"{os.path.basename(file_path)} 预处理超时（{self.PREPROCESS_TIMEOUT}秒），上传失败。",
            }
        except Exception as e:
            print(f"[DocsUploader] 预处理异常: {e}")
            return {
                "error": True,
                "reason_code": "preprocess_error",
                "reason": f"{os.path.basename(file_path)} 预处理失败。",
            }

    # ===================== 文件预处理 =====================

    def _process_file(self, file_path):
        if not os.path.exists(file_path):
            return {"error": True, "reason_code": "file_not_found", "reason": "文件不存在。"}
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.gif']:
            return {
                'type': 'image', 'path': file_path,
                'content': None, 'dataframe': None,
                'pdf_mode': None, 'temp_dir': None,
            }
        elif ext in ['.xlsx', '.xls']:
            text, df = self._preprocess_excel(file_path)
            if text is not None and df is not None:
                return {
                    'type': 'excel', 'path': file_path,
                    'content': text, 'dataframe': df,
                    'pdf_mode': None, 'temp_dir': None,
                }
            return {"error": True, "reason_code": "excel_parse", "reason": "这个表格读取失败了。"}
        elif ext in ['.txt', '.pdf', '.docx', '.pptx', '.csv', '.html']:
            try:
                md = MarkItDown()
                result = md.convert(file_path)
                markdown_content = result.text_content or ""

                n_ctx = int(os.environ.get("XLLAMA_CTX", 8192))
                char_limit = int(n_ctx * 0.8 * 4)
                MAX_CHARS = min(char_limit, 50000)

                if len(markdown_content) > MAX_CHARS:
                    markdown_content = markdown_content[:MAX_CHARS] + "\n\n[文档过长，截断]"

                pdf_scan_info = None

                if ext == '.pdf':
                    pdf_scan_info = self._detect_pdf_scan_pages(file_path)

                    if pdf_scan_info and pdf_scan_info.get("has_scanned_pages"):
                        scan_notice = self._build_pdf_scan_notice(pdf_scan_info)
                        if scan_notice:
                            if not markdown_content.strip():
                                markdown_content = scan_notice
                            else:
                                markdown_content += "\n\n" + scan_notice

                return {
                    'type': 'markdown',
                    'source_format': ext.lstrip('.'),
                    'content_type': 'markdown',
                    'path': file_path,
                    'content': markdown_content,
                    'dataframe': None,
                    'pdf_mode': None,
                    'temp_dir': None,
                    'pdf_scan_info': pdf_scan_info,
                }

            except Exception as e:
                if ext == '.pdf':
                    try:
                        pdf_scan_info = self._detect_pdf_scan_pages(file_path)
                        if pdf_scan_info and pdf_scan_info.get("has_scanned_pages"):
                            markdown_content = self._build_pdf_scan_notice(pdf_scan_info)
                            if not markdown_content:
                                markdown_content = "[系统标记] 该PDF未提取到有效文本，疑似扫描件。"

                            return {
                                'type': 'markdown',
                                'source_format': 'pdf',
                                'content_type': 'markdown',
                                'path': file_path,
                                'content': markdown_content,
                                'dataframe': None,
                                'pdf_mode': None,
                                'temp_dir': None,
                                'pdf_scan_info': pdf_scan_info,
                            }
                    except Exception:
                        pass

                print(f"[DocsUploader] MarkItDown 转换失败: {e}")
                return {"error": True, "reason_code": "markitdown_failed", "reason": f"文档解析失败: {e}"}
        else:
            return {"error": True, "reason_code": "unsupported_type", "reason": "这个文件类型暂时不支持。"}

    def _find_header_row(self, file_path, sheet_name):
        try:
            df_raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None, nrows=20)
            if df_raw.empty:
                return 0
            non_null_counts = df_raw.count(axis=1)
            valid_counts = non_null_counts[non_null_counts >= 3]
            if not valid_counts.empty:
                return valid_counts.idxmax()
            return 0
        except Exception:
            return 0

    def _preprocess_excel(self, file_path):
        try:
            all_text = []
            dataframes = []
            with pd.ExcelFile(file_path) as xls:
                sheet_names = xls.sheet_names[:3]
                for sheet_name in sheet_names:
                    header_row = self._find_header_row(file_path, sheet_name)
                    df = pd.read_excel(xls, sheet_name=sheet_name, header=header_row)
                    df.columns = [str(c).replace('\n', ' ').strip() for c in df.columns if pd.notna(c)]
                    df = df.dropna(axis=1, how='all')
                    for col in df.columns:
                        if df[col].dtype == 'object':
                            numeric_col = pd.to_numeric(df[col], errors='coerce')
                            if numeric_col.notna().sum() > df[col].notna().sum() * 0.8:
                                df[col] = numeric_col
                    csv_str = df.to_csv(index=False)
                    all_text.append(f"【Sheet: {sheet_name}】\n{csv_str}")
                    dataframes.append(df)
            combined_text = "\n\n".join(all_text)
            combined_df = pd.concat(dataframes, ignore_index=True) if dataframes else None
            return combined_text, combined_df
        except Exception as e:
            print(f"[DocsUploader] Excel预处理失败: {e}")
            return None, None

    def _detect_pdf_scan_pages(self, file_path, max_analyze_pages=50, list_cap=30):

        info = {
            "detected": False,
            "page_count": 0,
            "analyzed_pages": 0,
            "partial_analysis": False,
            "has_scanned_pages": False,
            "scan_mode": "unknown",
            "scanned_pages": [],
            "scanned_page_count": 0,
            "scanned_pages_truncated": False,
            "detection_method": "pymupdf_text_image_heuristic",
        }

        try:
            doc = fitz.open(file_path)
        except Exception as e:
            print(f"[DocsUploader] PDF扫描检测打开失败: {e}")
            return info

        try:
            page_count = doc.page_count
            info["page_count"] = page_count
            info["detected"] = True

            if page_count <= 0:
                info["scan_mode"] = "none"
                return info

            limit = min(page_count, max_analyze_pages)
            info["partial_analysis"] = page_count > limit

            start_ts = time.time()
            scanned = []
            analyzed = 0

            for i in range(limit):
                if time.time() - start_ts > 20.0:
                    info["partial_analysis"] = True
                    break

                try:
                    page = doc.load_page(i)

                    text = page.get_text("text") or ""
                    text_len = len(re.sub(r"\s+", "", text))

                    page_area = max(
                        1.0,
                        float(page.rect.width) * float(page.rect.height)
                    )

                    image_area = 0.0
                    for img in page.get_image_info():
                        bbox = img.get("bbox") or (0, 0, 0, 0)
                        try:
                            w = max(0.0, float(bbox[2]) - float(bbox[0]))
                            h = max(0.0, float(bbox[3]) - float(bbox[1]))
                            image_area += min(w * h, page_area)
                        except Exception:
                            pass

                    coverage = image_area / page_area

                    if text_len < 10 and coverage >= 0.90:
                        scanned.append(i + 1)

                    analyzed += 1

                except Exception:
                    continue

            info["analyzed_pages"] = analyzed
            info["scanned_page_count"] = len(scanned)

            if len(scanned) > list_cap:
                info["scanned_pages"] = scanned[:list_cap]
                info["scanned_pages_truncated"] = True
            else:
                info["scanned_pages"] = scanned

            info["has_scanned_pages"] = bool(scanned)

            if analyzed == 0:
                info["scan_mode"] = "unknown"
            elif not scanned:
                info["scan_mode"] = "none"
            elif len(scanned) >= max(1, int(analyzed * 0.8)):
                info["scan_mode"] = "full"
            else:
                info["scan_mode"] = "partial"

        except Exception as e:
            print(f"[DocsUploader] PDF扫描检测失败: {e}")
            info["scan_mode"] = "unknown"
        finally:
            try:
                doc.close()
            except Exception:
                pass

        return info

    def _build_pdf_scan_notice(self, scan_info):
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

    # ===================== 缓存管理 =====================

    def add_attachment(self, result):
        if not result or result.get("error"):
            return None
        raw_type = result.get("type")
        if raw_type not in self.GROUP_MAP:
            return None
        group = self.GROUP_MAP[raw_type]

        file_no = self.next_file_no
        self.next_file_no += 1
        self.group_counters[group] += 1
        self.type_counters[raw_type] += 1
        group_no = self.group_counters[group]
        type_no = self.type_counters[raw_type]

        # 编号信息回写
        result["file_no"] = file_no
        result["group"] = group
        result["group_no"] = group_no
        result["type_no"] = type_no

        type_labels = {"image": "图片", "excel": "表格", "markdown": "文档"}
        spoken_label = type_labels.get(raw_type, "文件")
        exact_label = spoken_label
        
        result["spoken_label"] = spoken_label
        result["exact_label"] = exact_label
        result["filename"] = os.path.basename(result.get("path") or "")
        result["ttl_turns"] = self.DEFAULT_FILE_CACHE_TURNS

        ui_entry = {
            "file_no": file_no,
            "filename": result["filename"],
            "group": group,
            "spoken_label": spoken_label,
            "exact_label": exact_label,
        }
        self.file_cache.append(ui_entry)
        return result

    def on_turn_finished(self):
        self.file_cache.clear()

    def clear_all(self, reset_counters=True):
        return self.clear_staged(reset_counters=reset_counters)

    def clear(self):
        return self.clear_staged(reset_counters=True)

    def shutdown(self):
        return self.clear_staged(reset_counters=True)

    def _reset_counters(self):
        self.group_counters = {"image": 0, "table": 0, "doc": 0}
        self.type_counters = {"image": 0, "excel": 0, "markdown": 0}

    def clear_staged(self, reset_counters=True):
        removed_file_nos = []
        for entry in list(self.file_cache):
            fn = entry.get("file_no")
            if fn is not None:
                removed_file_nos.append(fn)
            self.file_cache.remove(entry)
        if reset_counters:
            self._reset_counters()
        return removed_file_nos

    def reset_upload_state(self):
        self.file_cache.clear()
        self._reset_counters()
        self.next_file_no = 1
        
    # ===================== 查询接口 =====================

    def has_file(self):
        return len(self.file_cache) > 0

    def get_latest_file_no(self):
        if not self.file_cache:
            return None
        return self.file_cache[-1].get("file_no")

    # ===================== 结果消费 =====================

    def consume_results(self):
        try:
            raw = self._result_queue.get_nowait()
        except queue.Empty:
            return [], []
        if raw is None:
            return [], []
        if isinstance(raw, dict):
            raw = [raw]
        attachments = []
        errors = []
        for result in raw:
            if not result:
                continue
            if result.get("error"):
                errors.append(result)
                continue
            entry = self.add_attachment(result)
            if entry:
                attachments.append(entry)
            else:
                errors.append({
                    "error": True,
                    "reason_code": "add_cache_failed",
                    "reason": "文件加入缓存失败。",
                    "path": result.get("path"),
                })
        return attachments, errors

    # ===================== 异步状态接口 =====================

    def has_result(self):
        return not self._result_queue.empty()

    def is_preprocessing(self):
        return self._preprocessing

    def get_preprocess_elapsed(self):
        if not self._preprocessing or self._preprocess_start_time is None:
            return 0.0
        return time.time() - self._preprocess_start_time

    # ===================== UI 绘制 =====================

    def update_hover(self, x, y):
        if x is None or y is None or not self.has_file():
            new_hover = False
        else:
            ux, uy, uw, uh = self.upload_rect
            in_upload = ux <= x <= ux + uw and uy <= y <= uy + uh
            in_badge = self._point_in_rect(self.badge_rect, x, y)
            in_clear = self._point_in_rect(self.clear_rect, x, y)
            new_hover = in_upload or in_badge or in_clear
        if new_hover != self._clear_hover:
            self._clear_hover = new_hover
            return True
        return False

    def hide_hover(self):
        if self._clear_hover:
            self._clear_hover = False
            return True
        return False

    def _point_in_rect(self, rect, x, y):
        if not rect:
            return False
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def draw(self, draw, width, height, get_font_func=None):
        func = get_font_func or self.font_provider
        s = self.scale
        x, y, w, h = self.upload_rect
        draw.rectangle([x, y, x + w, y + h], fill=(60, 180, 100),
                       outline=(200, 200, 200), width=max(1, int(1 * s)))
        icon = "+"
        font_icon = func(self.font_sizes.get('upload_icon', 30))
        bbox = font_icon.getbbox(icon)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((x + (w - tw) // 2 - bbox[0], y + (h - th) // 2 - bbox[1]),
                  icon, font=font_icon, fill=(255, 255, 255))
        if self.has_file():
            badge_r = int(12 * s)
            badge_x = x + badge_r - int(2 * s)
            badge_y = y + badge_r - int(2 * s)
            draw.ellipse((badge_x - badge_r, badge_y - badge_r,
                          badge_x + badge_r, badge_y + badge_r), fill=(255, 50, 50))
            self.badge_rect = (badge_x - badge_r, badge_y - badge_r,
                               badge_x + badge_r, badge_y + badge_r)
            count = len(self.file_cache)
            num_text = str(count) if count <= 9 else "9+"
            font_num = func(self.font_sizes.get('badge_num', 16))
            bbox = font_num.getbbox(num_text)
            tw_num = bbox[2] - bbox[0]
            th_num = bbox[3] - bbox[1]
            draw.text((badge_x - tw_num // 2, badge_y - th_num // 2 - int(4 * s)),
                      num_text, font=font_num, fill=(255, 255, 255))
            if self._clear_hover:
                clear_btn_size = self.clear_btn_size
                clear_x = badge_x - badge_r - int(4 * s) - clear_btn_size
                clear_y = badge_y - clear_btn_size // 2
                self.clear_rect = (clear_x, clear_y, clear_x + clear_btn_size, clear_y + clear_btn_size)
                draw.rectangle(self.clear_rect, fill=(200, 80, 80),
                               outline=(255, 255, 255), width=max(1, int(1 * s)))
                clear_icon = "×"
                font_clear = func(self.font_sizes.get('clear_icon', 18))
                bbox = font_clear.getbbox(clear_icon)
                tw_c = bbox[2] - bbox[0]
                th_c = bbox[3] - bbox[1]
                draw.text((clear_x + (clear_btn_size - tw_c) // 2 + int(1 * s),
                           clear_y + (clear_btn_size - th_c) // 2 - int(8 * s)),
                          clear_icon, font=font_clear, fill=(255, 255, 255))
            else:
                self.clear_rect = None
        else:
            self.clear_rect = None
            self.badge_rect = None

    def hit_test(self, x, y):
        ux, uy, uw, uh = self.upload_rect
        if ux <= x <= ux + uw and uy <= y <= uy + uh:
            return 'upload'
        if self.clear_rect and self.clear_rect[0] <= x <= self.clear_rect[2] and \
           self.clear_rect[1] <= y <= self.clear_rect[3]:
            return 'clear'
        return None
