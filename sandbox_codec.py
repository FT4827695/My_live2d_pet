# -*- coding: utf-8 -*-
import math

try:
    import pandas as pd
except Exception:
    pd = None
try:
    import numpy as np
except Exception:
    np = None

PD_MARKER = "__sandbox_pd__"
MAX_DEPTH = 6


def json_safe(v, depth=0):
    if pd is None or np is None:
        return v if isinstance(v, (str, bool, int, float, type(None))) else str(v)
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.ndarray):
        return [json_safe(x, depth + 1) for x in v.tolist()]
    if isinstance(v, (pd.Timestamp, pd.Period, pd.Timedelta, pd.Interval)):
        return str(v)
    if depth >= MAX_DEPTH:
        return str(v)
    if isinstance(v, dict):
        return {str(k): json_safe(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [json_safe(x, depth + 1) for x in v]
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return str(v)


def _encode_index(idx):
    if isinstance(idx, pd.RangeIndex):
        return {
            "index_kind": "range",
            "range": [int(idx.start), int(idx.stop), int(idx.step)],
            "index_names": [None if idx.name is None else str(idx.name)],
        }
    if isinstance(idx, pd.MultiIndex):
        return {
            "index_kind": "multi",
            "index": json_safe([list(t) for t in idx.tolist()]),
            "index_names": [None if n is None else str(n) for n in idx.names],
        }
    return {
        "index_kind": "flat",
        "index": json_safe(idx.tolist()),
        "index_names": [None if idx.name is None else str(idx.name)],
    }


def _decode_index(enc):
    kind = enc.get("index_kind")
    names = enc.get("index_names") or [None]
    try:
        if kind == "range":
            a, b, c = (enc.get("range") or [0, 0, 1])
            return pd.RangeIndex(a, b, c, name=names[0])
        if kind == "multi":
            return pd.MultiIndex.from_tuples(
                [tuple(v) for v in (enc.get("index") or [])], names=names
            )
        vals = enc.get("index")
        if vals is None:
            return None
        return pd.Index(vals, name=names[0])
    except Exception:
        return None


def encode_table(obj):
    if pd is None:
        return None
    try:
        if isinstance(obj, pd.Series):
            enc = {
                PD_MARKER: "series",
                "name": None if obj.name is None else str(obj.name),
                "data": json_safe(obj.tolist()),
                "dtype": str(obj.dtype),
            }
            enc.update(_encode_index(obj.index))
            return enc
        if isinstance(obj, pd.DataFrame):
            enc = {
                PD_MARKER: "dataframe",
                "columns": [str(c) for c in obj.columns],
                "data": json_safe(
                    obj.astype(object).where(obj.notna(), None).values.tolist()
                ),
                "dtypes": {str(c): str(t) for c, t in obj.dtypes.items()},
            }
            enc.update(_encode_index(obj.index))
            return enc
    except Exception:
        return None
    return None

def decode_table(enc):
    if pd is None or not isinstance(enc, dict):
        return None
    marker = enc.get(PD_MARKER)
    try:
        if marker == "dataframe":
            df = pd.DataFrame(
                data=enc.get("data"),
                columns=enc.get("columns"),
                index=_decode_index(enc),
            )
            for col, dt in (enc.get("dtypes") or {}).items():
                try:
                    df[col] = df[col].astype(dt)
                except Exception:
                    pass
            return df
        if marker == "series":
            s = pd.Series(
                data=enc.get("data"),
                index=_decode_index(enc),
                name=enc.get("name"),
            )
            dt = enc.get("dtype")
            if dt:
                try:
                    s = s.astype(dt)
                except Exception:
                    pass
            return s
    except Exception:
        return None
    return None


def encode_result(obj, depth=0):
    if obj is None:
        return None
    if pd is not None and isinstance(obj, (pd.DataFrame, pd.Series)):
        enc = encode_table(obj)
        if enc is not None:
            return enc
        try:
            return obj.to_csv(index=False)   # 极端结构回退旧行为
        except Exception:
            return str(obj)
    if depth < MAX_DEPTH and isinstance(obj, dict):
        return {str(k): encode_result(v, depth + 1) for k, v in obj.items()}
    if depth < MAX_DEPTH and isinstance(obj, (list, tuple)):
        return [encode_result(v, depth + 1) for v in obj]
    return json_safe(obj)


def decode_result(obj, depth=0):
    if depth > MAX_DEPTH:
        return obj
    if isinstance(obj, dict):
        if obj.get(PD_MARKER) in ("dataframe", "series"):
            t = decode_table(obj)
            return t if t is not None else obj
        return {k: decode_result(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode_result(v, depth + 1) for v in obj]
    return obj
