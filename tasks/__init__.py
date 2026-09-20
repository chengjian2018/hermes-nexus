"""自主任务链路：后台执行引擎 + 任务持久化 + 触发/轮询 API（Q11/Q14/Q15）。

服务端 pattern 与对端（scripted / llm mock，channel 预留）多轮交互直到
终态；调用方 fire-and-forget 触发、轮询取结果快照。
"""
