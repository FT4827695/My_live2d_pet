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


from _sandbox_builtins import get_safe_builtins

_SAFE_BUILTINS = get_safe_builtins()

def _restore_df(payload_df):
    if payload_df is None:
        return None
    import pandas as pd
    import sandbox_codec
    df = sandbox_codec.decode_table(payload_df)
    if df is not None:
        return df
    df = pd.DataFrame(
        data=payload_df.get("data"),
        index=payload_df.get("index"),
        columns=payload_df.get("columns"),
    )
    for col, dt in (payload_df.get("dtypes") or {}).items():
        try:
            df[col] = df[col].astype(dt)
        except Exception:
            pass
    return df

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
            last = tree.body[-1].value
            skip = (isinstance(last, ast.Call)
                    and isinstance(last.func, ast.Name)
                    and last.func.id == "print")
            if not skip:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = (node.targets if isinstance(node, ast.Assign)
                                   else [node.target])
                        for t in targets:
                            if isinstance(t, ast.Name) and t.id == "result":
                                skip = True
                            if isinstance(t, (ast.Tuple, ast.List)):
                                for e in t.elts:
                                    if isinstance(e, ast.Name) and e.id == "result":
                                        skip = True
                    if skip:
                        break
            if not skip:
                tree.body[-1] = ast.Assign(
                    targets=[ast.Name(id="result", ctx=ast.Store())], value=last
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
        import sandbox_codec
        result = sandbox_codec.encode_result(result)
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
