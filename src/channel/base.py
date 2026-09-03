"""Channel 核心协议 —— 归一化入站消息、引擎操作束、渠道声明。

渠道（webhook 回调型）只实现 ChannelSpec 描述差异；共性流程（token 校验、
过期过滤、get-or-create、session 前缀、错误码）全部在 webhooks.py 通用
handler 里，结构上不可绕过。本模块纯协议无 IO，不 import 引擎与 main。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Tuple, Type, runtime_checkable

from pydantic import BaseModel

from src.chat.session import Session


@dataclass
class InboundMessage:
    """所有渠道归一化后的入站消息。

    timestamp 为 None 表示不做过期过滤（渠道尽力解析，解析不了不过滤）。
    session_key 不含渠道前缀 —— 前缀由通用 handler 统一拼。
    """

    channel: str
    text: str
    session_key: str
    timestamp: Optional[float] = None
    task_info: Dict[str, str] = field(default_factory=dict)


@dataclass
class EngineOps:
    """main.py 注入的引擎操作束 —— endpoint 与 channel 共用的三个核心函数。

    channel 模块只依赖本束，不 import main（可离线单测）。
    """

    get_session: Callable[[str], Optional[Session]]
    launch_session: Callable[..., Tuple[Optional[Session], str, str]]
    run_chat_turn: Callable[[Session, str], Tuple[Optional[str], Optional[Exception]]]


@runtime_checkable
class ChannelSpec(Protocol):
    """一个 webhook 渠道的完整声明 —— 只描述差异，不含行为。"""

    name: str
    payload_model: Type[BaseModel]
    default_pattern_env: str
    token_env: Optional[str]
    stale_seconds: float

    def parse(self, payload: Any) -> InboundMessage: ...

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]: ...
