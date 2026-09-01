# 纯 Agent Pattern：Module Dispatch（inject/transfer 双原语）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现纯 agent pattern 的模块间跳转：inject（邻接模块知识+工具投影，源 agent 直接回答）与 transfer（same-turn 静默移交，目标 agent 接话）双原语，统一 AGENT/FSM/ROUTE 三种 module 类型的跳转语义。

**Architecture:** `ModuleLink` 邻接声明（一字段两职责：转移图边集 + 投影清单）→ Pattern 注册期建图 + fail fast → `dispatch()` 唯一转移原语 → `chat()` 层 MAX_HOPS 重入循环消费 same-turn dispatch → `run_agent()` 注投影/借工具/拦 transfer。ROUTE 的 `jump_module` 分发改为静默 dispatch（跳过 route NLG），与 agent transfer 同构。

**Tech Stack:** Python 3.11（`.venv`），pytest，无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-09-01-agent-pattern-module-dispatch-design.md`

## Global Constraints（每个任务隐含遵守）

- 分层纪律（CLAUDE.md）：本计划涉及框架内核（chat.py/loop.py/base.py）与框架扩展（module.py/pattern.py/dispatch.py），串行执行，小步 commit，每个 Task 完成后跑全量 pytest：`export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q`（key 在 `~/.zshrc`）
- 一律走注册机制，不引入新全局单例（现有 pattern/tool/llm 三个 registry 不变）
- 不改 `PipelineStage`/`BaseModule` 构造签名：`sub_modules` 归一化在 `__init__` 内部消化，参数放宽接受 `str` 或 `ModuleLink` 混合列表，向后兼容
- 离线测试用 `tests/fake_provider.py` 的 FakeProvider（脚本化响应，不访问真实 API）
- commit 用中文、按逻辑单元；每个 Task 一个 commit
- ROUTE 静默分发是既有 pattern 的**行为变更**（用户已确认）：命中带 `jump_module` 的菜单节点不再生成菜单回复，直接 dispatch，FSM 首节点消化同一句 query。`tests/test_car_sales_route.py` 与 `tests/test_unified_stage.py` 的断言在 Task 6 按新语义更新

---

### Task 1: ModuleLink 与 BaseModule 归一化（module.py）

**Files:**
- Modify: `src/dialogue/module.py`
- Test: `tests/test_module_link.py`（新建）

**Interfaces:**
- Consumes: 现有 `BaseModule.__init__(sub_modules: Optional[List[Any]], ...)`
- Produces:
  - `ModuleLink(target: str, lend_knowledge: bool = True, lend_tools: Optional[List[str]] = None)` — dataclass，`lend_tools` 归一化为 `List[str]`
  - `BaseModule.sub_modules: List[ModuleLink]`（归一化后）；`BaseModule.answer_examples: List[str]`
  - `BaseModule.to_projection_text() -> str` — 头部投影块（供 Task 4 prompt 组装）

- [ ] **Step 1: 写失败测试**

```python
"""ModuleLink 归一化与模块头部投影测试。"""

from src.dialogue.module import AgentModule, FSMModule, ModuleLink


def test_str_link_normalized_with_knowledge_default():
    mod = AgentModule(module_code="a", sub_modules=["b"])
    assert len(mod.sub_modules) == 1
    link = mod.sub_modules[0]
    assert isinstance(link, ModuleLink)
    assert link.target == "b"
    assert link.lend_knowledge is True   # 旧写法默认借知识
    assert link.lend_tools == []


def test_modulelink_direct_config():
    link = ModuleLink(target="b", lend_knowledge=False, lend_tools=["t1"])
    assert link.lend_tools == ["t1"]
    mod = AgentModule(module_code="a", sub_modules=[link, "c"])
    assert mod.sub_modules[0] is link
    assert mod.sub_modules[1].target == "c"


def test_modulelink_defaults():
    link = ModuleLink(target="b")
    assert link.lend_knowledge is True
    assert link.lend_tools == []


def test_answer_examples_field():
    mod = AgentModule(module_code="a", answer_examples=["好的，已为您改到{time}。"])
    assert mod.answer_examples == ["好的，已为您改到{time}。"]
    mod2 = AgentModule(module_code="b")
    assert mod2.answer_examples == []


def test_to_projection_text_full():
    mod = FSMModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单、保险理赔的查询与办理",
        module_todo_description="查改保养预约、跟踪维修工单进度",
        answer_examples=["已为您把保养预约改到{时间}，请按时到店。"],
    )
    text = mod.to_projection_text()
    assert "售后维保" in text
    assert "保养预约" in text
    assert "查改保养预约" in text
    assert "已为您把保养预约改到" in text


def test_to_projection_text_empty_module():
    mod = AgentModule(module_code="x")
    assert mod.to_projection_text() == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_module_link.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModuleLink'`

- [ ] **Step 3: 最小实现**

`src/dialogue/module.py` 顶部加 import 与 dataclass（放在 `ModuleType` 之后、`BaseModule` 之前）：

```python
from dataclasses import dataclass, field as dc_field


@dataclass
class ModuleLink:
    """邻接声明：A 的 sub_modules 里的一条边。

    一字段两职责：既是对 transfer 合法目标的声明（转移图边集），
    又定义 A 上下文中 B 的投影厚度（知识/工具借出配置）。
    """

    target: str
    lend_knowledge: bool = True
    lend_tools: Optional[List[str]] = None

    def __post_init__(self):
        self.lend_tools = self.lend_tools or []
```

`BaseModule.__init__` 改动（签名不动，内部归一化 `sub_modules`，新增 `answer_examples` 参数）：

```python
    def __init__(
        self,
        ...existing params...,
        answer_examples: Optional[List[str]] = None,
        **kwargs,
    ):
        ...
        self.sub_modules = _normalize_links(sub_modules)
        self.answer_examples = answer_examples or []
        ...
```

模块级归一化函数（放在 `ModuleLink` 之后）：

```python
def _normalize_links(sub_modules: Optional[List[Any]]) -> List[ModuleLink]:
    """把 str / ModuleLink 混合列表归一化为 List[ModuleLink]。

    str 写法（旧兼容）自动包装为 lend_knowledge=True、lend_tools=[]。
    """
    links: List[ModuleLink] = []
    for item in sub_modules or []:
        if isinstance(item, ModuleLink):
            links.append(item)
        elif isinstance(item, str):
            links.append(ModuleLink(target=item))
        else:
            raise ValueError(f"sub_modules 元素必须是 str 或 ModuleLink: {item!r}")
    return links
```

`BaseModule` 加投影方法（放在 `__repr__` 之后）：

