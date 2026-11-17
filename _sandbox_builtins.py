# -*- coding: utf-8 -*-

from builtins import __build_class__ as _build_class


_ALLOWED_MODULES = {
    "pandas", "numpy", "math", "re", "json",
    "datetime", "collections", "itertools", "statistics",
}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in _ALLOWED_MODULES:
        raise ImportError(f"禁止导入非白名单模块: {name}")
    return __import__(name, globals, locals, fromlist, level)


def get_safe_builtins():
    return {
        "print": print, "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter, "sorted": sorted,
        "reversed": reversed, "any": any, "all": all,
        "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
        "divmod": divmod, "pow": pow,
        "int": int, "float": float, "str": str, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "frozenset": frozenset, "bytes": bytes, "bytearray": bytearray,
        "complex": complex,
        "isinstance": isinstance, "issubclass": issubclass, "type": type,
        "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
        "callable": callable, "repr": repr, "format": format,
        "ord": ord, "chr": chr, "bin": bin, "hex": hex, "oct": oct,
        "iter": iter, "next": next, "slice": slice,
        "True": True, "False": False, "None": None,
        "Exception": Exception, "ValueError": ValueError,
        "TypeError": TypeError, "KeyError": KeyError,
        "IndexError": IndexError, "AttributeError": AttributeError,
        "ZeroDivisionError": ZeroDivisionError, "RuntimeError": RuntimeError,
        "StopIteration": StopIteration, "OverflowError": OverflowError,
        "ArithmeticError": ArithmeticError, "LookupError": LookupError,
        "UnicodeError": UnicodeError, "UnicodeDecodeError": UnicodeDecodeError,
        "UnicodeEncodeError": UnicodeEncodeError,
        "NotImplementedError": NotImplementedError,
        "AssertionError": AssertionError,
        # 支持 class 定义
        "__build_class__": _build_class,
        "object": object,
        "property": property,
        "staticmethod": staticmethod,
        "classmethod": classmethod,
        "super": super,
        "__import__": _safe_import,
    }
