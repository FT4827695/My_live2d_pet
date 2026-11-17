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
import traceback
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
except Exception:
    pd = None

try:
    import numpy as np
except Exception:
    np = None


EXPORT_TABLE_EXTENSIONS = {".xlsx", ".csv"}

_BOILERPLATE_VERBS = r"(?:保存|存储|导出|写入)"
# 数值化阈值
_NUMERIC_RATIO = 0.8
_NULL_TOKENS = {"", "nan", "none", "null", "na", "n/a", "<na>", "nat", "-", "--"}

_STORAGE_CHECK_MAX_CELLS = 50000
_NUM_TEXT_RE = re.compile(r"^[+-]?[\d,，]+(?:\.\d+)?$")


def _verify_xlsx_storage(path: str, header_rows=None):
    try:
        import openpyxl
    except Exception:
        return None
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    except Exception:
        return None

    numeric_cells = 0
    bad_cells = 0
    bad_cols = []
    scanned = 0
    truncated = False
    try:
        for ws in wb.worksheets:
            hr = 0
            if header_rows:
                try:
                    hr = int(header_rows.get(ws.title, 0) or 0)
                except Exception:
                    hr = 0
            header = None
            cols = {}
            for ridx, row in enumerate(ws.iter_rows()):
                if ridx < hr:
                    continue
                if header is None:
                    header = [c.value for c in row]
                    continue
                for j, c in enumerate(row):
                    if scanned >= _STORAGE_CHECK_MAX_CELLS:
                        truncated = True
                        break
                    scanned += 1
                    v = c.value
                    if v is None:
                        continue
                    dt = c.data_type
                    if dt == "n":
                        numeric_cells += 1
                    elif dt in ("s", "inlineStr", "str"):
                        s = str(v).strip()
                        if s:
                            cols.setdefault(j, []).append(s)
                if truncated:
                    break
            for j, vals in cols.items():
                if any(re.match(r"^0\d", s) for s in vals):
                    continue
                cleaned = [s.replace(",", "").replace("，", "") for s in vals]
                if any(re.match(r"^[+-]?\d{16,}$", s) for s in cleaned):
                    continue
                bad = [s for s in cleaned if _NUM_TEXT_RE.match(s)]
                if len(bad) >= max(1, int(len(vals) * _NUMERIC_RATIO)):
                    bad_cells += len(bad)
                    name = str(header[j]) if header and j < len(header) else f"列{j + 1}"
                    bad_cols.append(f"{ws.title}/{name}")
    except Exception:
        return None
    finally:
        try:
            wb.close()
        except Exception:
            pass

    scope = "（抽样）" if truncated else ""
    if bad_cells:
        note = (
            f"存储层复检{scope}：发现 {bad_cells} 个应为数字却以文本存储的单元格，"
            f"涉及列：{'、'.join(bad_cols[:8])}。"
            f"该结论来自写入后直读单元格类型，优先于任何内存 dtype 推断，"
            f"请如实告知用户该文件存在文本型数字。"
        )
    else:
        note = (
            f"存储层复检{scope}：{numeric_cells} 个数值单元格均以数字格式存储，"
            f"未发现文本型数字。"
        )
    return {
        "checked": True,
        "numeric_cells": numeric_cells,
        "text_numeric_cells": bad_cells,
        "columns": bad_cols,
        "truncated": truncated,
        "note": note,
    }


def inspect_xlsx_storage(path: str, header_rows=None):
    return _verify_xlsx_storage(path, header_rows=header_rows)

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


def sanitize_sheet_name(name: str, index: int = 1) -> str:
    name = str(name or "").strip()
    name = re.sub(r'[\\/:*?\[\]]', "", name)
    name = name[:31].strip(" '_")
    if not name:
        name = f"Sheet{index}"
    return name


# 兼容旧调用名
_sanitize_sheet_name = sanitize_sheet_name