```python
    def to_projection_text(self) -> str:
        """模块头部投影：供邻接 module 的 agent prompt 注入（inject 原语）。

        只含头部四字段（name/description/todo/answer_examples），不含内部
        流程 prompt —— 流程深度不投影，深入需 transfer（spec §1.2）。
        """
        parts = []
        if self.module_name:
            parts.append(f"- 定义：【{self.module_name}】{self.module_description or ''}")
        if self.module_todo_description:
            parts.append(f"- 职责：{self.module_todo_description}")
        if self.answer_examples:
            examples = "；".join(self.answer_examples)
            parts.append(f"- 回答范式：「{examples}」")
        return "\n".join(parts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_module_link.py -v`
Expected: 6 PASS

- [ ] **Step 5: 跑全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add src/dialogue/module.py tests/test_module_link.py
git commit -m "feat(module): ModuleLink 邻接声明与 sub_modules 归一化（inject/transfer 数据模型）"
```

---

### Task 2: Pattern 注册期建图与 fail fast（pattern.py）

**Files:**
- Modify: `src/dialogue/pattern.py`
- Test: `tests/test_pattern_graph.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `ModuleLink`
- Produces:
  - `Pattern.dispatch_graph: Dict[str, Set[str]]` — 全部合法转移边（含 ROUTE `jump_module` 推导）
  - `Pattern.max_hops: int`（默认 2）
  - 注册期校验：悬空边 / 越权借出 / 自环 → `ValueError`

- [ ] **Step 1: 写失败测试**

```python
"""Pattern 转移图构建与注册期 fail fast 测试。"""

import pytest

from src.dialogue.module import AgentModule, FSMModule, ModuleLink
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern


def _mk_pattern(modules, **kw):
    return Pattern(
        code="p_test", name="t", description="t",
        entry_module_code=modules[0].module_code, modules=modules, **kw,
    )


def test_dispatch_graph_from_links():
    a = AgentModule(module_code="a", sub_modules=["b", ModuleLink(target="c")])
    b = AgentModule(module_code="b")
    c = FSMModule(module_code="c")
    p = _mk_pattern([a, b, c])
    assert p.dispatch_graph == {"a": {"b", "c"}}


def test_route_jump_module_derived_into_graph():
    menu = BaseNode(node_code="menu_x", node_name="x", jump_module="b")
    root = BaseNode(node_code="root", node_name="r", sub_nodes=["menu_x"])
    route_mod = AgentModule(module_code="rt", module_nodes=[root, menu])
    route_mod.type = type(route_mod).type  # 保持默认；推导只看 jump_module 属性
    b = AgentModule(module_code="b")
    p = _mk_pattern([route_mod, b])
    assert "b" in p.dispatch_graph["rt"]


def test_dangling_link_raises():
    a = AgentModule(module_code="a", sub_modules=["ghost"])
    b = AgentModule(module_code="b")
    with pytest.raises(ValueError, match="悬空"):
        _mk_pattern([a, b])


def test_unauthorized_lend_raises():
    b = AgentModule(module_code="b", use_tools=["t1"])
    a = AgentModule(
        module_code="a",
        sub_modules=[ModuleLink(target="b", lend_tools=["t_not_in_b"])],
    )
    with pytest.raises(ValueError, match="借出"):
        _mk_pattern([a, b])


def test_self_loop_raises():
    a = AgentModule(module_code="a", sub_modules=["a"])
    with pytest.raises(ValueError, match="自环"):
        _mk_pattern([a])


def test_agent_to_fsm_link_allowed():
    """混合 pattern：AGENT → FSM 边合法（不拦）。"""
    a = AgentModule(module_code="a", sub_modules=["f"])
    f = FSMModule(module_code="f", module_nodes=[
        BaseNode(node_code="f1", node_name="n1", is_end=True)
    ])
    p = _mk_pattern([a, f])
    assert p.dispatch_graph["a"] == {"f"}


def test_max_hops_default_and_override():
    a = AgentModule(module_code="a")
    assert _mk_pattern([a]).max_hops == 2
    assert _mk_pattern([a], max_hops=1).max_hops == 1
```

注意：`test_route_jump_module_derived_into_graph` 里 route 模块用 `AgentModule` 实例挂 `jump_module` 节点（推导逻辑只看节点属性，不依赖 module 类型），这样测试不依赖 ROUTE 类型的节点装配细节。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pattern_graph.py -v`
Expected: FAIL — `dispatch_graph` 属性不存在 / 校验未触发

- [ ] **Step 3: 最小实现**

`src/dialogue/pattern.py` 顶部加 `from typing import Optional, Any, Dict, Set`，`Pattern.__init__` 在 module_map 构建循环之后追加：

```python
        # ------------------------------------------------------------------
        # 转移图构建 + 注册期 fail fast（spec §2.4）
        # ------------------------------------------------------------------
        self.max_hops = int(kwargs.pop("max_hops", 2))
        self.dispatch_graph: Dict[str, Set[str]] = {}

        if self.modules is not None:
            for module in self.modules:
                edges: Set[str] = set()
                # 1) sub_modules 邻接边
                for link in module.sub_modules:
                    if link.target not in self.module_map:
                        raise ValueError(
                            f"悬空转移边: {module.module_code} → {link.target}"
                            f"（目标不在 module_map 中）"
                        )
                    if link.target == module.module_code:
                        raise ValueError(
                            f"自环转移边: {module.module_code} → {link.target}"
                        )
                    target = self.module_map[link.target]
                    unauthorized = set(link.lend_tools) - set(target.use_tools or [])
                    if unauthorized:
                        raise ValueError(
                            f"越权借出: {module.module_code} 借出配置无效: "
                            f"{sorted(unauthorized)} 不在 {link.target}.use_tools 中"
                        )
                    edges.add(link.target)
                # 2) ROUTE 菜单节点 jump_module 推导
                for node in module.module_nodes:
                    jump_target = getattr(node, "jump_module", None)
                    if jump_target:
                        if jump_target not in self.module_map:
                            raise ValueError(
                                f"悬空转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target} 不存在"
                            )
                        edges.add(jump_target)
                if edges:
                    self.dispatch_graph[module.module_code] = edges
```

同时把现有 `for key, value in kwargs.items(): setattr(...)` 保持在其后（`max_hops` 已 pop，不会被重复 setattr）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_pattern_graph.py -v`
Expected: 7 PASS

