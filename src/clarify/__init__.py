"""clarify —— 双轨澄清（Dual-Track Clarify）。

任务型对话中用户回答偏离主线时的判别与应答：
- 轨道一（kb）      : 业务知识库召回作答 + 轻拉回
- 轨道二（fallback）: 问题响应 + 强拉回
- 模糊（mixed）     : 部分业务知识 + 问题响应
"""

from src.clarify.rule import ClarifyRouteRule

__all__ = ["ClarifyRouteRule"]
