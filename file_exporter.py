# -*- coding: utf-8 -*-
"""
file_exporter.py
独立文件导出模块。
"""

import os
import re
import io
import csv
import json
import datetime
from typing import Any, Dict, Optional

try:
    import pandas as pd
except Exception:
    pd = None


EXPORT_TABLE_EXTENSIONS = {".xlsx", ".csv"}

# ===================== 桌面路径 =====================

def get_desktop_dir(create: bool = True) -> Optional[str]:
    candidates = []

    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            if value:
                candidates.append(os.path.expandvars(value))
    except Exception:
        pass

    home = os.path.expanduser("~")
    candidates.extend([
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(home, "OneDrive", "桌面"),
        os.path.join(home, "桌面"),
    ])

    seen = set()
    for path in candidates:
        if not path:
            continue
        path = os.path.normpath(path)
        if path in seen:
            continue
        seen.add(path)

        try:
            if os.path.isdir(path):
                return path
            if create:
                os.makedirs(path, exist_ok=True)
                return path
        except Exception:
            continue

    return None


# ===================== 文件名工具 =====================

def sanitize_filename(name: str, max_length: int = 80) -> str:
    if not name:
        return ""

    name = os.path.basename(str(name))
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")

    if len(name) > max_length:
        name = name[:max_length].strip(" ._")

    return name


def _unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    index = 1

    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({index}){ext}")
        index += 1

    return candidate


def _sanitize_sheet_name(name: str, index: int = 1) -> str:
    name = str(name or "").strip()
    name = re.sub(r"[\\/:*?\[\]]", "_", name)
    name = name[:31].strip(" '_")

    if not name:
        name = f"Sheet{index}"

    return name


# ===================== 主导出接口 =====================

def save_exportable(
    payload: Dict[str, Any],
    requested_format: Optional[str] = None
) -> Dict[str, Any]:
    try:
        if not payload:
            return _fail("empty_payload")

        desktop = get_desktop_dir(True)
        if not desktop:
            return _fail("desktop_unavailable")

        data = payload.get("data")
        kind = payload.get("kind", "text")
        filename_base = (
            payload.get("filename_base")
            or payload.get("source_label")
            or "导出结果"
        )

        normalized_data, resolved_kind = _normalize_export_data(
            data,
            kind,
            requested_format
        )

        ext = _resolve_extension(resolved_kind, requested_format)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = sanitize_filename(filename_base) or "导出结果"
        filename = f"{base_name}_{timestamp}{ext}"
        path = _unique_path(desktop, filename)

        if ext == ".txt":
            if isinstance(normalized_data, str):
                normalized_data = _clean_export_text(normalized_data)
            _write_text(path, normalized_data)
            
        elif ext == ".json":
            _write_json(path, normalized_data)

        elif ext == ".csv":
            _write_csv(path, normalized_data)

        elif ext == ".xlsx":
            try:
                _write_xlsx(path, normalized_data)
            except Exception as e:
                print(f"[FileExporter] xlsx写入失败，尝试回退: {e}")

                df = _to_single_dataframe(normalized_data)
                if df is not None:
                    path = _unique_path(desktop, f"{base_name}_{timestamp}.csv")
                    _write_csv(path, df)
                    ext = ".csv"
                else:
                    path = _unique_path(desktop, f"{base_name}_{timestamp}.txt")
                    _write_text(path, normalized_data)
                    ext = ".txt"

        bubble_kind = "table" if ext in EXPORT_TABLE_EXTENSIONS else "doc"

        return {
            "success": True,
            "path": path,
            "format": ext,
            "bubble_kind": bubble_kind,
        }

    except Exception as e:
        print(f"[FileExporter] 导出失败: {e}")
        return _fail("write_failed")


def _fail(error_code: str) -> Dict[str, Any]:
    return {
        "success": False,
        "path": None,
        "format": None,
        "bubble_kind": "doc",
        "error_code": error_code,
    }


# ===================== 数据归一化 =====================