- [ ] **Step 5: 全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add src/dialogue/pattern.py tests/test_pattern_graph.py
git commit -m "feat(pattern): 注册期转移图构建与 fail fast（悬空/越权借出/自环）"
```

---

### Task 3: dispatch() 原语 + DialogueContext 记账（dispatch.py / base.py）

**Files:**
- Create: `src/dialogue/dispatch.py`
- Modify: `src/dialogue/base.py`（仅加常量约定注释，metadata 键不改 dataclass 字段）
- Test: `tests/test_dispatch.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `Pattern.dispatch_graph`
- Produces:
  - `ModuleDispatch(target_module_code: str, reason: str = "", source: str = "")` — dataclass
  - `dispatch(ctx: DialogueContext, event: ModuleDispatch) -> bool`：校验邻接（`ctx.metadata["dispatch_graph"]`）→ 转移（写 `current_module_code`/`current_node_code=None`）→ 记账（`ctx.metadata["dispatch_log"]` 追加、`ctx.metadata["handoff_context"]`）；非法目标返回 False
  - metadata 键约定：`dispatch_graph`（chat 启动时注入）、`dispatch_log: List[Dict]`、`handoff_context: Dict`、`served_by_projection: str`
  - `MAX_BOUNCE_BACK = 1`：同轮 A→B 后 B 立即转回 A 的拒绝阈值（dispatch_log 长度判断）

- [ ] **Step 1: 写失败测试**

```python
"""dispatch() 转移原语测试：校验、转移、记账、回弹拒绝。"""

from src.dialogue.base import DialogueContext
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.dialogue.module import AgentModule
from src.dialogue.pattern import Pattern


def _mk_ctx():
    a = AgentModule(module_code="a", sub_modules=["b"])
    b = AgentModule(module_code="b", sub_modules=["a"])
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="a", modules=[a, b])
    ctx = DialogueContext(session_id="s", user_query="q")
    ctx.module_map = p.module_map
    ctx.node_map = p.node_map
    ctx.current_module_code = "a"
    ctx.metadata["dispatch_graph"] = p.dispatch_graph
    return ctx


def test_dispatch_moves_state_and_logs():
    ctx = _mk_ctx()
    ok = dispatch(ctx, ModuleDispatch(target_module_code="b", reason="售后深入", source="handoff_tool"))
    assert ok is True
    assert ctx.current_module_code == "b"
    assert ctx.current_node_code is None
    log = ctx.metadata["dispatch_log"]
    assert log == [{"from": "a", "to": "b", "source": "handoff_tool", "reason": "售后深入"}]
    assert ctx.metadata["handoff_context"] == {
        "from": "a", "reason": "售后深入",
    }


def test_dispatch_rejects_non_adjacent_target():
    ctx = _mk_ctx()
    ok = dispatch(ctx, ModuleDispatch(target_module_code="ghost"))
    assert ok is False
    assert ctx.current_module_code == "a"
    assert ctx.metadata.get("dispatch_log") is None or ctx.metadata["dispatch_log"] == []


def test_dispatch_rejects_bounce_back_same_turn():
    """A→B 后同轮 B 立即转回 A：拒绝（防移交环 spec §4.2）。"""
    ctx = _mk_ctx()
    dispatch(ctx, ModuleDispatch(target_module_code="b"))
    ok = dispatch(ctx, ModuleDispatch(target_module_code="a"))
    assert ok is False
    assert ctx.current_module_code == "b"


def test_dispatch_no_graph_denies():
    ctx = _mk_ctx()
    ctx.metadata.pop("dispatch_graph")
    assert dispatch(ctx, ModuleDispatch(target_module_code="b")) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: src.dialogue.dispatch`

- [ ] **Step 3: 实现 dispatch.py**

```python
"""Module dispatch —— 全 pattern 通用的唯一模块转移原语（spec §3）。

三种跳转源（agent transfer 工具 / ROUTE jump_module / 兼容 [jump] 标签）
都构造 ModuleDispatch 走本函数；它只做纯状态转移 + 记账，
不决定轮次边界（same-turn 重入由 chat() 层消费 dispatch 事件实现）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.dialogue.base import DialogueContext

logger = logging.getLogger(__name__)

# 同轮回弹上限：A→B 后同轮 B 转回 A 视为移交环，拒绝
_MAX_BOUNCE_BACK = 1


@dataclass
class ModuleDispatch:
    """一次模块转移事件。"""

    target_module_code: str
    reason: str = ""      # 移交上下文：供目标模块承接（注入 prompt）
    source: str = ""      # handoff_tool / route_menu / jump_tag


def dispatch(ctx: "DialogueContext", event: ModuleDispatch) -> bool:
    """执行模块转移：邻接校验 → 状态转移 → 记账。

    Returns:
        True 转移成功；False 目标非法（不邻接/回弹/无图），状态不变。
    """
    graph = ctx.metadata.get("dispatch_graph") or {}
    edges = graph.get(ctx.current_module_code or "", set())
    target = event.target_module_code

    if target not in edges:
        logger.warning(
            "[dispatch] 目标 '%s' 不在 '%s' 的邻接表中，拒绝转移",
            target, ctx.current_module_code,
        )
        return False

    # 防环：同轮 A→B→A 回弹拒绝（dispatch_log 为本轮累积，chat() 每轮开头清空）
    log = ctx.metadata.setdefault("dispatch_log", [])
    if log:
        first_from = log[0].get("from")
        if target == first_from and len(log) >= _MAX_BOUNCE_BACK:
            logger.warning("[dispatch] 同轮回弹 %s → %s，拒绝（移交环）",
                           ctx.current_module_code, target)
            return False

    log.append({
        "from": ctx.current_module_code,
        "to": target,
        "source": event.source,
        "reason": event.reason,
    })
    ctx.metadata["handoff_context"] = {
        "from": ctx.current_module_code,
        "reason": event.reason,
    }
    ctx.current_module_code = target
    ctx.current_node_code = None
    logger.info("[dispatch] %s → %s (source=%s)",
                log[-1]["from"], target, event.source)
    return True
```

`src/dialogue/base.py` 的 `DialogueContext` 类 docstring 末尾补一段 metadata 键约定（不改字段，纯文档）：

```python
        # metadata 键约定（module dispatch 机制使用，见 dispatch.py）：
        #   dispatch_graph       : Dict[str, Set[str]]  合法转移边（chat 启动时注入）
        #   dispatch_log         : List[Dict]           本轮转移链（每轮开头清空）
        #   handoff_context      : Dict                 最近一次转移的承接信息
        #   served_by_projection : str                  A 借投影答轮的来源域
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_dispatch.py -v`
Expected: 4 PASS

