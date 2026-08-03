"""
测试 channel 模块。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.channel import TerminalChannel


# ============================================================================
# Tests: 基础生命周期
# ============================================================================

class TestChannelLifecycle:
    def test_open_close(self):
        """测试打开和关闭渠道。"""
        ch = TerminalChannel(mock_mode=True)
        assert not ch.is_active()

        ch.open()
        assert ch.is_active()
        assert not ch.should_close()

        ch.close()
        assert not ch.is_active()

    def test_double_open(self):
        """重复打开应该重置状态。"""
        ch = TerminalChannel(mock_mode=True)
        ch.open()
        ch.signal_close()
        assert ch.should_close()

        ch.open()  # 重新打开
        assert ch.is_active()
        assert not ch.should_close()

    def test_double_close(self):
        """重复关闭不抛异常。"""
        ch = TerminalChannel(mock_mode=True)
        ch.open()
        ch.close()
        ch.close()  # 不抛异常
        assert not ch.is_active()


# ============================================================================
# Tests: 消息收发（Mock 模式）
# ============================================================================

class TestChannelMockMode:
    def test_receive_mock_responses(self):
        """按顺序返回 mock 响应。"""
        ch = TerminalChannel(
            mock_mode=True,
            mock_responses=["第一句", "第二句", "第三句"],
        )
        ch.open()

        assert ch.receive() == "第一句"
        assert ch.is_active()
        assert ch.receive() == "第二句"
        assert ch.receive() == "第三句"

    def test_auto_close_when_exhausted(self):
        """mock 响应用完后自动关闭。"""
        ch = TerminalChannel(
            mock_mode=True,
            mock_responses=["一条消息"],
            auto_close_on_exhaust=True,
        )
        ch.open()

        assert ch.receive() == "一条消息"
        assert ch.receive() is None
        assert ch.should_close()
        assert not ch.is_active()

    def test_no_auto_close_when_disabled(self):
        """禁用自动关闭时，耗尽后不关闭。"""
        ch = TerminalChannel(
            mock_mode=True,
            mock_responses=["一条消息"],
            auto_close_on_exhaust=False,
        )
        ch.open()

        ch.receive()  # 返回 "一条消息"
        result = ch.receive()  # 耗尽，但不自动关闭
        assert result is None
        assert not ch.should_close()

    def test_empty_mock_responses(self):
        """空 mock 列表，首次 receive 就自动关闭。"""
        ch = TerminalChannel(mock_mode=True, mock_responses=[])
        ch.open()

        assert ch.receive() is None
        assert ch.should_close()

    def test_exit_command_in_mock(self):
        """mock 响应中包含退出命令。"""
        ch = TerminalChannel(
            mock_mode=True,
            mock_responses=["好的", "/done", "不应该返回的"],
        )
        ch.open()

        assert ch.receive() == "好的"  # 正常返回
        assert ch.receive() is None  # /done 触发关闭
        assert ch.should_close()
        assert ch.remaining_mocks() == 1  # "不应该返回的" 还没被消费

    def test_various_exit_commands(self):
        """测试各种退出命令。"""
        for cmd in ["/exit", "/done", "/quit", "/bye", "exit", "quit", "done"]:
            ch = TerminalChannel(mock_mode=True, mock_responses=[cmd])
            ch.open()
            ch.receive()
            assert ch.should_close(), f"命令 '{cmd}' 应该触发关闭"

    def test_remaining_mocks_count(self):
        """测试剩余 mock 计数。"""
        ch = TerminalChannel(mock_mode=True, mock_responses=["a", "b", "c"])
        ch.open()

        assert ch.remaining_mocks() == 3
        ch.receive()
        assert ch.remaining_mocks() == 2
        ch.receive()
        assert ch.remaining_mocks() == 1
        ch.receive()
        assert ch.remaining_mocks() == 0


# ============================================================================
# Tests: 信号控制
# ============================================================================

class TestChannelSignals:
    def test_signal_close_from_outside(self):
        """外部发送关闭信号。"""
        ch = TerminalChannel(mock_mode=True, mock_responses=["消息1", "消息2"])
        ch.open()
        assert ch.is_active()

        ch.signal_close()
        assert ch.should_close()
        assert not ch.is_active()

        # 后续 receive 返回 None
        assert ch.receive() is None

    def test_send_when_closed(self):
        """关闭后 send 不报错。"""
        ch = TerminalChannel(mock_mode=True)
        ch.open()
        ch.close()
        ch.send("关闭后的消息")  # 不抛异常
