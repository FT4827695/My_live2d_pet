# -*- coding: utf-8 -*-

from services import CodeExecutor, TextUtils


class LegacyCodeBlockDriver:

    def __init__(self, pet_instance):
        self.pet = pet_instance

    def _collect_excel_frames(self, cache_pool):
        if cache_pool is None:
            return None, None

        excel_dfs = []

        for key in cache_pool.keys():
            entry = cache_pool.get(key)

            if entry and entry.get("type") == "excel" and entry.get("data") is not None:
                excel_dfs.append(entry["data"])

        if len(excel_dfs) == 1:
            return excel_dfs[0], None

        if len(excel_dfs) > 1:
            return None, excel_dfs

        return None, None

    def run(
        self,
        messages,
        cache_pool=None,
        df=None,
        dfs=None,
        max_loops=3,
        generation_id=None,
        message_processor=None,
        stats=None,
        image_paths=None
    ):
        engine = self.pet.llm_engine
        current_messages = list(messages)
        used_fc = False

        if image_paths is None:
            image_paths = []
        else:
            image_paths = list(image_paths)

        if df is None and dfs is None:
            df, dfs = self._collect_excel_frames(cache_pool)

        last_raw_result = None
        last_output = ""

        def _aborted():
            if generation_id is None or self.pet is None:
                return False

            try:
                return not self.pet.is_generation_current(generation_id)
            except Exception:
                return False

        def _record(raw, out):
            if message_processor is not None and hasattr(message_processor, "_record_tool_result"):
                try:
                    message_processor._record_tool_result(raw, out)
                except Exception:
                    pass

        for loop_idx in range(max_loops):
            if _aborted():
                return "（已停止）", last_raw_result, last_output, used_fc

            raw = engine.generate(
                current_messages,
                image_paths=image_paths,
                return_raw_dict=True
            )

            if raw is None:
                return None, last_raw_result, last_output, used_fc

            if stats is not None:
                stats.record_raw_dict(raw, messages=current_messages)

            if isinstance(raw, dict):
                content = raw.get("content", "") or ""
            else:
                content = str(raw or "")

            code_block = TextUtils.extract_code_block(content)

            if code_block and (df is not None or dfs is not None):
                success, output, raw_result = CodeExecutor.execute_code_with_raw(
                    code_block,
                    df=df,
                    dfs=dfs
                )

                current_messages.append({
                    "role": "assistant",
                    "content": content
                })

                if success:
                    if raw_result is not None:
                        last_raw_result = raw_result

                    if output:
                        last_output = output

                    _record(raw_result, output)

                    result_text = output if output else str(raw_result)
                    result_text = str(result_text)[:1500]

                    current_messages.append({
                        "role": "user",
                        "content": (
                            f"代码执行结果：\n{result_text}\n"
                            f"请基于这个结果，用自然语言直接回答用户。\n"
                            f"【严禁再次输出任何代码块】"
                        )
                    })
                    continue

                current_messages.append({
                    "role": "user",
                    "content": (
                        f"代码执行报错:\n{str(output)[:500]}\n"
                        f"必须修正代码，确保使用内置的 df / df1 / df2 / dfs 变量，且禁止使用 import 语句。"
                    )
                })
                continue

            return content, last_raw_result, last_output, used_fc

        return "（处理轮数达到上限）", last_raw_result, last_output, used_fc
