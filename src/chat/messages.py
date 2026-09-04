"""AGENT messages 构建 —— module.messages_builder 可插拔解析。

设计参照 Customer-Agent（兄弟项目）的 MessageBuilder 职责分离：agent 运行时
拆为 message_builder / llm_client / tool_executor / session_manager 四件套，
本模块承担其中 message_builder 一职。与 hermes-nexus 现状的对应：

- 声明在 module 层（BaseModule.messages_builder）——AGENT 模块不挂节点，
  无 node/pattern 三层（对照管线槽位的 node > module > pattern，此处单层即全量）
- 解析消费集中在 chat 层单点（同 stage_slots 的「声明在 module、解析在
  执行层」风格）；loop.run_agent 是唯一消费方，经 AgentRunner 协议替换
  后端时同样应复用本入口，保证行为一致
- MessagesBuilder 契约：(system_prompt, cxt) -> messages 列表，
  与 loop 旧 _build_messages 1:1 可替换；system_prompt 为终态
  （force_close 后缀已拼入），builder 不需要感知收尾逻辑
- 降级：配置了但不可调用 → warning + 默认构建（stage_slots 同款）；
  builder 执行异常不捕获（用户代码失败应可见，不静默吞）

不可信数据纪律（沿用 Customer-Agent MessageBuilder 的安全实践）：
自定义 builder 把召回结果/商品目录等外部文本拼入 messages 时，应保持其在
user/tool 角色并显式标记「非系统指令」，不得写入 system 角色——外部内容
不获得指令权威。默认构建只透传框架自身产出的 system_prompt 与会话历史。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List

if TYPE_CHECKING:
    from src.dialogue.base import DialogueContext

logger = logging.getLogger(__name__)

# AGENT 模块自定义 messages 构建器：终态 system_prompt + 会话上下文 → OpenAI 格式消息列表
MessagesBuilder = Callable[[str, "DialogueContext"], List[Dict[str, Any]]]


def default_build_messages(
    system_prompt: str, cxt: "DialogueContext"
) -> List[Dict[str, Any]]:
    """默认构建：system 条目 + history 中 user/assistant 行（loop 旧实现平移）。

    - ``system_prompt`` 为空时不加 system 条目（保留原边界行为）
    - history 只收 user/assistant 角色，tool/system 行过滤
    - 顺序保持 history 原序
    """
    messages: List[Dict[str, Any]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for msg in cxt.history:
        if msg.role in ("user", "assistant"):
            # 临时护栏（回放守卫落地后移除）：跳过带 tool_calls 的 assistant
            # 行，避免中间态把 content="" 的协议行发给真实 API
            if msg.tool_calls:
                continue
            messages.append({"role": msg.role, "content": msg.content})

    return messages


def build_agent_messages(
    module: Any, system_prompt: str, cxt: "DialogueContext"
) -> List[Dict[str, Any]]:
    """AGENT 模块 messages 构建入口：module.messages_builder 优先，降级默认。

    Args:
        module: 当前模块对象（读取 ``messages_builder`` 槽位）
        system_prompt: 终态 system prompt（force_close 后缀已拼入）
        cxt: 会话上下文（builder 自主决定如何使用 history/槽位/metadata）

    Returns:
        OpenAI 格式 messages 列表（直接传给 provider.chat_completion）
    """
    builder = getattr(module, "messages_builder", None)
    if builder is not None:
        if callable(builder):
            return builder(system_prompt, cxt)
        logger.warning(
            "[messages] module %s 的 messages_builder 不可调用，降级默认构建: %r",
            getattr(module, "module_code", "?"), builder,
        )
    return default_build_messages(system_prompt, cxt)
