"""
测试 chat 模块。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.chat import ChatSession, CONVERSATION_COMPLETE_MARKER


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_request_data():
    """参考 JSON 结构构造请求数据。"""
    return {
        "task_id": "task_test_001",
        "task_type": "food-finding",
        "summary": "北京望京恒电大厦附近团建聚餐，23人，家常菜不辣",
        "sections": [
            {
                "name": "basic_info",
                "label": "基础信息收集",
                "description": "确认用餐时间、人数、饮食偏好及位置",
                "status": "completed",
                "items": [
                    {
                        "field": "dining_time",
                        "label": "用餐时间",
                        "value": "2026-08-03T18:00:00+08:00",
                        "source": "user_provided",
                    },
                    {
                        "field": "party_size",
                        "label": "用餐人数",
                        "value": 23,
                        "source": "user_provided",
                    },
                    {
                        "field": "dietary_preference",
                        "label": "饮食偏好",
                        "value": "家常菜，不太能吃辣",
                        "source": "user_provided",
                    },
                ],
                "completion_criteria": "信息已确认",
                "criteria_met": True,
                "dialogue_summary": "明天晚6点，23人团建",
            },
        ],
        "interaction_objects": [],
    }


# ============================================================================
# Tests
# ============================================================================

class TestChatSession:
    def test_run_with_mock_simple(self, sample_request_data):
        """mock 模式下简单对话。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["准确", "没问题"],
            max_turns=5,
        )

        assert result["task_id"] == "task_test_001"
        assert result["status"] in ("completed", "max_turns_reached")
        assert len(result["messages"]) >= 1  # 至少有首条问候

        # 检查消息格式
        for msg in result["messages"]:
            assert "role" in msg
            assert "content" in msg
            assert msg["role"] in ("user", "assistant")

    def test_run_no_system_prompt_in_result(self, sample_request_data):
        """返回的消息中不包含 system prompt。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["好的"],
        )

        for msg in result["messages"]:
            assert msg["role"] != "system"

    def test_run_with_user_confirmation_triggers_completion(self, sample_request_data):
        """用户说'确认'后对话结束。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["确认"],
        )

        # 应该正常完成
        assert result["status"] == "completed"
        # 消息中不应该有 CONVERSATION_COMPLETE 标记
        for msg in result["messages"]:
            assert CONVERSATION_COMPLETE_MARKER not in msg["content"]

    def test_run_max_turns_reached(self, sample_request_data):
        """达到最大轮数限制。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["问题1", "问题2", "问题3", "问题4", "问题5"],
            max_turns=3,
        )

        assert result["status"] == "max_turns_reached"
        assert len(result["messages"]) <= 2 * 3 + 1  # 每轮 user+assistant + greeting

    def test_run_with_interaction_objects(self, sample_request_data):
        """包含候选餐厅的对话。"""
        data = {
            **sample_request_data,
            "interaction_objects": [
                {
                    "object_id": "TEST001",
                    "name": "测试餐厅",
                    "address": "测试地址1号",
                    "phone": "010-12345678",
                    "extra_info": {
                        "rating": 4.5,
                        "cost": 150,
                        "tag": "家常菜",
                        "distance_from_target": "~500m",
                        "budget_fit": True,
                    },
                }
            ],
        }

        session = ChatSession()
        result = session.run(
            data,
            mock_mode=True,
            mock_responses=["好的"],
        )

        assert result["status"] in ("completed", "max_turns_reached")

    def test_run_empty_sections(self):
        """空 sections 请求。"""
        session = ChatSession()
        result = session.run(
            {"task_id": "empty", "sections": []},
            mock_mode=True,
            mock_responses=["/done"],
        )

        assert result["task_id"] == "empty"
        # 至少返回了问候消息
        assert len(result["messages"]) >= 1

    def test_run_with_exit_command(self, sample_request_data):
        """用户输入退出命令。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["/exit"],
        )

        assert result["status"] == "completed"

    def test_mock_llm_greeting(self, sample_request_data):
        """mock LLM 返回问候语。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=[],  # 无用户输入，仅看问候
        )

        # 只有问候消息
        assert len(result["messages"]) >= 1
        greeting = result["messages"][0]
        assert greeting["role"] == "assistant"
        assert "预定" in greeting["content"]

    def test_use_mock_llm_property(self):
        """无 API key 时 use_mock_llm 为 True。"""
        session = ChatSession(api_key="")
        assert session.use_mock_llm is True

        session2 = ChatSession(api_key="sk-test-key")
        assert session2.use_mock_llm is False

    def test_timeout_config(self):
        """测试超时配置。"""
        session = ChatSession(timeout=30.0)
        assert session.timeout == 30.0


class TestMockLLMResponses:
    def test_mock_response_to_question(self, sample_request_data):
        """mock LLM 对疑问句的回复。"""
        session = ChatSession()
        result = session.run(
            sample_request_data,
            mock_mode=True,
            mock_responses=["有什么推荐的吗？"],
        )

        msgs = result["messages"]
        # 找到 assistant 对问题的回复
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) >= 2  # greeting + reply to question
        reply = assistant_msgs[-1]["content"]
        assert "家常菜" in reply or "包间" in reply
