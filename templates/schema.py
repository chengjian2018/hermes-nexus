"""模版 JSON 的字段词汇表与规范化。

只描述"哪些字段合法、什么形态"，不做语义校验（结构/引用层校验在
validator.py）。normalize_template 负责把原始 JSON 整理成编译器可直接
消费的规范形态，并收集未知字段/不支持字段的告警。
"""

import copy
import re
from typing import Any, Dict, List, Tuple

# pattern code / module_code / node_code 命名约束（与内置 pattern 命名一致）
CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# ----顶层字段----
TOP_REQUIRED = ("code", "name", "description", "entry_module_code", "modules")
TOP_OPTIONAL = (
    "recommended_form",  # fsm|agent|mixed，元 skill 的形态推荐（Q1，仅元数据）
    "form_rationale",    # 推荐理由（仅元数据）
    "counterpart_hint",  # 对端扮演建议（Q2/G2 产出，任务引擎的默认对端配置）
    "max_hops",          # 透传 Pattern 的同轮跳数预算
    "version",           # 元 skill 生成的版本标识（仅元数据）
)

FORM_TYPES = ("fsm", "agent", "mixed")
MODULE_TYPES = ("agent", "fsm", "route")

# 声明式边界：这些字段出现即剔除并告警（见模块 docstring）
TOP_UNSUPPORTED = ("generate", "pre_recall", "query", "post_recall",
                   "stages", "agent_hooks", "messages_builder")

# ----module 层字段----
MODULE_REQUIRED = ("module_code", "type")
MODULE_OPTIONAL = (
    "module_name", "module_description", "module_todo_description",
    "base_prompt", "base_nlu_prompt", "base_nlg_prompt",
    "use_tools", "sub_modules", "enable_clarify", "is_end",
    "answer_examples", "nodes",
)
MODULE_UNSUPPORTED = ("generate", "pre_recall", "query", "post_recall",
                      "agent_stage", "messages_builder", "agent_hooks")

# ----node 层字段----
NODE_REQUIRED = ("node_code",)
NODE_OPTIONAL = (
    "node_name", "node_description", "node_todo_description",
    "sub_nodes", "node_slots", "answer_examples",
    "base_nlu_prompt", "base_nlg_prompt", "is_end", "jump_module",
)
NODE_UNSUPPORTED = ("generate", "pre_recall", "query", "post_recall")

# ----转移边（sub_modules 元素）----
LINK_FIELDS = ("target", "lend_tools", "lend_knowledge")

# ----counterpart_hint----
COUNTERPART_HINT_FIELDS = ("role_prompt", "scripted_replies")


def _norm_str_list(value: Any) -> List[str]:
    """把宽松的列表形态规整为 List[str]（非字符串元素转 str 容忍）。"""
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def normalize_links(sub_modules: Any) -> List[Dict[str, Any]]:
    """sub_modules 规范化为 List[{"target": str, "lend_tools": [str], ...}]。

    str 元素（旧式兼容）包装为 {"target": ...}；dict 元素保留已声明的
    link 字段。非法元素原样保留（由 validator 报错），不在此抛异常。
    """
    if not isinstance(sub_modules, list):
        return []
    links: List[Dict[str, Any]] = []
    for item in sub_modules:
        if isinstance(item, str):
            links.append({"target": item, "lend_tools": []})
        elif isinstance(item, dict):
            link = {k: item[k] for k in LINK_FIELDS if k in item}
            link.setdefault("target", "")
            link["lend_tools"] = _norm_str_list(link.get("lend_tools"))
            links.append(link)
        else:
            links.append({"target": "", "lend_tools": [], "_invalid": item})
    return links


def normalize_template(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """规范化模版 JSON，返回 (normalized, notes)。

    notes 为剔除类告警（未知字段 / 不支持字段），形如
    ``"modules[0]: 不支持字段 generate 已剔除（声明式模版不表达 stage 实例）"``。
    语义错误（悬空边/未知工具等）不在本层，见 validator.validate_template。
    """
    notes: List[str] = []
    if not isinstance(raw, dict):
        return (raw if isinstance(raw, dict) else {}), notes

    tpl = copy.deepcopy(raw)

    for field in TOP_UNSUPPORTED:
        if tpl.pop(field, None) is not None:
            notes.append(
                f"顶层: 不支持字段 {field} 已剔除（声明式模版不表达 stage/代码定制）")
    for field in sorted(set(tpl) - set(TOP_REQUIRED) - set(TOP_OPTIONAL)
                        - set(TOP_UNSUPPORTED)):
        notes.append(f"顶层: 未知字段 {field} 已剔除")
        tpl.pop(field)

    hint = tpl.get("counterpart_hint")
    if isinstance(hint, dict):
        clean = {k: hint[k] for k in COUNTERPART_HINT_FIELDS if k in hint}
        for field in sorted(set(hint) - set(COUNTERPART_HINT_FIELDS)):
            notes.append(f"counterpart_hint: 未知字段 {field} 已剔除")
        tpl["counterpart_hint"] = clean

    modules = tpl.get("modules")
    if isinstance(modules, list):
        norm_modules = []
        for idx, module in enumerate(modules):
            if not isinstance(module, dict):
                norm_modules.append(module)
                continue
            for field in MODULE_UNSUPPORTED:
                if module.pop(field, None) is not None:
                    notes.append(
                        f"modules[{idx}]: 不支持字段 {field} 已剔除"
                        "（声明式模版不表达 stage/代码定制）")
            for field in sorted(set(module) - set(MODULE_REQUIRED)
                                 - set(MODULE_OPTIONAL) - set(MODULE_UNSUPPORTED)):
                notes.append(f"modules[{idx}]: 未知字段 {field} 已剔除")
                module.pop(field)
            module["sub_modules"] = normalize_links(module.get("sub_modules"))
            nodes = module.get("nodes")
            if isinstance(nodes, list):
                norm_nodes = []
                for nidx, node in enumerate(nodes):
                    if not isinstance(node, dict):
                        norm_nodes.append(node)
                        continue
                    for field in NODE_UNSUPPORTED:
                        if node.pop(field, None) is not None:
                            notes.append(
                                f"modules[{idx}].nodes[{nidx}]: 不支持字段 "
                                f"{field} 已剔除（声明式模版不表达 stage 实例）")
                    for field in sorted(set(node) - set(NODE_REQUIRED)
                                         - set(NODE_OPTIONAL) - set(NODE_UNSUPPORTED)):
                        notes.append(
                            f"modules[{idx}].nodes[{nidx}]: 未知字段 {field} 已剔除")
                        node.pop(field)
                    if isinstance(node.get("sub_nodes"), list):
                        node["sub_nodes"] = _norm_str_list(node["sub_nodes"])
                    norm_nodes.append(node)
                module["nodes"] = norm_nodes
            if isinstance(module.get("use_tools"), list):
                module["use_tools"] = _norm_str_list(module["use_tools"])
            norm_modules.append(module)
        tpl["modules"] = norm_modules

    return tpl, notes
