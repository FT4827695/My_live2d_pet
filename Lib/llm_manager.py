# Lib/llm_manager.py
# -*- coding: utf-8 -*-
"""
LLM 生命周期管理：加载 / 卸载 / 关闭
"""

import threading


class LlmManager:
    def __init__(
        self,
        on_loaded=None,        # (engine, use_gpu: bool, use_mmproj: bool) -> None
        on_load_failed=None,   # (error_msg: str) -> None
        on_unloaded=None,      # () -> None
    ):
        self._lock = threading.RLock()
        self.engine = None
        self._is_ready = False
        self._is_loading = False
        self._closing = False

        self.on_loaded = on_loaded
        self.on_load_failed = on_load_failed
        self.on_unloaded = on_unloaded

    # ---------- 状态查询 ----------
    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    @property
    def is_closing(self) -> bool:
        return self._closing

    # ---------- 加载 ----------
    def load(self, use_gpu: bool, use_mmproj: bool = True):
        with self._lock:
            if self._is_loading:
                return
            self._is_loading = True

        def worker():
            engine = None
            success = False
            try:
                from model_engine import XllamaEngine
                engine = XllamaEngine()
                success = engine.start_server(
                    use_gpu=use_gpu, use_mmproj=use_mmproj
                )
            except Exception as e:
                print(f"[LlmManager] 模型加载异常: {e}")
                success = False
                engine = None

            with self._lock:
                if self._closing:
                    if engine is not None and getattr(engine, "server", None) is not None:
                        try:
                            engine.stop_server()
                        except Exception:
                            pass
                    self._is_loading = False
                    return

                if success:
                    self.engine = engine
                    self._is_ready = True
                else:
                    self._is_ready = False
                    engine = None
                self._is_loading = False

            if success:
                if self.on_loaded:
                    try:
                        self.on_loaded(engine, use_gpu, use_mmproj)
                    except Exception as e:
                        print(f"[LlmManager] on_loaded 回调异常: {e}")
            else:
                if self.on_load_failed:
                    try:
                        self.on_load_failed("模型加载失败")
                    except Exception as e:
                        print(f"[LlmManager] on_load_failed 回调异常: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 卸载 ----------
    def unload(self):
        with self._lock:
            engine = self.engine
            self.engine = None
            self._is_ready = False
            self._is_loading = False

        if engine is not None:
            try:
                engine.stop_server()
            except Exception as e:
                print(f"[LlmManager] 停止引擎失败: {e}")

        if self.on_unloaded:
            try:
                self.on_unloaded()
            except Exception as e:
                print(f"[LlmManager] on_unloaded 回调异常: {e}")

    # ---------- 关闭（程序退出前） ----------
    def shutdown(self):
        with self._lock:
            self._closing = True
            engine = self.engine
            self.engine = None
            self._is_ready = False
            self._is_loading = False

        if engine is not None:
            try:
                engine.abort_generation()
            except Exception:
                pass
            try:
                engine.stop_server()
            except Exception as e:
                print(f"[退出] 停止模型引擎失败: {e}")
