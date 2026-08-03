"""
System Prompt 组装模块。

根据请求输入中的 sections 字段，组装完整的 system_prompt，
供 LLM 在多轮对话中使用。
"""

from __future__ import annotations

import json
from typing import Any

# ============================================================================
# System Prompt 模板
# ============================================================================

SYSTEM_PROMPT_TEMPLATE = """你是**望京恒电公司**的行政人员，正在**打电话给{restaurant_name}预定团建聚餐座位**。

---

## 场景说明

公司要在明天组织团建聚餐，你负责打电话给餐厅预定座位。系统已经帮你整理好了聚餐需求（见下方内部参考），你需要自然地跟餐厅工作人员沟通，把座位定下来。

## 你的角色定位

- 身份：望京恒电公司的行政人员，负责团建聚餐订座
- 场景：你拨通了{restaurant_name}的电话，跟餐厅工作人员沟通预定事宜
- 语言：口语化中文，像真人打电话一样自然，千万不要书面语
- 风格：礼貌、清晰、有条理，把事情说清楚
- 语速感受：每句话控制在 15-30 字，短句为主

{restaurant_info}

---

## 内部参考信息（你的备忘小抄，帮你记住团建需求）

以下是系统帮你整理的团建聚餐需求，这些都是**你的内部备忘**——
**你要在对话中自然地、逐步地跟餐厅确认，而不是一上来就把所有要求全倒给对方。**

{section_details}

---

## 对话流程（自然推进，不要生硬跳转）

1. **拨通电话**：先礼貌问候 —— "喂～你好，请问是{restaurant_name}吗？我想预定一下明天的团建聚餐"
2. **说明来意**：简单说清楚要预定团建聚餐，大概多少人
3. **逐步确认**：在聊天中自然地确认时间、人数、包间、口味、预算等关键信息，每次只确认一两项
4. **补充需求**：顺便问一下儿童座椅、低消等事项
5. **收尾**：关键信息确认完毕，跟餐厅确认预定成功，表示感谢

## 对话规则

- **这是语音通话**：说话要口语化、简短，不要长篇大论
- **不要念稿子**：不要把内部备注逐条念给对方，像正常人聊天一样自然沟通
- **每次只说一两件事**：别一次问太多问题，对方记不住
- **顺着对方的话走**：对方说什么你就接什么，别生硬切换话题
- **礼貌但不啰嗦**：像个干练的行政人员
- **你的需求很明确**：参考内部备注中的信息，把这些要求清楚地传达给餐厅

## 对话结束条件

当满足以下条件时，对话自然结束：
- 所有关键信息（时间、人数、口味、包间、预算、儿童座椅等）都已跟餐厅确认
- 餐厅确认可以安排，预定成功
- 你已表示感谢并确认预定

**对话结束时，请在你的回复末尾加上标记 `[CONVERSATION_COMPLETE]`**

## 重要提醒

- 你代表望京恒电公司，是为同事预定团建聚餐
- 信息状态为 "not_collected" 的，自然地顺便问一下就行
- 不确定对方回答的时候可以追问确认
- 不要编造信息，以内部备注为准
"""

# ============================================================================
# Section 格式化函数
# ============================================================================

STATUS_LABELS = {
    "completed": "✅ 已完成",
    "in_progress": "🔄 进行中",
    "pending": "⏳ 待处理",
    "not_collected": "❓ 未收集",
    "blocked": "🚫 已阻塞",
}


def _format_items(items: list[dict]) -> str:
    """格式化 section 中的 items 列表。"""
    if not items:
        return ""

    lines = ["**详细信息：**"]
    for item in items:
        field_label = item.get("label", item.get("field", "未知字段"))
        value = item.get("value", "未提供")
        source = item.get("source", "")
        note = item.get("note", "")

        # 格式化值的显示
        if isinstance(value, bool):
            value_str = "是" if value else "否"
        elif value is None or value == "":
            value_str = "（未提供）"
        elif value == "未提供":
            value_str = "（未提供）"
        else:
            value_str = str(value)

        # 来源标记
        source_map = {
            "user_provided": "👤用户提供",
            "inferred": "🧠系统推断",
            "default": "⚙️系统默认",
            "geocoded": "📍地理编码",
            "not_collected": "❓待收集",
        }
        source_tag = source_map.get(source, f"({source})" if source else "")

        # 备注
        note_str = f" — *{note}*" if note else ""

        lines.append(f"  - **{field_label}**：{value_str}  {source_tag}{note_str}")

    return "\n".join(lines)


def _format_section(section: dict) -> str:
    """格式化单个 section。"""
    name = section.get("name", "unknown")
    label = section.get("label", name)
    status = section.get("status", "unknown")
    description = section.get("description", "")
    dialogue_summary = section.get("dialogue_summary", "")
    items = section.get("items", [])
    completion_criteria = section.get("completion_criteria", "")
    criteria_met = section.get("criteria_met", False)

    status_label = STATUS_LABELS.get(status, f"({status})")
    criteria_note = "✅ 已满足" if criteria_met else "⚠️ 待满足"

    parts = [
        f"### {label}  {status_label}",
        f"",
    ]

    if description:
        parts.append(f"> {description}")
        parts.append("")

    if dialogue_summary:
        parts.append(f"**对话摘要**：{dialogue_summary}")
        parts.append("")

    if items:
        parts.append(_format_items(items))
        parts.append("")

    if completion_criteria:
        parts.append(f"**完成条件**：{completion_criteria}  ({criteria_note})")
        parts.append("")

    # 冲突检测信息
    conflict_info = section.get("conflict_detection")
    if conflict_info:
        conflicts = conflict_info.get("conflicts_found", [])
        notes = conflict_info.get("notes", "")
        if conflicts:
            parts.append(f"**⚠️ 冲突检测**：发现 {len(conflicts)} 个冲突")
            for c in conflicts:
                parts.append(f"  - {c}")
            parts.append("")
        if notes:
            parts.append(f"**冲突备注**：{notes}")
            parts.append("")

    return "\n".join(parts)


