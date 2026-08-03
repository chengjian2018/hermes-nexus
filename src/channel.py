"""
通信渠道模块。

提供终端交互渠道，支持：
- 真实终端模式：通过 stdin/stdout 与用户交互
- Mock 模式：使用预设脚本模拟用户输入，用于自动化测试
- 自动关闭：收集完信息或对话终止后自动关闭
"""

from __future__ import annotations

import sys
from typing import Optional


class TerminalChannel:
    """终端通信渠道。

    在终端中与用户进行文本交互，支持 mock 模式用于测试。
    当用户主动退出或 mock 响应耗尽时，自动关闭。

    Usage:
        # 真实终端模式
        ch = TerminalChannel()
        ch.open()
        while ch.is_active():
            ch.send("你好，请问有什么可以帮您？")
            user_msg = ch.receive()
            if user_msg:
                print(f"收到: {user_msg}")
        ch.close()

        # Mock 模式（自动关闭）
        ch = TerminalChannel(mock_mode=True, mock_responses=["好", "可以", "/done"])
        ch.open()
        ch.send("确认吗？")
        reply = ch.receive()  # → "好"
        ch.close()
    """

    EXIT_COMMANDS = {"/exit", "/done", "/quit", "/bye", "exit", "quit", "done"}

    def __init__(
        self,
        mock_mode: bool = False,
        mock_responses: list[str] | None = None,
        auto_close_on_exhaust: bool = True,
    ):
        """初始化终端渠道。

        Args:
            mock_mode: 是否启用 mock 模式（不读取真实终端输入）。
            mock_responses: mock 模式下的预设用户响应列表。
            auto_close_on_exhaust: mock 响应用完后是否自动发送关闭信号。
        """
        self._active = False
        self._close_signal = False
        self._mock_mode = mock_mode
        self._mock_responses = mock_responses or []
        self._mock_index = 0
        self._auto_close_on_exhaust = auto_close_on_exhaust

    # ---- 生命周期 ----

    def open(self) -> None:
        """打开渠道，开始交互会话。"""
        self._active = True
        self._close_signal = False

        if not self._mock_mode:
            print()
            print("=" * 60)
            print("📞  团建订座助手 Hermes 已上线")
            print("   （你扮演餐厅工作人员，接听客户订座电话）")
            print("   输入 /exit 或 /done 随时结束对话")
            print("=" * 60)
            print()

    def close(self) -> None:
        """关闭渠道，结束交互会话。"""
        if not self._active:
            return

        self._active = False

        if not self._mock_mode:
            print()
            print("=" * 60)
            print("👋 对话已结束，感谢使用 Hermes！")
            print("=" * 60)
            print()

    def is_active(self) -> bool:
        """检查渠道是否仍然活跃。

        Returns:
            True 如果渠道仍在运行且未被用户关闭。
        """
        return self._active and not self._close_signal

    def should_close(self) -> bool:
        """检查是否收到了关闭信号。

        Returns:
            True 如果用户发出了退出命令或 mock 响应已耗尽。
        """
        return self._close_signal

    def signal_close(self) -> None:
        """从外部发送关闭信号。"""
        self._close_signal = True

    # ---- 消息收发 ----

    def send(self, message: str) -> None:
        """向用户发送消息。

        Args:
            message: 要发送的文本内容。
        """
        if not self._active:
            return

        if self._mock_mode:
            # mock 模式下可选输出到 stderr 以便调试
            print(f"[MOCK] 📞 客户: {message}", file=sys.stderr)
        else:
            print()
            print(f"📞 客户: {message}")
            print()

    def receive(self) -> Optional[str]:
        """从用户接收一条消息。

        Returns:
            用户输入的文本；如果用户退出或 mock 耗尽则返回 None。
        """
        if not self.is_active():
            return None

        if self._mock_mode:
            return self._mock_receive()

        return self._terminal_receive()

    def _terminal_receive(self) -> Optional[str]:
        """从真实终端读取用户输入。"""
        try:
            user_input = input("🍽️  餐厅: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            self._close_signal = True
            return None

        if not user_input:
            return None

        if user_input.lower() in self.EXIT_COMMANDS:
            self._close_signal = True
            return None

        return user_input

    def _mock_receive(self) -> Optional[str]:
        """从 mock 响应列表中获取下一条响应。"""
        if self._mock_index >= len(self._mock_responses):
            if self._auto_close_on_exhaust:
                self._close_signal = True
            return None

        response = self._mock_responses[self._mock_index]
        self._mock_index += 1

        # 检查 mock 响应是否为退出命令
        if response.lower() in self.EXIT_COMMANDS:
            self._close_signal = True
            return None

        return response

    # ---- Mock 辅助方法 ----

    def remaining_mocks(self) -> int:
        """返回 mock 响应列表中剩余的条目数。"""
        return max(0, len(self._mock_responses) - self._mock_index)
