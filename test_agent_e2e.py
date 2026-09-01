#!/usr/bin/env python3
"""car_sales_agent pattern 真实 LLM 端到端冒烟（inject/transfer/sticky 三场景）。

用法:
    export DASHSCOPE_API_KEY="sk-xxx"
    python test_agent_e2e.py
"""

import logging
import sys

from config.config import get_llm_config
from src.chat.chat import chat
from src.chat.session import Session
from src.dialogue.register import discover_builtin_patterns, registry
from src.tools.register import discover_builtin_tools


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    discover_builtin_tools()
    llm_config = get_llm_config()
    discover_builtin_patterns()
    pattern = registry.get("car_sales_agent")
    if pattern is None:
        print("❌ pattern 'car_sales_agent' 未注册"); sys.exit(1)

    session = Session(session_id="e2e", pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["dispatch_graph"] = pattern.dispatch_graph
    session.cxt.metadata["task_info"] = {}
    session.cxt.llm_config = llm_config
    sessions = {"e2e": session}

    # 场景 1（inject）：前台借工单工具直接答
    print("== 场景1 inject ==")
    print("用户: 帮我看下京A12345的工单进度")
    print("助手:", chat("帮我看下京A12345的工单进度", "e2e", sessions))
    # 场景 2（transfer）：深入售后流程 → 前台移交，售后同轮接话
    print("== 场景2 transfer ==")
    print("用户: 保养预约想改期，顺便保险理赔有纠纷")
    print("助手:", chat("保养预约想改期，顺便保险理赔有纠纷", "e2e", sessions))
    # 场景 3（sticky）：继续售后话题 → 仍由 after_sales 持有
    print("== 场景3 sticky ==")
    print("用户: 理赔专员什么时候联系我")
    print("助手:", chat("理赔专员什么时候联系我", "e2e", sessions))
    print("最终持有模块:", session.cxt.current_module_code)


if __name__ == "__main__":
    main()