- [ ] **Step 5: 全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add src/dialogue/dispatch.py src/dialogue/base.py tests/test_dispatch.py
git commit -m "feat(dispatch): 模块转移原语（邻接校验/记账/回弹拒绝）"
```

---

### Task 4: run_agent 改造——投影注入 + transfer 工具 + tool 往返落盘（loop.py + prompt.py）

**Files:**
- Modify: `src/chat/loop.py`
- Modify: `src/prompt.py`（追加模板常量）
- Test: `tests/test_agent_inject_transfer.py`（新建）

**Interfaces:**
- Consumes: Task 1 `ModuleLink`/`to_projection_text()`；Task 3 `ModuleDispatch`/`dispatch()`
- Produces:
  - `TurnResult(reply: Optional[str], dispatch_event: Optional[ModuleDispatch])` — dataclass（loop.py 内定义，chat.py Task 5 消费）
  - `run_agent(session, module, llm_config) -> TurnResult`（替代 `conversation()`，后者保留为薄兼容 wrapper：调 run_agent，dispatch_event 为 None 时返回 reply，否则返回空串）
  - `build_transfer_tools(module, module_map) -> List[dict]`、`build_projection_block(module, module_map) -> str`（供测试直接断言）
  - prompt 常量：`AGENT_TEAM_RULES_PROMPT`、`AGENT_HANDOFF承接_PROMPT`（承接块模板，命名 `AGENT_TAKEOVER_PROMPT`）、`AGENT_PROJECTION_RECALL_PROMPT`（记账回看块）

- [ ] **Step 1: 写失败测试**

测试用例设计（FakeProvider 不支持 tools 参数的多轮脚本，本任务测试直接 mock `build_provider` 返回脚本化 provider 实例）：

```python
"""run_agent：投影注入 / transfer 拦截 / tool 往返落盘测试。"""

from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.base import DialogueContext, SessionMessage
from src.dialogue.module import AgentModule, ModuleLink
from src.dialogue.pattern import Pattern


def _mk_session():
    after_sales = AgentModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单办理",
        module_todo_description="查改保养预约",
        answer_examples=["已为您改到{时间}。"],
        use_tools=["query_workorder"],
        sub_modules=["reception"],
    )
    reception = AgentModule(
        module_code="reception",
        module_name="前台接待",
        module_description="接待与分诊",
        sub_modules=[
            ModuleLink(target="after_sales",
                       lend_tools=["query_workorder"]),
        ],
    )
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="reception",
                modules=[reception, after_sales])
    s = Session(session_id="s", pattern_code="p")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "reception"
    s.cxt.metadata["dispatch_graph"] = p.dispatch_graph
    s.cxt.llm_config = {"code": "x", "model": "m"}
    return s


class ScriptedProvider:
    """按脚本依次返回响应；记录收到的 messages/tools 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        return item


def test_projection_block_contains_knowledge_and_tools():
    from src.chat.loop import build_projection_block
    s = _mk_session()
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map)
    assert "售后维保" in block
    assert "保养预约" in block
    assert "query_workorder" in block   # 借出工具列在投影块


def test_transfer_tools_generated_per_link():
    from src.chat.loop import build_transfer_tools
    s = _mk_session()
    tools = build_transfer_tools(
        s.cxt.module_map["reception"], s.cxt.module_map)
    names = [t["function"]["name"] for t in tools]
    assert names == ["transfer_to_after_sales"]
    desc = tools[0]["function"]["description"]
    assert "售后维保" in desc


def test_run_agent_direct_reply_with_lent_tool():
    """inject 路径：A 借工具答完 → TurnResult(reply, None) + lent_by 记账。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "query_workorder", "arguments": "{}"}}]},
        {"content": "您的工单已查到，预计明天完工。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.llm_config)
    assert result.reply == "您的工单已查到，预计明天完工。"
    assert result.dispatch_event is None
    assert s.cxt.metadata["served_by_projection"] == "after_sales"
    # tool 往返落 history
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("lent_by") == "after_sales"


def test_run_agent_transfer_returns_dispatch_event():
    """transfer 路径：A 调 transfer 工具 → 立即返回 dispatch_event，reply 不出口。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的这就为您处理", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_after_sales",
                                     "arguments": '{"reason": "售后投诉"}'}}]},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.llm_config)
    assert result.reply is None or result.reply == ""
    assert result.dispatch_event is not None
    assert result.dispatch_event.target_module_code == "after_sales"
    assert result.dispatch_event.reason == "售后投诉"
    # 状态已由 run_agent 内部 dispatch() 转移
    assert s.cxt.current_module_code == "after_sales"


def test_takeover_block_injected_for_target():
    """承接块：transfer 后目标模块首轮 prompt 含承接上下文。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.current_module_code = "after_sales"
    s.cxt.metadata["handoff_context"] = {"from": "reception", "reason": "售后投诉"}
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "看到您要投诉，我先记录一下。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["after_sales"], s.cxt.llm_config)
    assert "reception" in provider.seen[0]["messages"][0]["content"]
    assert "售后投诉" in provider.seen[0]["messages"][0]["content"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_agent_inject_transfer.py -v`
Expected: FAIL — `cannot import name 'run_agent'`

- [ ] **Step 3: 实现**

`src/prompt.py` 追加三个常量：

```python
AGENT_TEAM_RULES_PROMPT = """## 团队协作规则
1. 「邻接能力」块覆盖的问题：一句话能答或一次工具调用能解决的，直接以自己的身份回答，不要提及能力来源。
2. 需要多轮深入流程（完整业务流程、复杂方案沟通）的，调用 transfer_to_XX 工具，reason 中带上已收集的用户信息。
3. 调用 transfer 工具的那一次，不要对用户说任何话（包括"为您转接"）——接手方会直接回复用户，用户对这个切换无感知。
"""

AGENT_TAKEOVER_PROMPT = """## 承接上下文
你从【{__from_module__}】接手了本对话。用户诉求与已有信息：{__reason__}
请直接以自己的身份接续回复（可自然承接，如"看到您想……"），不要描述转接过程。
"""

AGENT_PROJECTION_RECALL_PROMPT = """## 上一轮提示
上一轮你借用了【{__projection_source__}】的能力处理了用户请求。用户若继续该话题：
简单追问 → 继续直接答；需要深入流程 → 调用 transfer_to_{__projection_source__}。
"""
```

`src/chat/loop.py` 改造（保留 `_resolve_tools`/`_execute_tool`/`_build_messages`，重写主流程）：

```python
# 新增 imports
from dataclasses import dataclass
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.prompt import (
    AGENT_PROJECTION_RECALL_PROMPT,
    AGENT_TAKEOVER_PROMPT,
    AGENT_TEAM_RULES_PROMPT,
    fill_prompt_template,   # 若 prompt.py 未导出则从 src.dialogue.base import
)

TRANSFER_TOOL_PREFIX = "transfer_to_"
_MAX_TOOL_ROUNDS = 10  # 保留


@dataclass
class TurnResult:
    """单模块单轮执行结果：reply 与 dispatch_event 互斥（spec §3.2）。"""
    reply: Optional[str] = None
    dispatch_event: Optional[ModuleDispatch] = None


def build_projection_block(module, module_map) -> str:
    """邻接投影块：每条 lend_knowledge 边一片（spec §4 §3.2）。"""
    blocks = []
    for link in module.sub_modules:
        if not link.lend_knowledge:
            continue
        target = module_map.get(link.target)
        if target is None:
            continue
        parts = [f"## 邻接能力：{target.module_name}（{target.module_code}）"]
        parts.append(target.to_projection_text())
        if link.lend_tools:
            parts.append(f"- 可借工具：{', '.join(link.lend_tools)}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def build_transfer_tools(module, module_map) -> list:
    """由 sub_modules 逐边生成 transfer 工具（spec §4 §3.3）。"""
    tools = []
    for link in module.sub_modules:
        target = module_map.get(link.target)
        if target is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": f"{TRANSFER_TOOL_PREFIX}{link.target}",
                "description": (
                    f"移交给【{target.module_name}】。适用：该域的多轮深入流程。"
                    f"不适用：一句话或一次工具能解决的请求——那类直接自己处理。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {
                        "type": "string",
                        "description": "移交原因及已收集的用户信息摘要，供接手方无缝承接",
                    }},
                    "required": ["reason"],
                },
            },
        })
    return tools


