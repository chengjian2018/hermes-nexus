"""stages —— 管线阶段实现集合。

四槽位（pre_recall / query / post_recall / generate）的具体 stage 实现统一放本包：
- ``nlu/`` ``nlg/``：两阶段形态的意图识别与回复生成
- ``unified.py``：单次调用 NLU+NLG 合一形态（generate 单 stage）
- ``clarify/``：FSM 偏题澄清（双轨）
- ``query/``：查询改写槽位
- ``recaller/``：召回/重排槽位

管线契约（``PipelineStage``、槽位三层解析）在 ``dialogue/``；
业务 pattern 引用这里的 stage 组装模块。
"""
