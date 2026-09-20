"""模版编译器：规范化的模版 JSON → Pattern 对象（Q8/Q9）。

编译假设入参已经通过 validator（结构合法性由校验器保证）；编译后由
调用方执行 ``registry.register(pattern)``（原生同 code 覆盖）与落盘。
counterpart_hint / recommended_form 等元数据挂到 Pattern 属性上，
供任务引擎取默认对端配置（不参与 Pattern 构造校验）。
"""

import logging
from typing import Any, Dict, List

from dialogue.module import AgentModule, FSMModule, ModuleLink, RouteModule
from dialogue.node import BaseNode
from dialogue.pattern import Pattern

logger = logging.getLogger(__name__)

_MODULE_CLASSES = {
    "agent": AgentModule,
    "fsm": FSMModule,
    "route": RouteModule,
}


def _compile_node(node: Dict[str, Any]) -> BaseNode:
    kwargs = {}
    if node.get("jump_module"):
        kwargs["jump_module"] = node["jump_module"]
    return BaseNode(
        node_code=node.get("node_code"),
        node_name=node.get("node_name"),
        node_description=node.get("node_description"),
        node_todo_description=node.get("node_todo_description"),
        sub_nodes=list(node.get("sub_nodes") or []),
        node_slots=dict(node.get("node_slots") or {}),
        answer_examples=node.get("answer_examples"),
        base_nlu_prompt=node.get("base_nlu_prompt"),
        base_nlg_prompt=node.get("base_nlg_prompt"),
        is_end=bool(node.get("is_end", False)),
        **kwargs,
    )


def _compile_links(sub_modules: List[Any]):
    """规范化后的 link dict 列表 → str / ModuleLink 混合列表（BaseModule 自行归一）。"""
    links = []
    for link in sub_modules or []:
        if isinstance(link, str):
            links.append(link)
        elif isinstance(link, dict) and link.get("target"):
            links.append(ModuleLink(
                target=link["target"],
                lend_tools=list(link.get("lend_tools") or []),
                lend_knowledge=bool(link.get("lend_knowledge", True)),
            ))
    return links


def _compile_module(module: Dict[str, Any]):
    cls = _MODULE_CLASSES[module["type"]]
    return cls(
        module_code=module.get("module_code"),
        module_name=module.get("module_name"),
        module_description=module.get("module_description"),
        module_todo_description=module.get("module_todo_description"),
        module_nodes=[_compile_node(n) for n in (module.get("nodes") or [])],
        sub_modules=_compile_links(module.get("sub_modules")),
        use_tools=list(module.get("use_tools") or []),
        base_prompt=module.get("base_prompt"),
        base_nlu_prompt=module.get("base_nlu_prompt"),
        base_nlg_prompt=module.get("base_nlg_prompt"),
        enable_clarify=bool(module.get("enable_clarify", False)),
        is_end=bool(module.get("is_end", False)),
        answer_examples=module.get("answer_examples"),
    )


def compile_template(tpl: Dict[str, Any]) -> Pattern:
    """编译（已校验通过的）模版 JSON 为 Pattern；结构非法时由 Pattern.__init__ fail-fast。"""
    pattern_kwargs = {}
    if tpl.get("max_hops") is not None:
        pattern_kwargs["max_hops"] = int(tpl["max_hops"])
    pattern = Pattern(
        code=tpl["code"],
        name=tpl["name"],
        description=tpl["description"],
        entry_module_code=tpl["entry_module_code"],
        modules=[_compile_module(m) for m in tpl.get("modules") or []],
        **pattern_kwargs,
    )
    # 元数据挂载（不经构造参数，避免 kwargs setattr 依赖 modules 分支的隐晦路径）
    pattern.recommended_form = tpl.get("recommended_form")
    pattern.counterpart_hint = tpl.get("counterpart_hint") or {}
    pattern.template_version = tpl.get("version")
    return pattern
