"""话术模版 JSON 的字段规范与规范化（声明式模版的词汇表）。

声明式模版是 Pattern/Module/Node 的数据化表达（设计共识
docs/plans/2026-09-20 §2.3）。表达力边界：

- 可表达：prompt（模块/节点级）、槽位、节点、FSM 转移边（sub_nodes）、
  模块转移边（sub_modules / ModuleLink）、工具 ACL（use_tools）、
  enable_clarify / is_end / answer_examples / max_hops。
- 不可表达：四槽位 stage 实例与 Python 代码级定制
  （messages_builder / agent_hooks / agent_stage）——出现即由本模块
  剔除并产生 UNSUPPORTED_FIELD 告警（validator 消费）。

需要代码级定制时手写 pattern 文件走 AST 发现（如 customer_agent）。
"""