def run_agent(session, module, llm_config) -> TurnResult:
    """执行单个 AGENT 模块一轮：inject 直接答 / transfer 立即返回（spec §3.3）。"""
    cxt = session.cxt
    provider = build_provider(llm_config)

    system_prompt = _build_system_prompt(module, cxt)  # 内部升级，见下
    own_tools = _resolve_tools(module, session.pattern)
    lent = _resolve_lent_tools(module, session.pattern)  # 新函数
    transfer_tools = build_transfer_tools(module, cxt.module_map)
    tools = own_tools + lent + transfer_tools

    messages = _build_messages(system_prompt, cxt)
    lent_by = {t["function"]["name"]: link.target
               for link in module.sub_modules
               for t in [] }  # 占位说明见下——实际由 _resolve_lent_tools 返回 (schemas, name→source) 二元组

    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.7)
    max_tokens = llm_config.get("max_tokens", 2048)

    for _ in range(_MAX_TOOL_ROUNDS):
        result = provider.chat_completion(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools or None,
            tool_choice="auto" if tools else None,
        ) if tools else provider.chat_completion(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens,
        )

        content = result.get("content", "") or ""
        tool_calls = result.get("tool_calls", []) or []

        if not tool_calls:
            return TurnResult(reply=content)

        # 本轮工具调用里是否含 transfer
        transfer_call = next(
            (tc for tc in tool_calls
             if tc.get("function", {}).get("name", "").startswith(TRANSFER_TOOL_PREFIX)),
            None,
        )
        if transfer_call is not None:
            # A 的 content 不出口但保留进 history（spec §3.3）
            if content:
                cxt.add_message("assistant", content, stage="agent",
                                metadata={"suppressed": True})
            _execute_transfer(session, transfer_call)
            return TurnResult(dispatch_event=ModuleDispatch(
                target_module_code=transfer_call["function"]["name"][len(TRANSFER_TOOL_PREFIX):],
                reason="", source="handoff_tool",
            ))  # 注：状态已由 _execute_transfer 内 dispatch() 转移；event 供 chat 层日志

        # 普通工具调用：执行、落 history、回填
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            args = _parse_args(tc)
            tool_result = _execute_tool(name, args)
            source = lent_by.get(name)
            if source:
                cxt.metadata["served_by_projection"] = source
            cxt.add_message("tool", tool_result, stage="agent",
                            metadata={"tool_name": name,
                                      "lent_by": source} if source else {"tool_name": name})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": tool_result})

    logger.warning("Agent loop 达到最大轮次，强制收尾")
    return TurnResult(reply="抱歉，处理超时，请稍后重试。")


def _execute_transfer(session, transfer_call) -> None:
    """解析 transfer 工具调用并执行 dispatch()（含 reason）。"""
    cxt = session.cxt
    name = transfer_call.get("function", {}).get("name", "")
    target = name[len(TRANSFER_TOOL_PREFIX):]
    args = _parse_args(transfer_call)
    reason = args.get("reason", "") if isinstance(args, dict) else ""
    ok = dispatch(cxt, ModuleDispatch(target_module_code=target,
                                      reason=reason, source="handoff_tool"))
    if not ok:
        logger.warning("[transfer] 目标 %s 转移失败（不邻接/回弹），本轮继续", target)


def _resolve_lent_tools(module, pattern):
    """解析借入工具 schema 与 name→来源域映射（spec §3.3 权限）。

    Returns:
        (schemas, lent_by)：schemas 为 OpenAI 格式列表；lent_by 为
        {tool_name: 来源 module_code}。
    """
    schemas, lent_by = [], {}
    for link in module.sub_modules:
        if not link.lend_tools:
            continue
        target = (pattern.module_map if pattern else {}).get(link.target)
        if target is None:
            continue
        allowed = set(target.use_tools or []) & set(link.lend_tools)
        for schema in tool_registry.get_definitions(allowed):
            name = schema["function"]["name"]
            schemas.append(schema)
            lent_by[name] = link.target
    return schemas, lent_by
```

注意实现时删掉上面 `run_agent` 里的 `lent_by = {...占位...}` 片段，改为 `lent_schemas, lent_by = _resolve_lent_tools(module, session.pattern)`，`tools = own_tools + lent_schemas + transfer_tools`。

`_build_system_prompt(module, cxt)` 升级为五块结构（保留原任务信息/槽位部分）：

```python
def _build_system_prompt(module, cxt) -> str:
    parts = []
    if module.base_prompt:
        parts.append(module.base_prompt)

    projection = build_projection_block(module, cxt.module_map)
    if projection:
        parts.append(projection)
        parts.append(AGENT_TEAM_RULES_PROMPT)

    handoff = cxt.metadata.get("handoff_context")
    if handoff:
        parts.append(fill_prompt_template(AGENT_TAKEOVER_PROMPT, {
            "__from_module__": handoff.get("from", ""),
            "__reason__": handoff.get("reason", "") or "（无补充信息）",
        }))

    served = cxt.metadata.get("served_by_projection")
    if served:
        parts.append(fill_prompt_template(AGENT_PROJECTION_RECALL_PROMPT, {
            "__projection_source__": served,
        }))

    task_info = cxt.metadata.get("task_info", {})
    if task_info:
        parts.append("\n## 任务信息")
        for key, value in task_info.items():
            parts.append(f"- {key}: {value}")

    if cxt.filled_slots:
        parts.append("\n## 已填充槽位")
        parts.append(json.dumps(cxt.filled_slots, ensure_ascii=False, indent=2))

    return "\n".join(parts)
```

（`[jump]` 标签逻辑随旧 `conversation()` 主体删除；`conversation()` 改为兼容 wrapper。）旧函数 `_process_jump_tags` 删除，`_JUMP_PATTERN` 一并清理。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_agent_inject_transfer.py -v`
Expected: 5 PASS