def _unique_columns(names) -> List[str]:
    seen = {}
    out = []
    for raw in names:
        name = str(raw).strip() or "列"
        if name in seen:
            seen[name] += 1
            out.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 0
            out.append(name)
    return out


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

        req = _normalize_format(requested_format)
        normalized_data, resolved_kind = _normalize_export_data(data, kind, req)
        ext = _resolve_extension(resolved_kind, req)

        note = None
        storage_check = None
        if req == "csv" and ext == ".xlsx":
            note = (
                "结果包含多张表（如汇总+明细），CSV 只能存放单张表，"
                "已改用 Excel 多工作表保存。"
            )

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = sanitize_filename(filename_base) or "导出结果"
        path = _unique_path(desktop, f"{base_name}_{timestamp}{ext}")

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
                traceback.print_exc()
                df = _to_single_dataframe(normalized_data)
                if df is not None:
                    path = _unique_path(desktop, f"{base_name}_{timestamp}.csv")
                    _write_csv(path, df)
                    ext = ".csv"
                    extra = "Excel 写入失败（可能缺少 openpyxl），已回退为 CSV 单表保存。"
                else:
                    path = _unique_path(desktop, f"{base_name}_{timestamp}.txt")
                    _write_text(path, normalized_data)
                    ext = ".txt"
                    extra = "Excel 写入失败且无法表格化，已回退为文本保存。"
                note = f"{note} {extra}" if note else extra
            else:
                storage_check = _verify_xlsx_storage(path)
                if storage_check:
                    note = f"{note} {storage_check['note']}" if note else storage_check["note"]
                        
        bubble_kind = "table" if ext in EXPORT_TABLE_EXTENSIONS else "doc"

        if not os.path.exists(path):
            return _fail("write_failed")

        if req in ("excel", "csv") and ext == ".txt":
            msg = ("请求的是表格导出（excel/csv），但结果无法解析为表格结构"
                   "（缺少表头或列数不一致），已按文本原样保存，未做猜测性拆列。")
            note = f"{note} {msg}" if note else msg
            return {
                "success": False,
                "path": path,
                "format": ext,
                "bubble_kind": "doc",
                "error_code": "not_tabular",
                "note": note,
                "storage_check": storage_check,
            }

        return {
            "success": True,
            "path": path,
            "format": ext,
            "bubble_kind": bubble_kind,
            "note": note,
            "storage_check": storage_check,
        }
    
    except Exception as e:
        print(f"[FileExporter] 导出失败: {e}")
        traceback.print_exc()
        return _fail("write_failed")


def _fail(error_code: str) -> Dict[str, Any]:
    return {
        "success": False,
        "path": None,
        "format": None,
        "bubble_kind": "doc",
        "error_code": error_code,
        "note": None,
    }


def _normalize_format(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "auto"):
        return None
    if s in ("xlsx", "excel", "table"):
        return "excel"
    if s in ("text", "txt"):
        return "txt"
    if s in ("csv", "json"):
        return s
    return None


# ===================== 基础判定 =====================
def _is_scalar(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, bool, int, float)):
        return True
    if np is not None and isinstance(value, np.generic):
        return True
    if pd is not None and isinstance(value, (pd.Timestamp, pd.Timedelta, pd.Period)):
        return True
    return False


def _is_na(value) -> bool:
    if pd is None:
        return value is None
    try:
        r = pd.isna(value)
        if hasattr(r, "__len__"):
            return False
        return bool(r)
    except Exception:
        return False


def _scalar_cell(value):
    if _is_scalar(value):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    except Exception:
        return str(value)


def _to_number(s: str):
    try:
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        return float(s)
    except Exception:
        return s


# ===================== DataFrame 收尾 =====================
def _finalize_df(df):
    if pd is None or df is None:
        return None
    try:
        if isinstance(df, pd.Series):
            s = df.copy()
            if s.name is None:
                s = s.rename("值")
            out = s.reset_index()
        elif isinstance(df, pd.DataFrame):
            out = df.copy()
            if (not isinstance(out.index, pd.RangeIndex)) or (out.index.name is not None):
                out = out.reset_index()
        else:
            return None
        out.columns = _unique_columns(out.columns)
        out = _coerce_numeric_columns(out)
        return out
    except Exception:
        return None