def _normalize_export_data(
    data: Any,
    kind: str,
    requested_format: Optional[str]
):
    if data is None:
        return "", "text"

    if pd is not None:
        if isinstance(data, pd.DataFrame):
            return data, "table"
        if isinstance(data, pd.Series):
            return data, "table"

    if (
        pd is not None
        and isinstance(data, dict)
        and data
        and all(isinstance(v, pd.DataFrame) for v in data.values())
    ):
        return data, "table_multi"

    # 字符串
    if isinstance(data, str):
        text = data.strip()

        if requested_format == "json":
            obj = _try_json_text(text)
            if obj is not None:
                return obj, "json"

        if requested_format in ("excel", "csv", "table"):
            df = _try_text_to_dataframe(text)
            if df is not None:
                return df, "table"

        if kind in ("table", "auto_struct"):
            obj = _try_json_text(text)
            if obj is not None:
                df = _try_struct_to_dataframe(obj)
                if df is not None:
                    return df, "table"
                return obj, "json"

            df = _try_text_to_dataframe(text)
            if df is not None:
                return df, "table"

        return data, "text"

    # dict / list
    if isinstance(data, (dict, list)):
        if requested_format in ("excel", "csv", "table") or kind in ("table", "auto_struct"):
            df = _try_struct_to_dataframe(data)
            if df is not None:
                if isinstance(df, dict):
                    return df, "table_multi"
                return df, "table"

        return data, "json"

    return str(data), "text"


def _resolve_extension(kind: str, requested_format: Optional[str]) -> str:
    if requested_format == "txt":
        return ".txt"
    if requested_format == "csv":
        return ".csv"
    if requested_format == "json":
        return ".json"
    if requested_format == "excel":
        return ".xlsx"

    if kind in ("table", "table_multi"):
        return ".xlsx"

    if kind == "json":
        return ".json"

    return ".txt"


# ===================== 写入器 =====================

def _write_text(path: str, data: Any) -> None:
    text = _coerce_text(data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path: str, data: Any) -> None:
    if pd is not None:
        if isinstance(data, pd.DataFrame):
            data = json.loads(data.to_json(orient="records", force_ascii=False))
        elif isinstance(data, pd.Series):
            data = data.to_dict()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _write_csv(path: str, data: Any) -> None:
    df = _to_single_dataframe(data)

    if df is None:
        text = _coerce_text(data)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        return

    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_xlsx(path: str, data: Any) -> None:
    if pd is None:
        raise RuntimeError("pandas 不可用，无法写入 Excel")

    # 多 sheet：dict[str, DataFrame]
    if (
        isinstance(data, dict)
        and data
        and all(isinstance(v, pd.DataFrame) for v in data.values())
    ):
        used_sheet_names = set()

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for idx, (sheet_name, df) in enumerate(data.items(), start=1):
                safe_name = _sanitize_sheet_name(sheet_name, idx)

                # 避免重名 sheet
                original_name = safe_name
                suffix = 1
                while safe_name.lower() in used_sheet_names:
                    safe_name = f"{original_name[:28]}_{suffix}"
                    suffix += 1
                used_sheet_names.add(safe_name.lower())

                df.to_excel(writer, index=False, sheet_name=safe_name)
        return

    # 单 sheet
    df = _to_single_dataframe(data)
    if df is None:
        raise ValueError("无法转换为 Excel 表格数据")

    df.to_excel(path, index=False, engine="openpyxl")


# ===================== 转换辅助 =====================

def _coerce_text(data: Any) -> str:
    if pd is not None:
        if isinstance(data, pd.DataFrame):
            return data.to_csv(index=False)
        if isinstance(data, pd.Series):
            return data.to_string()

    if isinstance(data, (dict, list)):
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(data)

    return str(data)