- [ ] **Step 5: 全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add src/chat/loop.py src/prompt.py tests/test_agent_inject_transfer.py
git commit -m "feat(loop): run_agent 双原语执行器（投影注入/transfer 拦截/tool 往返落盘）"
```

---

### Task 5: chat 层重入循环 + ROUTE 静默分发（chat.py）

**Files:**
- Modify: `src/chat/chat.py`
- Test: `tests/test_chat_reentry.py`（新建）

**Interfaces:**
- Consumes: Task 3 `dispatch()`/metadata 约定；Task 4 `TurnResult`/`run_agent()`
- Produces:
  - `chat()` 内重入循环：`for hop in range(pattern.max_hops)`，dispatch_event 非空时以目标模块重跑；超限强制收尾
  - `run_pipeline()` ROUTE 分支：命中带 `jump_module` 菜单节点 → 跳过 NLG、返回 dispatch（静默分发）
  - 每轮开头清空 `dispatch_log`

- [ ] **Step 1: 写失败测试**

```python
"""chat 层重入循环：same-turn transfer / ROUTE 静默分发 / 防环强制收尾。"""

from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.module import AgentModule, ModuleLink, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from tests.test_agent_inject_transfer import ScriptedProvider, _mk_session  # 复用


def _agent_pattern(**kw):
    """两 agent（reception → after_sales）+ 可选 max_hops。"""
    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", module_todo_description="售后流程",
        sub_modules=["reception"])
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="after_sales")])
    return Pattern(code="p2", name="t", description="t",
                   entry_module_code="reception",
                   modules=[reception, after_sales], **kw)


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["dispatch_graph"] = pattern.dispatch_graph
    session.cxt.llm_config = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from src.chat.chat import chat as chat_fn
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