# ===================== 统一归一化 =====================
def _to_blocks(data: Any) -> List[Tuple[str, Any]]:
    if pd is None:
        return []

    if isinstance(data, (pd.DataFrame, pd.Series)):
        df = _finalize_df(data)
        return [("结果", df)] if df is not None else []

    if isinstance(data, dict):
        if not data:
            return []
        if all(isinstance(v, (pd.DataFrame, pd.Series)) for v in data.values()):
            blocks = []
            for k, v in data.items():
                df = _finalize_df(v)
                if df is not None:
                    blocks.append((str(k), df))
            return blocks

        scalars: Dict[str, Any] = {}
        tables: List[Tuple[str, Any]] = []
        for k, v in data.items():
            name = str(k)
            df = None
            try:
                if isinstance(v, (pd.DataFrame, pd.Series)):
                    df = _finalize_df(v)
                elif isinstance(v, dict):
                    df = _finalize_df(pd.json_normalize([v]))
                elif isinstance(v, (list, tuple)):
                    seq = list(v)
                    if seq and any(isinstance(x, dict) for x in seq):
                        df = _finalize_df(pd.json_normalize(seq))
                    elif seq and all(isinstance(x, (list, tuple)) for x in seq) \
                            and len({len(x) for x in seq}) == 1:
                        df = _finalize_df(pd.DataFrame(seq))
                    else:
                        df = _finalize_df(pd.DataFrame({name: seq}))
            except Exception:
                df = None
            if df is not None:
                tables.append((name, df))
            else:
                scalars[name] = _scalar_cell(v)

        blocks = []
        if scalars:
            blocks.append(("汇总", pd.DataFrame([scalars])))
        blocks.extend(tables)
        return blocks

    if isinstance(data, (list, tuple)):
        seq = list(data)
        if not seq:
            return []
        if any(isinstance(x, (pd.DataFrame, pd.Series)) for x in seq):
            blocks = []
            for i, x in enumerate(seq, start=1):
                df = _finalize_df(x)
                if df is not None:
                    blocks.append((f"结果{i}", df))
            return blocks
        try:
            if any(isinstance(x, dict) for x in seq):
                df = _finalize_df(pd.json_normalize(seq))
            elif all(isinstance(x, (list, tuple)) for x in seq) \
                    and len({len(x) for x in seq}) == 1:
                df = _finalize_df(pd.DataFrame(seq))
            else:
                df = _finalize_df(pd.DataFrame({"值": seq}))
        except Exception:
            df = None
        return [("结果", df)] if df is not None else []

    if isinstance(data, str):
        s = data.strip()
        df = _try_text_to_dataframe(s)
        if df is not None:
            df = _finalize_df(df)
            if df is not None:
                return [("结果", df)]
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
            return [("结果", pd.DataFrame({"结果": [_to_number(s)]}))]
        return []

    if _is_scalar(data):
        try:
            return [("结果", pd.DataFrame({"结果": [data]}))]
        except Exception:
            return []
    return []


def _dedupe_block_names(blocks):
    seen = {}
    out = []
    for name, df in blocks:
        n = str(name or "结果")
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        out.append((n, df))
    return out


def _blocks_payload(blocks):
    blocks = _dedupe_block_names(blocks)
    if len(blocks) == 1:
        return blocks[0][1], "table"
    return dict(blocks), "table_multi"


def _normalize_export_data(
    data: Any,
    kind: str,
    requested_format: Optional[str]
):
    if data is None:
        return "", "text"

    want_table = requested_format in ("excel", "csv")
    want_json = requested_format == "json"

    # ---- 字符串 ----
    if isinstance(data, str):
        text = data.strip()
        if want_json:
            obj = _try_json_text(text)
            if obj is not None:
                return obj, "json"
            obj = _try_json_text(text)
            if obj is not None:
                blocks = _to_blocks(obj)
                if blocks:
                    return _blocks_payload(blocks)
            df = _try_text_to_dataframe(text)
            if df is not None:
                df = _finalize_df(df)
                if df is not None:
                    return df, "table"
        return data, "text"

    # ---- 原生表格 ----
    if pd is not None and isinstance(data, (pd.DataFrame, pd.Series)):
        blocks = _to_blocks(data)
        if blocks:
            return _blocks_payload(blocks)

    # ---- dict / list ----
    if isinstance(data, (dict, list, tuple)):
        if want_table or kind in ("table", "auto_struct"):
            blocks = _to_blocks(data)
            if blocks:
                return _blocks_payload(blocks)
        return data, "json"

    # ---- 标量 ----
    if want_table:
        blocks = _to_blocks(data)
        if blocks:
            return _blocks_payload(blocks)
    if want_json:
        return data, "json"
    return str(data), "text"


def _resolve_extension(kind: str, requested_format: Optional[str]) -> str:
    if requested_format == "csv":
        if kind == "table_multi":
            return ".xlsx"
        return ".csv"
    if requested_format == "txt":
        return ".txt"
    if requested_format == "json":
        return ".json"
    if requested_format == "excel":
        return ".xlsx"
    if kind in ("table", "table_multi"):
        return ".xlsx"
    if kind == "json":
        return ".json"
    return ".txt"