def _clean_export_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text

    original = text.strip()

    bad_sentence_patterns = [
        r"已(?:经|为|将|把)?(?:您|你)?.*?(?:保存|存储|导出|写入|生成|整理)",
        r"文件已保存.*",
        r"保存(?:到|至|为|成)(?:桌面|文件|本地)?.*",
        r"整理(?:为|成).*?(?:并|后)?保存.*",
    ]

    parts = re.split(r"(?<=[。．.!！?？\n])", text)
    kept_parts = []

    for part in parts:
        s = part.strip()
        if not s:
            kept_parts.append(part)
            continue

        bad = False

        if len(s) <= 80:
            for pat in bad_sentence_patterns:
                if re.search(pat, s, flags=re.IGNORECASE):
                    bad = True
                    break

        if not bad:
            kept_parts.append(part)

    cleaned = "".join(kept_parts)

    # 再按行过滤
    lines = cleaned.splitlines()
    cleaned_lines = []

    line_bad_patterns = [
        r"^(好的|收到|明白|了解|可以)[，,。.!！~～\s]*",
        r"^已(?:经|为|将|把)?(?:您|你)?.*?(?:保存|存储|导出|写入|生成|整理)",
        r"^文件已保存.*",
        r"^.*保存(?:到|至|为|成)(?:桌面|文件|本地).*",
    ]

    for line in lines:
        s = line.strip()
        if not s:
            cleaned_lines.append(line)
            continue

        bad = False

        if len(s) <= 80:
            for pat in line_bad_patterns:
                if re.search(pat, s, flags=re.IGNORECASE):
                    bad = True
                    break

        if not bad:
            cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()

    cleaned = re.sub(
        r"^(好的|收到|明白|了解|可以)[，,。.!！~～\s]*",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    return cleaned if cleaned.strip() else original

def _to_single_dataframe(data: Any):
    if pd is None:
        return None

    try:
        import numpy as np
        if isinstance(data, (int, float, np.number)):
            return pd.DataFrame({"结果": [float(data)]})
    except Exception:
        if isinstance(data, (int, float)):
            return pd.DataFrame({"结果": [float(data)]})

    if isinstance(data, pd.DataFrame):
        return data

    if isinstance(data, pd.Series):
        return data.to_frame()

    if isinstance(data, dict):
        # dict[str, DataFrame] 合并成单表，兜底用
        if data and all(isinstance(v, pd.DataFrame) for v in data.values()):
            frames = []
            try:
                for name, df in data.items():
                    tmp = df.copy()
                    tmp.insert(0, "_sheet", str(name))
                    frames.append(tmp)
                if frames:
                    return pd.concat(frames, ignore_index=True)
            except Exception:
                return None

        try:
            df = pd.DataFrame(data)
            if not df.empty:
                return df
        except Exception:
            pass

        try:
            df = pd.DataFrame([data])
            if not df.empty:
                return df
        except Exception:
            return None

    if isinstance(data, list):
        try:
            df = pd.DataFrame(data)
            if not df.empty:
                return df
        except Exception:
            pass

        try:
            df = pd.DataFrame([data])
            if not df.empty:
                return df
        except Exception:
            return None

    if isinstance(data, str):
        s = data.strip()

        if s:
            try:
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
                    return pd.DataFrame({"结果": [float(s)]})
            except Exception:
                pass

        return _try_text_to_dataframe(data)

    return None

def _try_struct_to_dataframe(obj: Any):
    if pd is None:
        return None

    if isinstance(obj, pd.DataFrame):
        return obj

    if isinstance(obj, pd.Series):
        return obj.to_frame()

    if isinstance(obj, dict):
        if not obj:
            return None

        if all(isinstance(v, pd.DataFrame) for v in obj.values()):
            return obj

        try:
            df = pd.DataFrame(obj)
            if not df.empty:
                return df
        except Exception:
            pass

        try:
            df = pd.DataFrame([obj])
            if not df.empty:
                return df
        except Exception:
            return None

    if isinstance(obj, list):
        try:
            df = pd.DataFrame(obj)
            if not df.empty:
                return df
        except Exception:
            pass

        try:
            df = pd.DataFrame([obj])
            if not df.empty:
                return df
        except Exception:
            return None

    return None


def _try_json_text(text: str):
    if not text:
        return None

    s = text.strip()
    if not (
        (s.startswith("{") and s.endswith("}"))
        or (s.startswith("[") and s.endswith("]"))
    ):
        return None

    try:
        return json.loads(s)
    except Exception:
        return None


def _try_text_to_dataframe(text: str):
    if not text or pd is None:
        return None
    
    stripped = text.strip()
    if not stripped or "\n" not in stripped:
        return None

    lines = stripped.splitlines()
    fixed_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        if fixed_lines and not fixed_lines[-1].endswith("|") and line.startswith("|"):
            fixed_lines[-1] += " " + line
        else:
            fixed_lines.append(line)

    if any("|" in line for line in fixed_lines):
        rows = []
        for line in fixed_lines:
            if "|" not in line:
                continue
            
            core = line.strip().strip("|")
            cells = [c.strip() for c in core.split("|")]
            
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != ""):
                continue
                
            if any(c != "" for c in cells):
                rows.append(cells)
                
        if len(rows) >= 2:
            max_cols = max(len(r) for r in rows)
            rows = [r + [""] * (max_cols - len(r)) for r in rows]
            
            header = rows[0]
            seen = {}
            unique_header = []
            for h in header:
                h = str(h).strip() or "列"
                if h in seen:
                    seen[h] += 1
                    unique_header.append(f"{h}_{seen[h]}")
                else:
                    seen[h] = 0
                    unique_header.append(h)
                    
            return pd.DataFrame(rows[1:], columns=unique_header)

    sample = stripped[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        df = pd.read_csv(io.StringIO(stripped), dialect=dialect)
        if not df.empty and len(df.columns) > 1:
            return df
    except Exception:
        pass
    
    try:
        df = pd.read_csv(io.StringIO(stripped), sep=None, engine="python")
        if not df.empty and len(df.columns) > 1:
            return df
    except Exception:
        pass
        
    return None
