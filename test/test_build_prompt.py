"""
测试 build_prompt 模块。
"""

import json
import os
import sys
import pytest

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.build_prompt import (
    build_system_prompt,
    build_system_prompt_from_json,
    SYSTEM_PROMPT_TEMPLATE,
    _format_section,
    _format_items,
    _format_interaction_objects,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_sections():
    """构造测试用的 sections 数据。"""
    return [
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
                    "label": "饮食偏好/禁忌",
                    "value": "家常菜，不太能吃辣",
                    "source": "user_provided",
                },
                {
                    "field": "has_pregnant",
                    "label": "是否有孕妇",
                    "value": False,
                    "source": "user_provided",
                },
                {
                    "field": "children_age",
                    "label": "小孩年龄",
                    "value": "未提供",
                    "source": "not_collected",
                },
            ],
            "completion_criteria": "dining_time, party_size, dietary_preference 已确认",
            "criteria_met": True,
            "dialogue_summary": "用户确认明天晚6点、23人团建",
        },
        {
            "name": "constraint_resolution",
            "label": "约束探测与消解",
            "description": "探测用户的特殊约束条件",
            "status": "completed",
            "items": [
                {
                    "field": "need_private_room",
                    "label": "是否需要包间",
                    "value": "preferred_flexible",
                    "source": "user_provided",
                    "note": "最好包间，也可以分几桌坐",
                },
            ],
            "completion_criteria": "所有约束字段已确认",
            "criteria_met": True,
            "dialogue_summary": "最好包间但可接受分桌",
            "conflict_detection": {
                "conflicts_found": [],
                "notes": "小孩+包间无冲突",
            },
        },
    ]


@pytest.fixture
def sample_interaction_objects():
    """构造测试用的候选餐厅数据。"""
    return [
        {
            "object_id": "TEST001",
            "name": "测试餐厅A",
            "address": "测试路1号",
            "phone": "010-12345678",
            "extra_info": {
                "rating": 4.5,
                "cost": 150,
                "tag": "家常菜",
                "distance_from_target": "~500m",
                "budget_fit": True,
            },
        },
    ]


# ============================================================================
# Tests: _format_items
# ============================================================================

class TestFormatItems:
    def test_format_items_with_values(self, sample_sections):
        """正常 items 格式化。"""
        items = sample_sections[0]["items"]
        result = _format_items(items)

        assert "用餐时间" in result
        assert "2026-08-03" in result
        assert "23" in result
        assert "家常菜" in result
        assert "否" in result  # has_pregnant = False
        assert "（未提供）" in result  # children_age = "未提供"
        assert "👤用户提供" in result
        assert "❓待收集" in result

    def test_format_items_empty(self):
        """空 items 列表。"""
        result = _format_items([])
        assert result == ""

    def test_format_items_with_note(self):
        """带 note 字段。"""
        items = [
            {
                "field": "test",
                "label": "测试字段",
                "value": "test_value",
                "source": "user_provided",
                "note": "这是一个备注",
            }
        ]
        result = _format_items(items)
        assert "*这是一个备注*" in result


# ============================================================================
# Tests: _format_section
# ============================================================================

class TestFormatSection:
    def test_format_completed_section(self, sample_sections):
        """已完成 section 的格式化。"""
        result = _format_section(sample_sections[0])

        assert "基础信息收集" in result
        assert "✅ 已完成" in result
        assert "确认用餐时间" in result
        assert "用户确认明天晚6点" in result
        assert "✅ 已满足" in result

    def test_format_section_with_conflict(self, sample_sections):
        """带冲突检测的 section。"""
        result = _format_section(sample_sections[1])

        assert "约束探测与消解" in result
        assert "小孩+包间无冲突" in result

    def test_format_section_minimal(self):
        """最小字段的 section。"""
        section = {
            "name": "test",
            "label": "测试",
            "status": "pending",
            "items": [],
        }
        result = _format_section(section)
        assert "测试" in result
        assert "⏳ 待处理" in result


# ============================================================================
# Tests: build_system_prompt
# ============================================================================

class TestBuildSystemPrompt:
    def test_build_with_all_fields(self, sample_sections, sample_interaction_objects):
        """完整字段构建 prompt。"""
        prompt = build_system_prompt(
            sections=sample_sections,
            interaction_objects=sample_interaction_objects,
            summary="测试任务摘要",
            task_id="test_001",
        )

        # 检查关键内容
        assert "测试餐厅A" in prompt
        assert "行政人员" in prompt
        assert "测试任务摘要" in prompt
        assert "test_001" in prompt
        assert "基础信息收集" in prompt
        assert "约束探测与消解" in prompt
        assert "测试餐厅A" in prompt
        assert "CONVERSATION_COMPLETE" in prompt

    def test_build_with_empty_sections(self):
        """空 sections。"""
        prompt = build_system_prompt(sections=[], task_id="empty_test")
        assert "empty_test" in prompt

    def test_build_without_interaction_objects(self, sample_sections):
        """无候选餐厅。"""
        prompt = build_system_prompt(sections=sample_sections)
        assert "候选餐厅" not in prompt

    def test_template_placeholders_replaced(self, sample_sections):
        """确保所有模板占位符都被替换。"""
        prompt = build_system_prompt(sections=sample_sections)
        # 不应该有未替换的 format 占位符
        assert "{section_details}" not in prompt


# ============================================================================
# Tests: build_system_prompt_from_json
# ============================================================================

class TestBuildPromptFromJSON:
    def test_from_reference_json(self):
        """从参考 JSON 文件加载并构建 prompt。"""
        json_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "望京恒电团建_用餐需求.json",
        )

        if not os.path.exists(json_path):
            pytest.skip("参考 JSON 文件不存在")

        prompt = build_system_prompt_from_json(json_path)
        assert len(prompt) > 500
        assert "美锦酒家" in prompt
        assert "肆月河豚" not in prompt  # 只包含第一家餐厅，不推荐别家


# ============================================================================
# Tests: template
# ============================================================================

class TestTemplate:
    def test_template_contains_marker(self):
        """模板包含对话结束标记说明。"""
        assert "CONVERSATION_COMPLETE" in SYSTEM_PROMPT_TEMPLATE

    def test_template_contains_role(self):
        """模板包含角色定义。"""
        assert "行政人员" in SYSTEM_PROMPT_TEMPLATE
        assert "打电话给" in SYSTEM_PROMPT_TEMPLATE