# ===================== JSON 序列化 =====================
def _df_records(df):
    out = df.copy()
    try:
        out = out.astype(object).where(pd.notna(out), None)
    except Exception:
        pass
    return out.to_dict(orient="records")


def _series_dict(s):
    return {k: (None if _is_na(v) else v) for k, v in s.to_dict().items()}


def _json_default(o):
    if pd is not None:
        if isinstance(o, pd.DataFrame):
            return _df_records(o)
        if isinstance(o, pd.Series):
            return _series_dict(o)
        if isinstance(o, pd.Index):
            return list(o)
        if isinstance(o, (pd.Timestamp, pd.Period, pd.Timedelta)):
            return str(o)
    if np is not None:
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    if isinstance(o, (set, frozenset)):
        return list(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", errors="replace")
    return str(o)


# ===================== 写入器 =====================
def _write_text(path: str, data: Any) -> None:
    text = _coerce_text(data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_json_default)


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
                safe_name = sanitize_sheet_name(sheet_name, idx)
                original_name = safe_name
                suffix = 1
                while safe_name.lower() in used_sheet_names:
                    safe_name = f"{original_name[:28]}_{suffix}"
                    suffix += 1
                used_sheet_names.add(safe_name.lower())
                final_df = _finalize_df(df)
                if final_df is None:
                    continue
                final_df.to_excel(writer, index=False, sheet_name=safe_name)
        return

    # 单 sheet
    df = _to_single_dataframe(data)
    if df is None:
        raise ValueError("无法转换为 Excel 表格数据")
    df.to_excel(path, index=False, engine="openpyxl")


# ===================== 转换辅助 =====================
def _coerce_text(data: Any) -> str:
    if pd is not None:
        if isinstance(data, (pd.DataFrame, pd.Series)):
            df = _finalize_df(data)
            if df is not None:
                return df.to_csv(index=False)
        if (
            isinstance(data, dict)
            and data
            and all(isinstance(v, pd.DataFrame) for v in data.values())
        ):
            parts = []
            for name, df in data.items():
                final_df = _finalize_df(df)
                if final_df is not None:
                    parts.append(f"【{name}】\n{final_df.to_csv(index=False)}")
            if parts:
                return "\n".join(parts)
    if isinstance(data, (dict, list, tuple)):
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)
        except Exception:
            return str(data)
    return str(data)


