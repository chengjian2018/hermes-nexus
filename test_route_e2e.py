#!/usr/bin/env python3
"""car_sales_route pattern 真实 LLM 端到端冒烟测试。

使用 config/local_config.yaml 中注册的 provider（dashscope/qwen）跑一次
完整的路由 → FSM 子模块多轮对话。

用法（DASHSCOPE_API_KEY 需在当前命令环境中）:
    export DASHSCOPE_API_KEY="sk-xxx"
    python test_route_e2e.py
"""

import logging
import sys

from config.config import get_llm_config
from src.chat.chat import chat
from src.chat.session import Session
from src.dialogue.register import discover_builtin_patterns, registry


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    llm_config = get_llm_config()
    print(f"LLM provider: {llm_config['code']}, model: {llm_config['model']}")

    discovered = discover_builtin_patterns()
    print(f"已发现 patterns: {discovered}")

    pattern = registry.get("car_sales_route")
    if pattern is None:
        print("❌ pattern 'car_sales_route' 未注册")
        sys.exit(1)

    session = Session(session_id="e2e", pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.llm_config = llm_config
    sessions = {"e2e": session}

    turns = [
        "你好，我想买一辆车",
        "比亚迪汉",
        "还要收别的钱吗",   # 偏题轮：预期 kb 应答 + 拉回预算问题
        "预算20万左右",
        "我在北京",
        "好的可以",
    ]
    for q in turns:
        print(f"\n用户: {q}")
        reply = chat(query=q, session_id="e2e", all_sessions=sessions)
        print(f"助手: {reply}")
        print(
            f"  [状态] module={session.cxt.current_module_code} "
            f"node={session.cxt.current_node_code} "
            f"slots={session.cxt.filled_slots}"
        )


if __name__ == "__main__":
    main()
