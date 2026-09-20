"""collect-all 三层模版校验器（Q3/Q10，设计共识 §2.2）。

一次跑完三层并收集**全部**问题返回（绝不 fail-fast）：

1. schema 层（schema.py 词汇表）：必填/类型/枚举/code 格式/模块与节点 code 全局唯一
2. 结构层：入口存在、转移边悬空/自环、越权借出、节点转移悬空、模块可达性、
   可达终态存在（镜像 Pattern.__init__ 的 fail-fast 检查并前移为收集式；
   逐节点环检测刻意不做——FSM 澄清环合法，运行期由 max_turns/timeout 兜底）
3. 引用层：use_tools 引用的工具已注册（error）且 ACL 已授予本 pattern
   （warning，allowed_patterns 默认全拒绝）；code 与内置 pattern 冲突
   （error，防动态注册覆盖 builtin）；pattern_llm 配置与本模版的
   modules/nodes 交叉校验（warning）

每个问题带 JSON 路径定位（如 ``modules[1].sub_modules[0].target``），
供元 skill 在 G5 修复与子 skill 注册报错时定位。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from templates.schema import (
    CODE_RE, FORM_TYPES, MODULE_TYPES, normalize_template,
)

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    """一条校验问题：JSON 路径 + 机器可读 code + 人读消息。"""
    path: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    """collect-all 校验结果：ok = errors 为空（warnings 不阻断注册）。"""
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def validate_template(
    raw: Any,
    pattern_registry: Any = None,
    tool_registry: Any = None,
    template_codes: Optional[Set[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """校验一份模版 JSON；registry 参数可注入（默认用真实单例，测试可打桩）。

    Args:
        raw: 模版 JSON（dict 形态；其他形态报 schema 错）
        pattern_registry: Pattern 注册表（内置冲突检查）
        tool_registry: 工具注册表（引用层检查）
        template_codes: 已注册为模版来源的 code 集合（区分 template/builtin）
        config: 已加载的配置 dict（pattern_llm 交叉校验；None 则跳过该层）
    """
    result = ValidationResult()
    tpl, notes = normalize_template(raw)
    for note in notes:
        result.warnings.append(Issue(path="", code="UNSUPPORTED_FIELD", message=note))

    if pattern_registry is None:
        from dialogue.register import registry as pattern_registry  # noqa: F811
    if tool_registry is None:
        from tools.register import registry as tool_registry  # noqa: F811
    template_codes = template_codes or set()

    # ------------------------------------------------------------------
    # 1) schema 层
    # ------------------------------------------------------------------
    if not isinstance(raw, dict):
        result.errors.append(Issue(
            path="", code="TYPE_ERROR",
            message=f"模版应为 JSON 对象，实际为 {type(raw).__name__}"))
        return result

    code = tpl.get("code")
    if not _is_str(code) or not code:
        result.errors.append(Issue(path="code", code="MISSING_FIELD",
                                   message="code 必填且为字符串"))
    elif not CODE_RE.match(code):
        result.errors.append(Issue(
            path="code", code="CODE_FORMAT",
            message=f"code '{code}' 需匹配 {CODE_RE.pattern}"))
    for f in ("name", "description", "entry_module_code"):
        if not _is_str(tpl.get(f)) or not tpl.get(f):
            result.errors.append(Issue(path=f, code="MISSING_FIELD",
                                       message=f"{f} 必填且为非空字符串"))
    modules = tpl.get("modules")
    if not isinstance(modules, list) or not modules:
        result.errors.append(Issue(path="modules", code="MISSING_FIELD",
                                   message="modules 必填且为非空数组"))
        modules = []
    if tpl.get("recommended_form") is not None and tpl["recommended_form"] not in FORM_TYPES:
        result.errors.append(Issue(
            path="recommended_form", code="ENUM_INVALID",
            message=f"recommended_form 需为 {list(FORM_TYPES)} 之一"))
    max_hops = tpl.get("max_hops")
    if max_hops is not None and (not isinstance(max_hops, int) or max_hops < 1):
        result.errors.append(Issue(
            path="max_hops", code="TYPE_ERROR",
            message="max_hops 需为 >=1 的整数"))

    hint = tpl.get("counterpart_hint")
    if hint is not None:
        if not isinstance(hint, dict):
            result.errors.append(Issue(
                path="counterpart_hint", code="TYPE_ERROR",
                message="counterpart_hint 应为对象"))
        else:
            if not _is_str(hint.get("role_prompt")) or not hint.get("role_prompt"):
                result.warnings.append(Issue(
                    path="counterpart_hint.role_prompt", code="MISSING_FIELD",
                    message="建议提供对端角色 prompt（任务引擎的 llm 对端默认配置）"))
            replies = hint.get("scripted_replies")
            if replies is not None and not isinstance(replies, list):
                result.errors.append(Issue(
                    path="counterpart_hint.scripted_replies", code="TYPE_ERROR",
                    message="scripted_replies 应为字符串数组"))

    # ----modules / nodes 的 schema 检查 + code 收集----
    # 两遍扫描：module code 先收全（node_map 扁平，节点唯一性要对照全部模块 code）
    module_codes: List[str] = []
    node_codes: List[str] = []          # (module_idx, node) 展开后的全局 node code
    node_owner: Dict[str, int] = {}     # node_code -> module_idx（跨模块转移告警用）
    for idx, module in enumerate(modules):
        mpath = f"modules[{idx}]"
        if not isinstance(module, dict):
            result.errors.append(Issue(path=mpath, code="TYPE_ERROR",
                                       message=f"module 应为对象，实际为 {type(module).__name__}"))
            continue
        mcode = module.get("module_code")
        if not _is_str(mcode) or not mcode:
            result.errors.append(Issue(path=f"{mpath}.module_code", code="MISSING_FIELD",
                                       message="module_code 必填且为字符串"))
        else:
            if not CODE_RE.match(mcode):
                result.errors.append(Issue(
                    path=f"{mpath}.module_code", code="CODE_FORMAT",
                    message=f"module_code '{mcode}' 需匹配 {CODE_RE.pattern}"))
            if mcode in module_codes:
                result.errors.append(Issue(
                    path=f"{mpath}.module_code", code="DUPLICATE_CODE",
                    message=f"module_code '{mcode}' 重复"))
            else:
                module_codes.append(mcode)
        mtype = module.get("type")
        if mtype not in MODULE_TYPES:
            result.errors.append(Issue(
                path=f"{mpath}.type", code="ENUM_INVALID",
                message=f"type 需为 {list(MODULE_TYPES)} 之一，实际为 {mtype!r}"))
        for f in ("module_name", "module_description", "module_todo_description"):
            if module.get(f) is not None and not _is_str(module.get(f)):
                result.errors.append(Issue(
                    path=f"{mpath}.{f}", code="TYPE_ERROR", message=f"{f} 应为字符串"))
        for f in ("base_prompt", "base_nlu_prompt", "base_nlg_prompt"):
            if module.get(f) is not None and not _is_str(module.get(f)):
                result.errors.append(Issue(
                    path=f"{mpath}.{f}", code="TYPE_ERROR", message=f"{f} 应为字符串"))
        if module.get("enable_clarify") is not None and not isinstance(module.get("enable_clarify"), bool):
            result.errors.append(Issue(
                path=f"{mpath}.enable_clarify", code="TYPE_ERROR",
                message="enable_clarify 应为布尔值"))
        if module.get("is_end") is not None and not isinstance(module.get("is_end"), bool):
            result.errors.append(Issue(
                path=f"{mpath}.is_end", code="TYPE_ERROR", message="is_end 应为布尔值"))
        if module.get("answer_examples") is not None and not isinstance(module.get("answer_examples"), list):
            result.errors.append(Issue(
                path=f"{mpath}.answer_examples", code="TYPE_ERROR",
                message="answer_examples 应为字符串数组"))

        nodes = module.get("nodes")
        if nodes is not None and not isinstance(nodes, list):
            result.errors.append(Issue(
                path=f"{mpath}.nodes", code="TYPE_ERROR", message="nodes 应为数组"))
            nodes = None
        if mtype in ("fsm", "route") and nodes is not None and not nodes:
            result.warnings.append(Issue(
                path=f"{mpath}.nodes", code="EMPTY_NODES",
                message=f"{mtype} 模块没有任何节点，FSM 管线无从转移"))
        if mtype == "agent" and nodes:
            result.warnings.append(Issue(
                path=f"{mpath}.nodes", code="AGENT_WITH_NODES",
                message="agent 模块的 nodes 不会被状态机管线消费（仅入 node_map）"))
        if mtype == "agent" and not module.get("base_prompt"):
            result.warnings.append(Issue(
                path=f"{mpath}.base_prompt", code="MISSING_FIELD",
                message="agent 模块建议提供 base_prompt（否则只有默认 system 骨架）"))

    for idx, module in enumerate(modules):
        if not isinstance(module, dict):
            continue
        mpath = f"modules[{idx}]"
        for nidx, node in enumerate(module.get("nodes") or []):
            npath = f"{mpath}.nodes[{nidx}]"
            if not isinstance(node, dict):
                result.errors.append(Issue(
                    path=npath, code="TYPE_ERROR",
                    message=f"node 应为对象，实际为 {type(node).__name__}"))
                continue
            ncode = node.get("node_code")
            if not _is_str(ncode) or not ncode:
                result.errors.append(Issue(path=f"{npath}.node_code", code="MISSING_FIELD",
                                           message="node_code 必填且为字符串"))
            else:
                if not CODE_RE.match(ncode):
                    result.errors.append(Issue(
                        path=f"{npath}.node_code", code="CODE_FORMAT",
                        message=f"node_code '{ncode}' 需匹配 {CODE_RE.pattern}"))
                if ncode in node_codes or ncode in module_codes:
                    # node_map 是扁平的：module_code 也会经 _init_node 进入 node_map，
                    # node_code 撞任何 module_code/既有 node_code 都会互相遮蔽
                    result.errors.append(Issue(
                        path=f"{npath}.node_code", code="DUPLICATE_CODE",
                        message=f"node_code '{ncode}' 与既有 module/node code 冲突（node_map 扁平）"))
                else:
                    node_codes.append(ncode)
                    node_owner[ncode] = idx
            if node.get("node_slots") is not None and not isinstance(node.get("node_slots"), dict):
                result.errors.append(Issue(
                    path=f"{npath}.node_slots", code="TYPE_ERROR",
                    message="node_slots 应为 {槽位名: 描述} 对象"))
            if node.get("is_end") is not None and not isinstance(node.get("is_end"), bool):
                result.errors.append(Issue(
                    path=f"{npath}.is_end", code="TYPE_ERROR", message="is_end 应为布尔值"))
            if node.get("jump_module") is not None and not _is_str(node.get("jump_module")):
                result.errors.append(Issue(
                    path=f"{npath}.jump_module", code="TYPE_ERROR",
                    message="jump_module 应为字符串"))

    # ------------------------------------------------------------------
    # 2) 结构层（module 图）
    # ------------------------------------------------------------------
    module_set = set(module_codes)
    entry = tpl.get("entry_module_code")
    if _is_str(entry) and entry and module_set and entry not in module_set:
        result.errors.append(Issue(
            path="entry_module_code", code="ENTRY_MISSING",
            message=f"入口模块 '{entry}' 不在 modules 中"))

    edges: Dict[str, Set[str]] = {m: set() for m in module_codes}
    for idx, module in enumerate(modules):
        if not isinstance(module, dict) or not _is_str(module.get("module_code")):
            continue
        mcode = module["module_code"]
        mpath = f"modules[{idx}]"
        for lidx, link in enumerate(module.get("sub_modules") or []):
            lpath = f"{mpath}.sub_modules[{lidx}]"
            target = link.get("target") if isinstance(link, dict) else None
            if not _is_str(target) or not target:
                result.errors.append(Issue(
                    path=lpath, code="TYPE_ERROR",
                    message="转移边应为 \"模块code\" 或 {target, lend_tools} 对象"))
                continue
            if target == mcode:
                result.errors.append(Issue(
                    path=lpath, code="SELF_LOOP",
                    message=f"自环转移边: {mcode} → {target}"))
                continue
            if target not in module_set:
                result.errors.append(Issue(
                    path=lpath, code="DANGLING_EDGE",
                    message=f"悬空转移边: {mcode} → {target}（目标模块不存在）"))
                continue
            if target in edges[mcode]:
                result.warnings.append(Issue(
                    path=lpath, code="DUPLICATE_EDGE",
                    message=f"重复转移边: {mcode} → {target}"))
                continue
            edges[mcode].add(target)
            # 越权借出：lend_tools ⊆ target.use_tools（镜像 Pattern 检查）
            tmodule = next(
                (m for m in modules
                 if isinstance(m, dict) and m.get("module_code") == target), None)
            ttools = set((tmodule or {}).get("use_tools") or [])
            unauthorized = sorted(set(link.get("lend_tools") or []) - ttools)
            if unauthorized:
                result.errors.append(Issue(
                    path=lpath, code="UNAUTHORIZED_LEND",
                    message=f"越权借出: {mcode} 借出 {unauthorized}，"
                            f"但 {target}.use_tools 未声明这些工具"))

        # ----节点转移边（node_map 扁平：node code ∪ module code 均合法目标）----
        for nidx, node in enumerate(module.get("nodes") or []):
            if not isinstance(node, dict) or not _is_str(node.get("node_code")):
                continue
            ncode = node["node_code"]
            npath = f"{mpath}.nodes[{nidx}]"
            for tidx, target in enumerate(node.get("sub_nodes") or []):
                spath = f"{npath}.sub_nodes[{tidx}]"
                if target == ncode:
                    # 自指合法（澄清环：NLU 可连续输出同一 next_node），仅提示
                    continue
                if target in module_set:
                    result.warnings.append(Issue(
                        path=spath, code="NODE_TO_MODULE",
                        message=f"节点转移 {ncode} → {target} 指向模块头投影节点"
                                "（跨层转移，建议改用 jump_module）"))
                elif target not in set(node_codes):
                    result.errors.append(Issue(
                        path=spath, code="DANGLING_EDGE",
                        message=f"节点转移 {ncode} → {target} 悬空（目标节点不存在）"))
                elif node_owner.get(target) not in (None, idx):
                    result.warnings.append(Issue(
                        path=spath, code="CROSS_MODULE_NODE",
                        message=f"节点转移 {ncode} → {target} 跨模块"
                                "（node_map 扁平所以合法，注意模块边界语义）"))
            jump = node.get("jump_module")
            if jump:
                if jump not in module_set:
                    result.errors.append(Issue(
                        path=f"{npath}.jump_module", code="DANGLING_EDGE",
                        message=f"jump_module → {jump} 不存在"))
                elif jump == mcode:
                    result.errors.append(Issue(
                        path=f"{npath}.jump_module", code="SELF_LOOP",
                        message=f"jump_module → {jump} 模块自环"))

    # ----可达性（模块图 BFS）----
    if _is_str(entry) and entry in module_set:
        seen = {entry}
        queue = [entry]
        while queue:
            cur = queue.pop(0)
            for nxt in edges.get(cur, ()):  # noqa: B023
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for unreachable in sorted(module_set - seen):
            result.warnings.append(Issue(
                path="modules", code="UNREACHABLE",
                message=f"模块 '{unreachable}' 从入口不可达"))
        # 可达终态：可达模块的 is_end，或可达模块内节点的 is_end
        has_terminal = any(
            (m.get("is_end") for m in modules
             if isinstance(m, dict) and m.get("module_code") in seen
             and isinstance(m.get("is_end"), bool)) )
        if not has_terminal:
            has_terminal = any(
                (isinstance(n, dict) and n.get("is_end")
                 for m in modules
                 if isinstance(m, dict) and m.get("module_code") in seen
                 for n in (m.get("nodes") or [])))
        if not has_terminal:
            result.warnings.append(Issue(
                path="modules", code="NO_TERMINAL",
                message="无可达终态（is_end），任务只能靠 max_turns/timeout 收束"))

    # ----recommended_form 与实际模块形态的一致性（warning）----
    form = tpl.get("recommended_form")
    if _is_str(form) and module_codes:
        types = {m.get("type") for m in modules if isinstance(m, dict)}
        if form == "agent" and "agent" not in types:
            result.warnings.append(Issue(
                path="recommended_form", code="FORM_MISMATCH",
                message="推荐 agent 但没有任何 agent 模块"))
        if form == "fsm" and not ({"fsm", "route"} & types):
            result.warnings.append(Issue(
                path="recommended_form", code="FORM_MISMATCH",
                message="推荐 fsm 但没有任何 fsm/route 模块"))

    # ------------------------------------------------------------------
    # 3) 引用层
    # ------------------------------------------------------------------
    if _is_str(code) and code:
        # 内置冲突：code 已注册但不是模版来源 → 动态注册会覆盖 builtin
        if pattern_registry.is_registered(code) and code not in template_codes:
            result.errors.append(Issue(
                path="code", code="BUILTIN_CONFLICT",
                message=f"code '{code}' 与已注册的内置 pattern 冲突（动态注册会覆盖它），请换一个 code"))

    for idx, module in enumerate(modules):
        if not isinstance(module, dict):
            continue
        mcode = module.get("module_code")
        for tidx, tool in enumerate(module.get("use_tools") or []):
            tpath = f"modules[{idx}].use_tools[{tidx}]"
            try:
                entry_obj = tool_registry.get_entry(tool)
            except Exception:
                logger.exception("工具注册表查询失败: %s", tool)
                entry_obj = None
            if entry_obj is None:
                result.errors.append(Issue(
                    path=tpath, code="TOOL_UNKNOWN",
                    message=f"工具 '{tool}' 未注册"))
                continue
            try:
                allowed = tool_registry.get_allowed_tools_for_pattern(code, mcode)
            except Exception:
                allowed = set()
            if tool not in allowed:
                result.warnings.append(Issue(
                    path=tpath, code="TOOL_ACL",
                    message=f"工具 '{tool}' 已注册但未授予 pattern '{code}'"
                            "（allowed_patterns 默认全拒绝），运行时对该模块不可见"))

    # pattern_llm 交叉校验（config 缺省时跳过）
    if config is not None and _is_str(code) and code:
        pcfg = (config.get("pattern_llm") or {}).get(code) or {}
        for mcode in (pcfg.get("modules") or {}):
            if mcode not in module_set:
                result.warnings.append(Issue(
                    path="pattern_llm", code="PATTERN_LLM_MISMATCH",
                    message=f"pattern_llm.{code}.modules 配置了模版中不存在的 module '{mcode}'"))
        for ncode in (pcfg.get("nodes") or {}):
            if ncode not in set(node_codes):
                result.warnings.append(Issue(
                    path="pattern_llm", code="PATTERN_LLM_MISMATCH",
                    message=f"pattern_llm.{code}.nodes 配置了模版中不存在的 node '{ncode}'"))

    return result