def _clean_export_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text

    original = text.strip()
    if "|" in text and "\n" in text:
        return original

    bad_sentence_patterns = [
        rf"^已(?:经|为|将|把)?(?:您|你)?.{{0,20}}?{_BOILERPLATE_VERBS}",
        r"^文件已保存",
        rf"^保存(?:到|至|为|成)(?:桌面|文件|本地)",
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

    line_bad_patterns = [
        rf"^已(?:经|为|将|把)?(?:您|你)?.{{0,20}}?{_BOILERPLATE_VERBS}",
        r"^文件已保存",
        rf"^{_BOILERPLATE_VERBS}(?:到|至|为|成)(?:桌面|文件|本地)",
    ]
    cleaned_lines = []
    for line in cleaned.splitlines():
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
        r"^(好的|收到|明白|了解)[，,。.!！~～\s]+",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    return cleaned if cleaned else original


def _to_single_dataframe(data: Any):
    blocks = _to_blocks(data)
    if not blocks:
        return None
    blocks = _dedupe_block_names(blocks)
    if len(blocks) == 1:
        return blocks[0][1]
    frames = []
    for name, df in blocks:
        tmp = df.copy()
        tmp.insert(0, "_表", str(name))
        frames.append(tmp)
    try:
        return pd.concat(frames, ignore_index=True)
    except Exception:
        return blocks[0][1]


def _try_struct_to_dataframe(obj: Any):
    blocks = _to_blocks(obj)
    if not blocks:
        return None
    payload, _kind = _blocks_payload(blocks)
    return payload


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


def _is_text_dtype(dt) -> bool:
    if dt == object:
        return True
    try:
        if pd.api.types.is_string_dtype(dt):
            return True
    except Exception:
        pass
    name = str(dt).lower()
    if name in ("str", "string", "object") or name.startswith("string["):
        return True
    return getattr(dt, "kind", "") in ("O", "U", "S")


def _coerce_numeric_columns(df):
    if pd is None or df is None or df.empty:
        return df
    for col in df.columns:
        s = df[col]
        if not isinstance(s, pd.Series) or not _is_text_dtype(s.dtype):
            continue

        raw = s.fillna("").astype(str).str.strip()
        null_mask = raw.str.lower().isin(_NULL_TOKENS)
        raw = raw.where(~null_mask, "")
        if raw.eq("").all():
            continue

        if raw.str.match(r"^0\d").any():
            continue
        cleaned = raw.str.replace(",", "", regex=False).str.replace("，", "", regex=False)
        if cleaned.str.match(r"^[+-]?\d{16,}$").any():
            continue

        conv = pd.to_numeric(cleaned, errors="coerce")
        non_empty = int(raw.ne("").sum())
        if not non_empty:
            continue
        if int(conv.notna().sum()) < max(1, int(non_empty * _NUMERIC_RATIO)):
            continue

        if conv.isna().any():
            df[col] = conv.astype(object).where(conv.notna(), s)
        else:
            df[col] = conv
    return df

def _is_plausible_header(cells) -> bool:
    texty = 0
    for c in cells:
        s = str(c).strip()
        if not s:
            continue
        t = s.replace(",", "").replace("，", "").rstrip("%")
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", t):
            texty += 1
    return texty > 0


def _extract_table_lines(lines):
    import re
    parsed = []
    for line in lines:
        cells = line.split()
        parsed.append(len(cells))
    
    if not parsed:
        return []
    
    best_start, best_len, best_cols = 0, 0, 0
    cur_start, cur_len = 0, 1
    for i in range(1, len(parsed)):
        if parsed[i] == parsed[i-1] and parsed[i] >= 2:
            cur_len += 1
        else:
            if cur_len > best_len and parsed[cur_start] >= 2:
                best_start, best_len, best_cols = cur_start, cur_len, parsed[cur_start]
            cur_start, cur_len = i, 1
    if cur_len > best_len and parsed[cur_start] >= 2:
        best_start, best_len = cur_start, cur_len
    
    if best_len < 2:
        return []
    return lines[best_start : best_start + best_len]


    if not text or pd is None:
        return None
    stripped = text.strip()
    if not stripped or "\n" not in stripped:
        return None

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    fixed_lines = []
    for line in lines:
        if (fixed_lines and line.startswith("|")
                and fixed_lines[-1].startswith("|")
                and not fixed_lines[-1].endswith("|")):
            fixed_lines[-1] += " " + line
        else:
            fixed_lines.append(line)

    if any("|" in ln for ln in fixed_lines):
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
            if max_cols >= 2:
                rows = [r + [""] * (max_cols - len(r)) for r in rows]
                df = pd.DataFrame(rows[1:], columns=_unique_columns(rows[0]))
                return _coerce_numeric_columns(df)

    has_thousands = bool(re.search(r"\d,\d{3}(?!\d)", stripped))
    delim_sequences = [";|\t", ",;|\t"] if has_thousands else [",;|\t"]

    for delims in delim_sequences:
        for delim in delims:
            try:
                rows = [
                    r for r in csv.reader(io.StringIO(stripped), delimiter=delim)
                    if r and any(str(c).strip() for c in r)
                ]
            except Exception:
                continue
            if len(rows) < 2:
                continue
            counts = {len(r) for r in rows}
            if len(counts) != 1 or next(iter(counts)) < 2:
                continue
            if not _is_plausible_header(rows[0]):
                continue
            df = pd.DataFrame(rows[1:], columns=_unique_columns(rows[0]))
            return _coerce_numeric_columns(df)

    if pd is not None:
        sample = stripped[:20000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
            rows = [
                r for r in csv.reader(io.StringIO(stripped), dialect=dialect)
                if r and any(str(c).strip() for c in r)
            ]
            if len(rows) >= 2:
                counts = {len(r) for r in rows}
                if len(counts) == 1 and next(iter(counts)) >= 2 \
                        and _is_plausible_header(rows[0]):
                    df = pd.DataFrame(rows[1:], columns=_unique_columns(rows[0]))
                    return _coerce_numeric_columns(df)
        except Exception:
            pass

        try:
            import io as _io
            lines = stripped.splitlines()
            table_lines = _extract_table_lines(lines) 
            if table_lines and len(table_lines) >= 2:
                table_text = "\n".join(table_lines)
                df = pd.read_csv(
                    _io.StringIO(table_text),
                    sep=r'\s+',
                    engine='python',
                    header=0,
                )
                if len(df.columns) >= 2 and len(df) >= 1:
                    df.columns = _unique_columns(df.columns)
                    return _coerce_numeric_columns(df)
        except Exception:
            pass

        return None
