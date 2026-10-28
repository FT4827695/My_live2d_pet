# sandbox_runner.py
import sys
import json
import io
import contextlib
import ast

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_ALLOWED_MODULES = {
    "pandas", "numpy", "math", "re", "json",
    "datetime", "collections", "itertools", "statistics",
}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in _ALLOWED_MODULES:
        raise ImportError(f"禁止导入非白名单模块: {name}")
    return __import__(name, globals, locals, fromlist, level)


_SAFE_BUILTINS = {
    "len": len, "range": range, "print": print,
    "str": str, "int": int, "float": float,
    "list": list, "dict": dict, "tuple": tuple,
    "set": set, "sum": sum, "max": max, "min": min,
    "abs": abs, "round": round, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "__import__": _safe_import,
}


def _restore_df(payload_df):
    if payload_df is None:
        return None
    import pandas as pd
    return pd.DataFrame(
        data=payload_df.get("data"),
        index=payload_df.get("index"),
        columns=payload_df.get("columns"),
    )


def main():
    payload = json.loads(sys.stdin.read())
    code = payload["code"]

    import pandas as pd
    import numpy as np
    import math, re, json as _json, datetime, collections, itertools, statistics

    df = _restore_df(payload.get("df"))
    dfs_raw = payload.get("dfs")
    dfs = [_restore_df(d) for d in dfs_raw] if dfs_raw else None

    namespace = {
        "pd": pd, "np": np, "math": math, "re": re,
        "json": _json, "datetime": datetime,
        "collections": collections, "itertools": itertools, "statistics": statistics,
        "df": df, "dfs": dfs, "result": None,
        "__builtins__": _SAFE_BUILTINS,
    }
    if dfs:
        for i, d in enumerate(dfs):
            namespace[f"df{i + 1}"] = d

    # 最后一个表达式赋值给 result
    try:
        tree = ast.parse(code)
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tree.body[-1] = ast.Assign(
                targets=[ast.Name(id="result", ctx=ast.Store())],
                value=tree.body[-1].value,
            )
            ast.fix_missing_locations(tree)
            code = ast.unparse(tree)
    except Exception:
        pass

    output = ""
    try:
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            exec(code, namespace)
        output = f.getvalue().strip()
        result = namespace.get("result")

        if result is not None:
            try:
                json.dumps(result)
            except (TypeError, ValueError):
                if hasattr(result, "to_csv"):
                    result = result.to_csv(index=False)
                elif hasattr(result, "to_string"):
                    result = result.to_string(index=True)
                else:
                    result = str(result)

        print(json.dumps({
            "success": True,
            "output": output,
            "result": result,
        }, ensure_ascii=False))
    except Exception:
        import traceback
        print(json.dumps({
            "success": False,
            "output": f"代码执行错误:\n{traceback.format_exc()}",
            "result": None,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