def _format_interaction_objects(objects: list[dict]) -> str:
    """格式化候选餐厅列表。"""
    if not objects:
        return ""

    lines = [
        "## 🍽️ 候选餐厅列表",
        "",
    ]

    for i, obj in enumerate(objects, 1):
        name = obj.get("name", "未知餐厅")
        address = obj.get("address", "")
        phone = obj.get("phone", "")
        extra = obj.get("extra_info", {})

        rating = extra.get("rating", "N/A")
        cost = extra.get("cost", "N/A")
        tags = extra.get("tag", "")
        distance = extra.get("distance_from_target", "")
        budget_fit = extra.get("budget_fit", True)
        budget_note = extra.get("budget_note", "")

        fit_label = "✅ 预算匹配" if budget_fit else f"⚠️ 预算不匹配 — {budget_note}"

        lines.append(f"### {i}. {name}")
        lines.append(f"  - **评分**：{rating} ⭐")
        lines.append(f"  - **人均**：¥{cost}")
        lines.append(f"  - **菜系**：{tags}")
        lines.append(f"  - **距离**：{distance}")
        lines.append(f"  - **地址**：{address}")
        lines.append(f"  - **电话**：{phone}")
        lines.append(f"  - {fit_label}")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# 主构建函数
# ============================================================================


def _get_restaurant_name(interaction_objects: list[dict] | None) -> str:
    """从交互对象中提取第一家餐厅名称。"""
    if interaction_objects and len(interaction_objects) > 0:
        return interaction_objects[0].get("name", "餐厅")
    return "餐厅"


def _get_restaurant_info(obj: dict | None) -> str:
    """从单个餐厅对象提取简要信息文本。"""
    if not obj:
        return ""
    name = obj.get("name", "")
    address = obj.get("address", "")
    phone = obj.get("phone", "")
    extra = obj.get("extra_info", {})

    rating = extra.get("rating", "")
    cost = extra.get("cost", "")
    tags = extra.get("tag", "")
    business_area = extra.get("business_area", "")
    opentime = extra.get("opentime_week", extra.get("opentime_today", ""))
    location = extra.get("location", "")
    distance = extra.get("distance_from_target", "")

    lines = [f"## 你正在联系的餐厅信息", ""]
    lines.append(f"- 店名：{name}")
    if rating:
        lines.append(f"- 评分：{rating} 分")
    if cost:
        lines.append(f"- 人均：约 ¥{cost}")
    if tags:
        lines.append(f"- 菜系/特色：{tags}")
    if business_area:
        lines.append(f"- 商圈：{business_area}")
    if address:
        lines.append(f"- 地址：{address}")
    if distance:
        lines.append(f"- 距客户位置：{distance}")
    if opentime:
        lines.append(f"- 营业时间：{opentime}")
    if phone:
        lines.append(f"- 电话：{phone}")

    return "\n".join(lines)


def build_system_prompt(
    sections: list[dict],
    interaction_objects: list[dict] | None = None,
    summary: str = "",
    task_id: str = "",
) -> str:
    """根据 sections 数据组装完整的 system_prompt。

    Args:
        sections: 从请求 JSON 中提取的 sections 列表，
                  每个 section 包含 name/label/status/items/dialogue_summary 等字段。
        interaction_objects: 候选餐厅/对象列表，第一个将作为角色身份。
        summary: 任务摘要。
        task_id: 任务 ID。

    Returns:
        组装好的 system_prompt 字符串。
    """
    # 提取第一家餐厅作为角色
    restaurant_name = _get_restaurant_name(interaction_objects)
    first_obj = interaction_objects[0] if interaction_objects else None
    restaurant_info = _get_restaurant_info(first_obj)

    # 组装内部备注（你的团建需求备忘）
    parts = []

    if summary:
        parts.append(f"### 📋 客户需求概要\n\n{summary}\n")
    if task_id:
        parts.append(f"（备忘编号：`{task_id}`）\n")

    if sections:
        formatted_sections = [_format_section(s) for s in sections]
        parts.append("\n---\n".join(formatted_sections))

    section_details = "\n".join(parts) if parts else "（暂无额外备注信息）"

    return SYSTEM_PROMPT_TEMPLATE.format(
        restaurant_name=restaurant_name,
        restaurant_info=restaurant_info,
        section_details=section_details,
    )


def build_system_prompt_from_json(json_path: str) -> str:
    """从 JSON 文件读取并构建 system_prompt。

    Args:
        json_path: JSON 文件路径。

    Returns:
        组装好的 system_prompt 字符串。
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return build_system_prompt(
        sections=data.get("sections", []),
        interaction_objects=data.get("interaction_objects", []),
        summary=data.get("summary", ""),
        task_id=data.get("task_id", ""),
    )


# ============================================================================
# CLI 调试入口
# ============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = "望京恒电团建_用餐需求.json"

    prompt = build_system_prompt_from_json(path)
    print(prompt)
    print("\n" + "=" * 60)
    print(f"[INFO] System prompt 长度: {len(prompt)} 字符")
