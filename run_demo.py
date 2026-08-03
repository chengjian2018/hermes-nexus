#!/usr/bin/env python
"""
Hermes Nexus 体验脚本。

支持两种模式：
  - 交互模式（默认）：在终端中与助手实时对话
  - Mock 模式：使用预设脚本自动演示完整流程

用法:
  python run_demo.py                  # 交互模式，真实对话
  python run_demo.py --mock           # Mock 自动演示
  python run_demo.py --api            # 通过 HTTP API 调用（需先启动 main.py）
  python run_demo.py --api --mock     # 通过 HTTP API + Mock 模式
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.chat import ChatSession
from src.build_prompt import build_system_prompt_from_json

# ============================================================================
# 配置
# ============================================================================

JSON_PATH = os.path.join(os.path.dirname(__file__), "望京恒电团建_用餐需求.json")

# 预设的 mock 对话脚本（模拟真实用户的多轮对话）
MOCK_SCRIPT = [
    "喂你好，我想问一下明天晚上订桌的事",
    "对，23个人，团建聚餐",
    "家常菜就行，不太能吃辣，有小孩",
    "最好是包间吧，实在不行分桌也行",
    "人均一两百都行，别太贵",
    "好的，那就定你们家了，具体信息都确认了",
    "没问题，明晚见",
]

# ============================================================================
# 交互模式：直接调用 ChatSession
# ============================================================================


def run_interactive(mock: bool = False):
    """终端交互模式（直接使用 src 模块）。"""
    # 加载 JSON
    if not os.path.exists(JSON_PATH):
        print(f"❌ 找不到文件: {JSON_PATH}")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("=" * 60)
    print("  🍽️  Hermes Nexus — 智能用餐助手体验")
    print("=" * 60)
    print(f"  任务: {data.get('summary', 'N/A')}")
    print(f"  Sections: {len(data.get('sections', []))} 个")
    print(f"  候选餐厅: {len(data.get('interaction_objects', []))} 家")
    print()

    if mock:
        api_key = os.getenv("LLM_API_KEY", "")
        llm_mode = "🧠 真实 LLM (DeepSeek)" if api_key else "📋 Mock LLM"
        print(f"  📋 Mock 模式 — 自动演示对话流程 ({llm_mode})")
        print(f"  预设 {len(MOCK_SCRIPT)} 轮用户输入")
        print()
        print("=" * 60)
        session = ChatSession()
        result = session.run(
            request_data=data,
            mock_mode=True,
            mock_responses=MOCK_SCRIPT,
            max_turns=20,
        )
        _print_result(result)
    else:
        # 检查是否有 API key 配置
        api_key = os.getenv("LLM_API_KEY", "")
        llm_mode = "🧠 真实 LLM" if api_key else "📋 Mock LLM"
        print(f"  💬 交互模式 ({llm_mode}) — 输入 /exit 或 /done 结束对话")
        print("=" * 60)
        session = ChatSession()
        result = session.run(
            request_data=data,
            mock_mode=False,  # 真实终端输入
            max_turns=20,
        )
        _print_result(result)


# ============================================================================
# HTTP API 模式：调用 FastAPI 服务
# ============================================================================


def run_via_api(mock: bool = False, base_url: str = "http://localhost:8000"):
    """通过 HTTP API 调用（需要先启动 main.py）。"""
    import urllib.request
    import urllib.error

    # 健康检查
    try:
        req = urllib.request.Request(f"{base_url}/api/v1/health")
        resp = urllib.request.urlopen(req, timeout=3)
        health = json.loads(resp.read().decode("utf-8"))
        print(f"✅ 服务连接成功: {health}")
    except Exception as e:
        print(f"❌ 无法连接到 {base_url}")
        print(f"   请先启动服务: python main.py")
        print(f"   错误: {e}")
        sys.exit(1)

    # 加载 JSON
    if not os.path.exists(JSON_PATH):
        print(f"❌ 找不到文件: {JSON_PATH}")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 构造请求
    payload = {
        "task_id": data.get("task_id", ""),
        "task_type": data.get("task_type", "food-finding"),
        "summary": data.get("summary", ""),
        "sections": data.get("sections", []),
        "interaction_objects": data.get("interaction_objects", []),
        "mock_mode": mock,
        "mock_responses": MOCK_SCRIPT if mock else None,
        "max_turns": 20,
    }

    print("=" * 60)
    print("  🌐 HTTP API 模式")
    print(f"  服务地址: {base_url}")
    print(f"  Mock: {mock}")
    print("=" * 60)
    print()
    print("  ⏳ 正在调用 /api/v1/chat ...")

    print(json.dumps(payload, ensure_ascii=False, indent=4))
    try:
        req = urllib.request.Request(
            f"{base_url}/api/v1/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read().decode("utf-8"))
        _print_result(result)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print(f"❌ HTTP {e.code}: {body}")
    except Exception as e:
        print(f"❌ 请求失败: {e}")


# ============================================================================
# 结果展示
# ============================================================================


def _print_result(result: dict):
    """美化打印对话结果。"""
    print()
    print("=" * 60)
    print("  📊 对话结果")
    print("=" * 60)
    print(f"  Task ID : {result.get('task_id', 'N/A')}")
    print(f"  Status  : {result.get('status', 'N/A')}")
    print(f"  Messages: {len(result.get('messages', []))} 条")
    print()

    messages = result.get("messages", [])
    for i, msg in enumerate(messages, 1):
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            print(f"  ┌─ [{i}] 👤 用户 ─────────────────────")
            for line in content.split("\n"):
                print(f"  │ {line}")
            print(f"  └{'─' * 40}")
        elif role == "assistant":
            print(f"  ┌─ [{i}] 🤖 Hermes ────────────────────")
            for line in content.split("\n"):
                print(f"  │ {line}")
            print(f"  └{'─' * 40}")
        else:
            print(f"  [{i}] [{role}] {content[:100]}...")

        print()

    print("=" * 60)
    summary = _summarize(result)
    print(f"  {summary}")
    print("=" * 60)


def _summarize(result: dict) -> str:
    """生成结果摘要。"""
    status = result.get("status", "unknown")
    messages = result.get("messages", [])

    user_msgs = [m for m in messages if m["role"] == "user"]
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]

    status_map = {
        "completed": "✅ 对话正常完成",
        "max_turns_reached": "⚠️ 达到最大轮数上限",
        "error": "❌ 对话出错",
    }

    summary = status_map.get(status, f"状态: {status}")
    summary += f" | 用户发言 {len(user_msgs)} 轮 | 助手回复 {len(assistant_msgs)} 轮"

    # 检查是否包含结束标记
    for m in assistant_msgs:
        if "[CONVERSATION_COMPLETE]" in m.get("content", ""):
            summary += " | 检测到结束标记 ✓"
            break

    return summary


# ============================================================================
# 入口
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Hermes Nexus 体验脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_demo.py                 交互模式，在终端与助手实时对话
  python run_demo.py --mock          Mock 自动演示完整流程
  python run_demo.py --api           通过已部署的 FastAPI 服务调用（交互）
  python run_demo.py --api --mock    通过 HTTP API + Mock 自动演示
        """,
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="启用 Mock 模式，使用预设脚本自动演示（不需要手动输入）",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="通过 HTTP API 调用（需先启动 main.py 服务）",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:8000",
        help="API 服务地址（默认 http://localhost:8000）",
    )
    args = parser.parse_args()

    if args.api:
        run_via_api(mock=args.mock, base_url=args.url)
    else:
        run_interactive(mock=args.mock)