def test_same_turn_transfer_b_replies():
    """A transfer → 同轮 B 接话，用户只听到 B。"""
    sessions = {}
    _launch(_agent_pattern(), sessions)
    provider = ScriptedProvider([
        # A：决定移交（content 被抑制）
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B：承接回复
        {"content": "看到您有售后需求，我先了解一下具体情况。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "看到您有售后需求，我先了解一下具体情况。"
    assert sessions["s1"].cxt.current_module_code == "after_sales"


def test_max_hops_exceeded_force_close():
    """连续 transfer 超过 max_hops=1：以当前模块强制收尾（prompt 注入勿再移交）。"""
    sessions = {}
    _launch(_agent_pattern(max_hops=1), sessions)
    provider = ScriptedProvider([
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales", "arguments": "{}"}}]},
        # 强制收尾轮：B 仍想转回，但已超限 → 应直接回复
        {"content": "好的，我来处理您的售后问题。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "好的，我来处理您的售后问题。"
```

（ROUTE 静默分发的测试在 Task 6 随既有 route 测试断言更新覆盖，这里不重复。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_chat_reentry.py -v`
Expected: FAIL — 现 chat() 无重入循环，A 的 transfer 轮无回复或行为不符

- [ ] **Step 3: 实现 chat.py 改造**

`chat()` 第 3 步（模块处理）替换为重入循环：

```python
    # ------------------------------------------------------------------
    # 3. 重入循环：same-turn dispatch 消费（spec §3.1）
    # ------------------------------------------------------------------
    cxt.metadata.pop("dispatch_log", None)   # 每轮开头清空转移链

    max_hops = getattr(pattern, "max_hops", 2)
    try:
        for hop in range(max_hops):
            current_module = pattern.module_map[cxt.current_module_code]
            if current_module.type == ModuleType.AGENT:
                result = _handle_agent_module(session, current_module)
            else:
                result = _run_pipeline(session, current_module)

            if getattr(result, "dispatch_event", None) is None:
                response = result.reply or ""
                break
            logger.info(
                "same-turn dispatch 第 %d 跳: → %s",
                hop + 1, result.dispatch_event.target_module_code,
            )
        else:
            # 超跳数：以当前模块强制收尾
            logger.warning("达到 max_hops=%d，强制收尾", max_hops)
            current_module = pattern.module_map[cxt.current_module_code]
            if current_module.type == ModuleType.AGENT:
                result = _handle_agent_module(
                    session, current_module, force_close=True)
                response = result.reply or ""
            else:
                result = _run_pipeline(session, current_module)
                response = (cxt.nlg_result or {}).get("content", "")
    except Exception as e:
        logger.exception("对话处理异常: session=%s", session_id)
        response = f"对话处理异常: {e}"
```

`_handle_agent_module` 加 `force_close` 参数（转发给 run_agent，run_agent 在 system prompt 末尾追加 `"请直接回应用户，勿再移交。"`，且不注入 transfer 工具）。

`_run_pipeline` 返回值改为 `TurnResult`：FSM 分支 `TurnResult(reply=nlg_content)`；ROUTE 分支改造（替换现有 `_handle_node_transition` 的 ROUTE 部分）：

```python
    # ROUTE：route_advance 后若当前菜单节点带 jump_module → 静默 dispatch
    if module.type == ModuleType.ROUTE:
        cur_node = cxt.node_map.get(cxt.current_node_code)
        jump_target = getattr(cur_node, "jump_module", None) if cur_node else None
        if jump_target and jump_target in cxt.module_map:
            ok = dispatch(cxt, ModuleDispatch(
                target_module_code=jump_target, source="route_menu"))
            if ok:
                return TurnResult(dispatch_event=...已转移, event 仅作日志)
        # 无 jump_module（如闲聊）或 dispatch 失败 → 正常走 NLG 回复
```

注意：`dispatch()` 已转移状态，`TurnResult.dispatch_event` 填一个日志用的 `ModuleDispatch`（`chat()` 循环只判空与读 target，不二次 dispatch）。`_handle_node_transition` 中 ROUTE 分支删除（职责移入 `_run_pipeline`），FSM 分支保留。`from src.chat.loop import TurnResult, run_agent`，`from src.dialogue.dispatch import ModuleDispatch, dispatch`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_chat_reentry.py -v`
Expected: 2 PASS

- [ ] **Step 5: 全量回归（此处预期 route/unified 测试因行为变更而 FAIL，属预期，Task 6 修）+ commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
# 预期：test_car_sales_route.py / test_unified_stage.py 部分 FAIL（ROUTE 静默分发行为变更）
git add src/chat/chat.py tests/test_chat_reentry.py
git commit -m "feat(chat): same-turn 重入循环与 ROUTE 静默分发（统一 dispatch 消费）"
```

---

### Task 6: 既有 route/unified 测试断言更新（行为变更落地）

**Files:**
- Modify: `tests/test_car_sales_route.py`
- Modify: `tests/test_unified_stage.py`

**Interfaces:**
- Consumes: Task 5 的新 ROUTE 语义
- Produces: 与静默分发一致的测试基线

- [ ] **Step 1: 更新 test_car_sales_route.py 断言**

`test_route_to_buy_fsm_full_flow` 第 1 轮改为：

```python
    # 第 1 轮：路由分类命中 menu_sales → 静默分发，FSM 首节点直接消化同一句 query
    reply = chat(sessions, "s1", "我想买车，帮忙看看有什么车型")
    # FSM 首节点 buy_ask_brand 的 NLU 抽取 brand（FakeProvider _fsm_nlu 按节点名映射）
    # 回复来自 buy_ask_budget 的 NLG（FSM 已推进到下一节点）
    assert "询问预算" in reply, f"回复应来自 FSM 首节点消化后的下一节点，实际: {reply!r}"
    assert session.cxt.current_module_code == "car_sales_buy"
    assert session.cxt.current_node_code == "buy_ask_budget"
```

后续轮次序号相应前移（原第 2 轮起每轮少一步；原流程的第 2/3/4 轮断言改为对第 2/3 轮），闲聊用例不变（menu_chitchat 无 jump_module，仍走 route NLG）。同文件其余涉及"菜单节点回复"的断言同步按新语义调整。

- [ ] **Step 2: 更新 test_unified_stage.py 断言**

`test_route_then_fsm_full_flow_single_call_per_turn` 的第 1 轮同理：`chat_once` 断言从 1 次调用变为 2 次（route 统一阶段 1 次 + FSM 首节点统一阶段 1 次）。`chat_once` 辅助函数加 `expect_calls=1` 参数：

```python
def chat_once(pattern, sessions, query, expect_calls=1):
    before = FakeProvider.call_count
    reply = chat(sessions, "s1", query)
    assert FakeProvider.call_count - before == expect_calls, (
        f"实际 {FakeProvider.call_count - before} 次"
    )
    return reply
```

首轮调用改为 `chat_once(pattern, sessions, "我想买车", expect_calls=2)`，断言 reply 来自 FSM 侧（含 "询问预算" 类标记）。`test_chitchat_stays_route_root` 不变。

- [ ] **Step 3: 跑两个文件确认通过**

Run: `.venv/bin/python -m pytest tests/test_car_sales_route.py tests/test_unified_stage.py -v`
Expected: 全 PASS

- [ ] **Step 4: 全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add tests/test_car_sales_route.py tests/test_unified_stage.py
git commit -m "test: route/unified 断言对齐 ROUTE 静默分发新语义"
```

---

### Task 7: car_sales_agent 演示 pattern（应用层）

**Files:**
- Create: `src/dialogue/car_sales_agent.py`
- Test: `tests/test_car_sales_agent.py`（新建）

**Interfaces:**
- Consumes: Task 1-5 全部机制
- Produces: 注册于 registry 的 `car_sales_agent` pattern（4 AgentModule：reception / sales_consult / after_sales / complaint，三类边）

- [ ] **Step 1: 写失败测试**

```python
"""car_sales_agent 演示 pattern：结构 + 三类边配置。"""


def test_pattern_discovered_and_edges():
    from src.dialogue.register import discover_builtin_patterns, registry
    imported = discover_builtin_patterns()
    assert "src.dialogue.car_sales_agent" in imported
    p = registry.get("car_sales_agent")
    assert p.entry_module_code == "reception"

    rec = p.module_map["reception"]
    links = {l.target: l for l in rec.sub_modules}
    # 全投影边（知识+工具）
    assert links["after_sales"].lend_knowledge is True
    assert "query_workorder" in links["after_sales"].lend_tools
    # 纯知识边
    assert links["sales_consult"].lend_knowledge is True
    assert links["sales_consult"].lend_tools == []
    # 纯 transfer 边
    assert links["complaint"].lend_knowledge is False

    # 转移图：专家可转回 reception
    assert "reception" in p.dispatch_graph["after_sales"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_car_sales_agent.py -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现 pattern 文件**

```python
"""汽车销售纯 Agent 模式 —— inject/transfer 双原语演示。

结构：
    car_sales_agent (Pattern, entry: reception)
    ├── reception        前台接待（分诊 + 邻接投影代答）
    │     ├── → after_sales    全投影边（知识 + query_workorder 工具）
    │     ├── → sales_consult  纯知识边
    │     └── → complaint      纯 transfer 边（敏感域，不投影）
    ├── sales_consult    购车顾问（可转回 reception）
    ├── after_sales      售后维保（query_workorder；可转回 reception）
    └── complaint        投诉处理（终态，无出边）
"""

from src.dialogue.module import AgentModule, ModuleLink
from src.dialogue.pattern import Pattern
from src.dialogue.register import registry

sales_consult = AgentModule(
    module_code="sales_consult",
    module_name="购车顾问",
    module_description="车型推荐、报价比较、贷款方案咨询",
    module_todo_description="理解购车需求并给出推荐与报价",
    answer_examples=["推荐您看看{model}，目前优惠后 {price} 万，很适合您的需求。"],
    use_tools=[],
    sub_modules=["reception"],
    base_prompt="你是 4S 店购车顾问，热情专业，只谈购车相关话题。",
)

after_sales = AgentModule(
    module_code="after_sales",
    module_name="售后维保",
    module_description="保养预约、维修工单、保险理赔的查询与办理",
    module_todo_description="查改保养预约、跟踪维修工单进度",
    answer_examples=["已为您把保养预约改到{时间}，请按时到店。"],
    use_tools=["query_workorder"],
    sub_modules=["reception"],
    base_prompt="你是 4S 店售后顾问，耐心细致，负责保养预约与维修工单。",
)

complaint = AgentModule(
    module_code="complaint",
    module_name="投诉处理",
    module_description="客户不满受理、安抚与人工升级",
    module_todo_description="记录投诉内容并升级人工专员",
    answer_examples=["非常抱歉给您带来不便，已为您记录并升级专员处理。"],
    use_tools=[],
    sub_modules=[],
    base_prompt="你是投诉处理专员，先安抚情绪，再记录投诉并告知将升级人工。",
)

reception = AgentModule(
    module_code="reception",
    module_name="前台接待",
    module_description="接待客户，解答通用问题，按需分诊到专家",
    module_todo_description="识别客户诉求：能直接答的直接答，需要专家的转交",
    answer_examples=["您好，请问有什么可以帮您？"],
    use_tools=[],
    sub_modules=[
        ModuleLink(target="after_sales",
                   lend_knowledge=True,
                   lend_tools=["query_workorder"]),
        ModuleLink(target="sales_consult",
                   lend_knowledge=True, lend_tools=[]),
        ModuleLink(target="complaint",
                   lend_knowledge=False, lend_tools=[]),
    ],
    base_prompt="你是 4S 店前台接待，友好高效，覆盖购车与售后的通用咨询。",
)

car_sales_agent_pattern = Pattern(
    code="car_sales_agent",
    name="汽车销售 Agent 助手",
    description="纯 agent 模式：前台分诊 + inject 代答 + transfer 深入移交",
    entry_module_code="reception",
    modules=[reception, sales_consult, after_sales, complaint],
)

registry.register(car_sales_agent_pattern)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_car_sales_agent.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + commit**

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q
git add src/dialogue/car_sales_agent.py tests/test_car_sales_agent.py
git commit -m "feat(app): car_sales_agent 纯 agent 演示 pattern（三类邻接边）"
```

---

### Task 8: 工具权限接线 + e2e 冒烟脚本 + 文档同步

**Files:**
- Modify: `src/tools/weather_tool.py`（不动，仅参考）— 实际改 `after_sales` 依赖的工单工具：Create `src/tools/workorder_tool.py`
- Create: `test_agent_e2e.py`（项目根，仿 `test_route_e2e.py` 惯例）
- Modify: `ARCHITECTURE.md`（模块地图补 dispatch/loop 改动）

**Interfaces:**
- Consumes: Task 7 pattern
- Produces: `query_workorder` 工具（`allowed_patterns={"car_sales_agent": ["after_sales"]}`，借出由 link.lend_tools 授权）；e2e 冒烟脚本

- [ ] **Step 1: 写工单工具（仿 weather_tool.py 结构）**

```python
"""Workorder query tool — mock 维修工单查询。

Tool name: ``query_workorder``
Permission: 仅 car_sales_agent pattern 的 after_sales 模块（借出经 ModuleLink.lend_tools 授权）。
"""

from typing import Any, Dict

from src.tools.register import registry, tool_result

_MOCK_ORDERS: Dict[str, Dict[str, Any]] = {
    "京A12345": {"status": "维修中", "item": "更换刹车片", "eta": "明天 17:00"},
}


def _handle_query_workorder(args: Dict[str, Any]) -> str:
    plate = args.get("plate", "")
    order = _MOCK_ORDERS.get(plate)
    if not order:
        return tool_result({"plate": plate, "status": "未找到工单"})
    return tool_result({"plate": plate, **order})


WORKORDER_SCHEMA = {
    "name": "query_workorder",
    "description": "按车牌号查询维修工单状态、项目与预计完工时间。",
    "parameters": {
        "type": "object",
        "properties": {"plate": {"type": "string", "description": "车牌号，如 京A12345"}},
        "required": ["plate"],
    },
}

registry.register(
    name="query_workorder",
    toolset="aftersales",
    schema=WORKORDER_SCHEMA,
    handler=_handle_query_workorder,
    description="查询维修工单（模拟数据）",
    emoji="🔧",
    allowed_patterns={"car_sales_agent": ["after_sales"]},
)
```

- [ ] **Step 2: 离线验证借出链路（ToolRegistry mock 数据）**

在 `tests/test_car_sales_agent.py` 追加：

```python
def test_workorder_tool_lend_chain():
    """工具按 after_sales 名义注册，经 lend_tools 借给 reception。"""
    import src.tools.workorder_tool  # noqa: F401 触发注册
    from src.chat.loop import _resolve_lent_tools
    from src.dialogue.register import discover_builtin_patterns, registry

    discover_builtin_patterns()
    p = registry.get("car_sales_agent")
    rec = p.module_map["reception"]
    schemas, lent_by = _resolve_lent_tools(rec, p)
    assert "query_workorder" in lent_by
    assert lent_by["query_workorder"] == "after_sales"
    assert any(s["function"]["name"] == "query_workorder" for s in schemas)
```

Run: `.venv/bin/python -m pytest tests/test_car_sales_agent.py -v` → PASS

- [ ] **Step 3: e2e 冒烟脚本（真实 key，人工评审）**

`test_agent_e2e.py`（仿 `test_route_e2e.py`）：

```python
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
    print("用户:", r1 := chat(sessions, "e2e", "帮我看下京A12345的工单进度"))
    print("助手:", r1)
    # 场景 2（transfer）：深入售后流程 → 前台移交，售后同轮接话
    print("== 场景2 transfer ==")
    print("用户: 保养预约想改期，顺便保险理赔有纠纷")
    print("助手:", chat(sessions, "e2e", "保养预约想改期，顺便保险理赔有纠纷"))
    # 场景 3（sticky）：继续售后话题 → 仍由 after_sales 持有
    print("== 场景3 sticky ==")
    print("用户: 理赔专员什么时候联系我")
    print("助手:", chat(sessions, "e2e", "理赔专员什么时候联系我"))
    print("最终持有模块:", session.cxt.current_module_code)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 真实 key 跑通 e2e（人工评审回复质量，不断言文案）**

Run: `export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python test_agent_e2e.py`
Expected: 三场景跑通无异常；场景 1 回复含工单状态且无转接话术；场景 3 `current_module_code == "after_sales"`

- [ ] **Step 5: ARCHITECTURE.md 同步 + 全量回归 + commit**

ARCHITECTURE.md「模块依赖图」补 `dispatch.py` 节点与 chat/loop 的新职责一句话（遵循现有格式）。

```bash
export DASHSCOPE_API_KEY=$(grep '^export DASHSCOPE_API_KEY' ~/.zshrc | cut -d= -f2-|tr -d '"') && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -m pytest tests/clarify -q
git add src/tools/workorder_tool.py tests/test_car_sales_agent.py test_agent_e2e.py ARCHITECTURE.md
git commit -m "feat(tools): query_workorder 工单工具与 agent e2e 冒烟；ARCHITECTURE 同步"
```

---

## Self-Review 结论

- **Spec 覆盖**：§2（Task 1/2）、§3（Task 3/4/5）、§4（Task 4 prompt 常量）、§5 错误处理（幻觉目标→工具按图生成+dispatch 二次校验 Task 3/4；移交环→MAX_HOPS Task 5 + 回弹拒绝 Task 3；借入失败→现有 `_execute_tool` 捕获；膨胀→观测 log 归 Task 4 实现细节）、§6（metadata 约定 Task 3 + tool 落盘 Task 4 + sticky 由不自动回收天然满足）、§7（三层测试齐）、§8 清单对齐。§5 的"投影膨胀 log warning"并入 Task 4 `_build_system_prompt` 实现时加一行 `logger.warning` 当 prompt > 4000 字。
- **占位符扫描**：Task 4 Step 3 中 `lent_by = {...占位...}` 片段已明确标注"实现时删除并替换"，非遗留占位。
- **类型一致性**：`TurnResult` 定义于 loop.py（Task 4），chat.py（Task 5）`from src.chat.loop import TurnResult`；`ModuleDispatch/dispatch` 均出自 dispatch.py（Task 3）；`build_projection_block/build_transfer_tools/_resolve_lent_tools` 签名在 Task 4 定义、Task 7/8 测试消费一致。
