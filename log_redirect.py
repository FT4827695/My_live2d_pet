# log_redirect.py

import sys
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


# =====================================================================
# ① 拦截 subprocess
# =====================================================================
def _patch_subprocess():
    if os.name != 'nt':
        return

    _OrigPopen = subprocess.Popen

    class _SilentPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SilentPopen

    for fn_name in ('run', 'call', 'check_output', 'check_call'):
        _orig = getattr(subprocess, fn_name)
        def _make_wrapper(orig_fn):
            def _wrapper(*args, **kwargs):
                kwargs['creationflags'] = kwargs.get('creationflags', 0) | CREATE_NO_WINDOW
                return orig_fn(*args, **kwargs)
            return _wrapper
        setattr(subprocess, fn_name, _make_wrapper(_orig))


# =====================================================================
# ② 日志重定向
# =====================================================================
_LOG_MAX_BYTES = 2 * 1024 * 1024   # 运行中上限：2MB


class _Writer:

    def __init__(self, path, fallback):
        self._path = path
        self._fallback = fallback
        self._bytes_written = 0
        self._file = self._open()

    def _open(self):
        try:
            return open(self._path, 'a', encoding='utf-8', errors='replace')
        except Exception:
            return None

    def _truncate_if_needed(self):
        if self._file is not None and self._bytes_written > _LOG_MAX_BYTES:
            try:
                self._file.close()
            except Exception:
                pass
            try:
                open(self._path, 'w').close()   # 清空
            except Exception:
                pass
            self._file = self._open()
            self._bytes_written = 0
            if self._file is not None:
                marker = f"[log] 日志超过 {_LOG_MAX_BYTES // 1024}KB，已自动截断。\n"
                try:
                    self._file.write(marker)
                    self._file.flush()
                    self._bytes_written = len(marker.encode('utf-8'))
                except Exception:
                    pass

    def write(self, text):
        if self._file is not None:
            try:
                self._file.write(text)
                self._file.flush()
                self._bytes_written += len(text.encode('utf-8', errors='replace'))
                self._truncate_if_needed()
            except Exception:
                pass
        if self._fallback is not None:
            try:
                self._fallback.write(text)
            except Exception:
                pass

    def flush(self):
        if self._file is not None:
            try:
                self._file.flush()
            except Exception:
                pass
        if self._fallback is not None:
            try:
                self._fallback.flush()
            except Exception:
                pass

    def fileno(self):
        if self._file is not None:
            return self._file.fileno()
        raise AttributeError("no fileno")

    def isatty(self):
        return False


def _redirect_stdio():
    if not getattr(sys, 'frozen', False):
        return

    base = os.path.dirname(sys.executable)
    log_path = os.path.join(base, "app.log")

    # ---- 每次启动：清空上次日志 ----
    try:
        open(log_path, 'w').close()
    except Exception:
        pass

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    sys.stdout = _Writer(log_path, original_stdout)
    sys.stderr = _Writer(log_path, original_stderr)


# =====================================================================
# 执行
# =====================================================================
_patch_subprocess()
_redirect_stdio()
