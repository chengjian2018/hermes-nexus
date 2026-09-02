"""hermes-nexus 调试 CLI —— 命令行测试模板对话。

子命令（fire 分发）:
    cli.py chat                  交互 REPL（默认）
    cli.py ask "你好"            单问单答（--session-id 可续聊库中会话）
    cli.py list                  列出已注册 patterns / llm providers / tools
    cli.py sessions              列出 data/dialogue.db 中的会话

选择交互: 不带 --pattern/--llm 启动时出 prompt_toolkit 方向键菜单
(pattern 单级; llm 两级 provider → models)。调试输出: -v 简要 / -vv 完整。

用法示例:
    .venv/bin/python cli.py chat --pattern car_sales_route -vv
    .venv/bin/python cli.py ask "我想买车" --session-id t1
    .venv/bin/python cli.py list patterns
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import fire

from src.chat.chat import chat as chat_turn
from src.chat.session import Session
from src.chat.store import SessionStore
from src.dialogue.register import discover_builtin_patterns, registry as pattern_registry
from src.llm.register import discover_builtin_providers, registry as llm_registry
from src.tools.register import discover_builtin_tools
from src.tools.register import registry as tool_registry
from config.config import get_llm_config, get_session_db_path

# ============================================================================
# ANSI 着色（NO_COLOR / 非 TTY 自动降级；见 https://no-color.org）
# ============================================================================

_COLOR_OK = sys.stdout.isatty() and "NO_COLOR" not in os.environ

def _c(text: str, code: str) -> str:
    if not _COLOR_OK:
        return text
    return f"\033[{code}m{text}\033[0m"

def dim(t):    return _c(t, "2")
def bold(t):   return _c(t, "1")
def green(t):  return _c(t, "32")
def cyan(t):   return _c(t, "36")
def yellow(t): return _c(t, "33")
def red(t):    return _c(t, "31")


# ============================================================================
# 纯函数：菜单渲染 / verbose 格式化 / 斜杠命令解析（单测覆盖）
# ============================================================================

def render_pattern_menu(title: str, patterns: List[Any]) -> str:
    """数字菜单的可测渲染（实际选择用 prompt_toolkit radiolist）。"""
    lines = [bold(title)]
    for i, p in enumerate(patterns, 1):
        lines.append(f"  {i}. {p.code} — {p.name}")
        if getattr(p, "description", ""):
            lines.append(dim(f"     {p.description}"))
    return "\n".join(lines)


def render_verbose_summary(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """-v 层：node 转移、intent/next_node、slots 变化。

    before/after 为轮次快照 dict: current_node_code / current_module_code /
    filled_slots / intent / next_node（缺省键视为未变化时忽略）。
    """
    lines: List[str] = []

    node_from, node_to = before.get("current_node_code"), after.get("current_node_code")
    mod_from, mod_to = before.get("current_module_code"), after.get("current_module_code")
    if mod_from != mod_to and node_from != node_to:
        lines.append(dim(f"  [{mod_from}·{node_from}] → [{mod_to}·{node_to}]"))
    elif node_from != node_to:
        lines.append(dim(f"  node: {node_from} → {node_to}"))
    elif mod_from != mod_to:
        lines.append(dim(f"  module: {mod_from} → {mod_to}"))

    intent = after.get("intent")
    if intent:
        lines.append(dim(f"  intent: {intent}"))
    next_node = after.get("next_node")
    if next_node and next_node != node_from:
        lines.append(dim(f"  next_node: {next_node}"))

    slots_before = before.get("filled_slots") or {}
    slots_after = after.get("filled_slots") or {}
    changed = {k: v for k, v in slots_after.items() if slots_before.get(k) != v}
    if changed:
        parts = [f"{k}={v!r}" for k, v in sorted(changed.items())]
        lines.append(dim(f"  slots: {', '.join(parts)}"))

    return "\n".join(lines)


def render_verbose_full(cxt) -> str:
    """-vv 层：完整 nlu/nlg JSON、recall、dispatch_log、agent tool 调用。"""
    lines: List[str] = [dim("  ── context ──")]

    if getattr(cxt, "nlu_result", None):
        lines.append(dim("  nlu_result: " + json.dumps(cxt.nlu_result, ensure_ascii=False)))
    if getattr(cxt, "nlg_result", None):
        lines.append(dim("  nlg_result: " + json.dumps(cxt.nlg_result, ensure_ascii=False)))
    if getattr(cxt, "agent_result", None):
        lines.append(dim("  agent_result: " + _safe_json(cxt.agent_result)))

    recall = getattr(cxt, "format_recall_info", None)
    if recall and (recall_info := recall()):
        lines.append(dim("  recall: " + recall_info.replace("\n", " | ")))

    meta = getattr(cxt, "metadata", None) or {}
    dispatch_log = meta.get("dispatch_log")
    if dispatch_log:
        lines.append(dim("  dispatch_log: " + _safe_json(dispatch_log)))

    return "\n".join(lines)


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(obj)


def parse_slash_command(line: str) -> Optional[Dict[str, Any]]:
    """解析 REPL 斜杠命令；非斜杠输入返回 None。

    返回 dict(name=..., arg=...)；arg 为命令后剩余文本（可空），
    未知命令返回 dict(name="unknown", arg=原始命令词)。
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    name = parts[0][1:].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if name not in _SLASH_COMMANDS:
        return {"name": "unknown", "arg": name}
    return {"name": name, "arg": arg}


_SLASH_COMMANDS = ("help", "exit", "reset", "slots", "new", "llm")

# provider/model 菜单的「维持 config 配置」固定项（spec §4.1：空 override =
# 完全按 yaml 三层编排解析）
KEEP_CONFIG = "__keep_config__"


def _keep_config_entry() -> Dict[str, str]:
    return {"value": KEEP_CONFIG, "label": "（维持 config 配置）",
            "hint": "不手动指定，按 yaml pattern/module/node 配置解析"}


def _patch_select(picked: str):
    """测试钩子：固定 select_from_menu 返回值。"""
    from unittest.mock import patch as _patch
    return _patch(__name__ + ".select_from_menu", return_value=picked)


def _provider_menu_entries() -> List[Dict[str, str]]:
    return [_keep_config_entry()] + [
        {"value": p.code, "label": f"{p.code} — {p.name}",
         "hint": getattr(p, "description", "")}
        for p in llm_registry.list_providers()
    ]


# ============================================================================
# prompt_toolkit 交互：方向键菜单 + 主输入行
# ============================================================================

def _ptk_import():
    try:
        from prompt_toolkit import PromptSession as PtkPromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import HTML
        return PtkPromptSession, Completer, Completion, HTML
    except ImportError:
        return None, None, None, None


def select_from_menu(title: str, entries: List[Dict[str, str]]) -> Optional[str]:
    """内联方向键选择器；不可用/非 TTY/取消时降级数字输入。

    不用 radiolist_dialog：其全屏对话框里 Enter 只标记选中，还需 Tab 到
    Ok 按钮再 Enter 才关闭——首按 Enter "卡住不动" 的体验即源于此。
    自建 Application：↑↓ 移动、Enter 直接返回选中 value、Esc/中断取消。

    entries: [{value, label, hint}]，返回 value 或 None（取消）。
    """
    mods = _ptk_import()
    if mods[0] is None or not (sys.stdout.isatty() and sys.stdin.isatty()):
        return _select_by_number(title, entries)
    PtkPromptSession, _, _, HTML = mods

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.formatted_text import HTML, to_formatted_text

    state = {"index": 0}

    def _render():
        parts = [f"<b>{title}</b>\n",
                 "<ansibrightblack>↑↓ 选择，Enter 确认，Esc 取消</ansibrightblack>\n"]
        for i, e in enumerate(entries):
            mark = "›" if i == state["index"] else " "
            hint = (f"  <ansibrightblack>{e['hint']}</ansibrightblack>"
                    if e.get("hint") else "")
            line = f"{mark} {i + 1}. {e['label']}{hint}"
            if i == state["index"]:
                line = f"<ansicyan><b>{line}</b></ansicyan>"
            parts.append(line + "\n")
        # FormattedTextControl callable 需返回扁平 fragments：整体转一次
        return to_formatted_text(HTML("".join(parts)))

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["index"] = max(0, state["index"] - 1)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        state["index"] = min(len(entries) - 1, state["index"] + 1)

    @kb.add("enter")
    def _accept(event):
        event.app.exit(result=entries[state["index"]]["value"])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    # 动态重渲染：FormattedTextControl 的 text 支持 callable
    body = FormattedTextControl(lambda: _render())
    app = Application(
        layout=Layout(HSplit([Window(content=body, dont_extend_height=True,
                                     wrap_lines=True)])),
        key_bindings=kb,
        full_screen=False,
    )
    result = app.run()
    return result


def _select_by_number(title: str, entries: List[Dict[str, str]]) -> Optional[str]:
    """数字菜单降级路径（无 prompt_toolkit / 非 TTY）。EOF 视为取消。"""
    print(render_pattern_menu(title.replace("选择 ", ""), [
        type("P", (), {"code": e["value"], "name": e["label"],
                       "description": e.get("hint", "")})() for e in entries
    ]))
    while True:
        try:
            raw = input("输入编号（回车取消）: ").strip()
        except EOFError:
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return entries[int(raw) - 1]["value"]
        print(red(f"无效编号: {raw}"))


class SlashCompleter:
    """主输入行斜杠命令补全。"""

    def __init__(self):
        mods = _ptk_import()
        self._Completer = mods[1]
        self._Completion = mods[2]

    def get_completer(self):
        if self._Completer is None:
            return None

        completer_self = self

        class _Impl(self._Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                parts = text.split(maxsplit=1)
                cmd = parts[0][1:].lower()
                for name in _SLASH_COMMANDS:
                    if name.startswith(cmd):
                        yield completer_self._Completion(
                            name, start_position=-len(cmd)
                        )

        return _Impl()


# ============================================================================
# 会话装配（复刻 main.py _launch_session_core 的绑定流程，不依赖 FastAPI）
# ============================================================================

def build_session(session_id: str, pattern_code: str,
                  llm_overrides: Optional[Dict[str, Any]] = None) -> Session:
    """创建 Session 并完成 pattern/llm 绑定。

    llm_overrides 非空时预置 metadata.llm_override（逐轮刷新下仍优先生效）；
    model 必填（各 stage 以 llm_config["model"] 下标访问）。
    """
    pattern = pattern_registry.get(pattern_code)
    if pattern is None:
        raise SystemExit(red(f"pattern '{pattern_code}' 未注册，可用: "
                             f"{pattern_registry.list_codes()}"))

    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map

    if llm_overrides:
        # 只写用户显式选择的 truthy 字段；空 override（如「维持 config 配置」）
        # 不写 llm_override —— 否则空 dict 仍为 truthy，会 pin 全局快照，
        # 三层覆盖 + 热生效全部失效（spec §4.1：空 override = 按 yaml 解析）
        picked = {k: v for k, v in llm_overrides.items() if v}
        if picked:
            session.cxt.metadata["llm_override"] = picked

    return session


def resolve_llm_choice(llm: str, model: str,
                       interactive: bool = True) -> Dict[str, Any]:
    """把 --llm/--model flag 与交互菜单解析为 llm_overrides dict。

    返回 {code, model}（均可能为空串 = 用 yaml 默认）。
    """
    code = llm or ""
    if not code and interactive:
        # yaml 已配 provider 时静默沿用其默认（菜单只服务"想换"的场景）；
        # 未配置才弹菜单选择
        try:
            code = get_llm_config().get("code") or ""
        except Exception:
            code = ""
        if code:
            return {"code": "", "model": ""}  # 空 override = 完全用 yaml 默认
        if llm_registry.list_providers():
            picked = select_from_menu("选择 LLM provider", _provider_menu_entries())
            if picked == KEEP_CONFIG:
                return {"code": "", "model": ""}
            code = picked or ""

    resolved_model = model or ""
    if code and not resolved_model and interactive:
        entry = llm_registry.get(code)
        models = list(getattr(entry, "models", None) or [])
        if models:
            entries = [_keep_config_entry()] + [
                {"value": m, "label": m} for m in models]
            picked = select_from_menu(f"选择 model（{code}）", entries) or ""
            if picked == KEEP_CONFIG:
                resolved_model = ""
            else:
                resolved_model = picked
        else:
            hint = f"（回车用 {getattr(entry, 'default_model', '') or '默认'}）"
            resolved_model = input(f"输入 model 名 {hint}: ").strip()
            if resolved_model == KEEP_CONFIG:
                resolved_model = ""

    return {"code": code, "model": resolved_model}


# ============================================================================
# 轮次执行 + 持久化（SessionStore 与 web 服务共用同一 db）
# ============================================================================

def _snapshot(cxt) -> Dict[str, Any]:
    nlu = cxt.nlu_result or {}
    return {
        "current_module_code": cxt.current_module_code,
        "current_node_code": cxt.current_node_code,
        "filled_slots": dict(cxt.filled_slots or {}),
        "intent": nlu.get("intent"),
        "next_node": nlu.get("next_node"),
    }


def run_turn(session: Session, query: str, sessions: Dict[str, Session],
             store: Optional[SessionStore], verbose: int = 0) -> str:
    """执行一轮对话：快照 → chat() → 落盘 → verbose 渲染。"""
    before = _snapshot(session.cxt)
    start_idx = len(session.cxt.history)

    reply = chat_turn(query, session.session_id, sessions)

    if store is not None:
        try:
            store.save_turn(session, start_idx)
        except Exception:
            logging.getLogger(__name__).exception("落盘失败（不影响对话）")

    if verbose >= 1:
        summary = render_verbose_summary(before, _snapshot(session.cxt))
        if summary:
            print(summary)
    if verbose >= 2:
        full = render_verbose_full(session.cxt)
        if full.strip():
            print(full)
    return reply


def _open_store(persist) -> Optional[SessionStore]:
    # fire 把 --persist=false 解析成字符串 'false'（truthy！），归一化：
    if persist in (False, "false", "False", "0", 0, None):
        return None
    try:
        return SessionStore(get_session_db_path())
    except Exception:
        logging.getLogger(__name__).exception("会话存储不可用，降级为内存态")
        return None


def _find_or_create(session_id: str, pattern_code: str,
                    llm_overrides: Dict[str, Any], store: Optional[SessionStore],
                    sessions: Dict[str, Session]) -> Session:
    """--session-id 命中库中未过期会话则恢复续聊，否则新建并落盘。"""
    if store is not None:
        for restored, _ in store.load_active_sessions(ttl_seconds=7 * 24 * 3600):
            if restored.session_id == session_id:
                pattern = pattern_registry.get(restored.pattern_code)
                if pattern is None:
                    print(yellow(f"会话 {session_id} 的 pattern "
                                 f"'{restored.pattern_code}' 未注册，新建会话"))
                    break
                restored.pattern = pattern
                restored.cxt.module_map = pattern.module_map
                restored.cxt.node_map = pattern.node_map
                if llm_overrides:
                    picked = {k: v for k, v in llm_overrides.items() if v}
                    if picked:
                        restored.cxt.metadata["llm_override"] = picked
                print(green(f"已恢复会话 {session_id} "
                            f"({restored.pattern_code})，继续对话"))
                sessions[session_id] = restored
                return restored

    session = build_session(session_id, pattern_code, llm_overrides)
    sessions[session_id] = session
    if store is not None:
        store.create_session(session)
    return session


# ============================================================================
# REPL
# ============================================================================

HELP_TEXT = """\
命令:
  /help            显示本帮助
  /exit            退出（Ctrl-D 同效）
  /reset           重置当前会话（同 pattern 重新开始）
  /slots           显示当前 slots / node / module 状态
  /new [pattern]   换 pattern 新会话（无参数出选择菜单）
  /llm [code]      切换 LLM（无参数出选择菜单，只影响后续轮次）\
"""


def _prompt_text(session: Session):
    """REPL 提示符。prompt_toolkit 路径返回 formatted_text（自带着色，
    不解析裸 ANSI 码——传 str 会把 \\033[... 原样显示成乱码）；
    input() 降级路径返回带 ANSI 码的 str。"""
    cfg = session.cxt.llm_config or {}
    model = cfg.get("model") or "默认model"
    text = f"你 ({session.pattern_code}/{model})> "
    if _COLOR_OK:
        from prompt_toolkit.formatted_text import HTML
        return HTML(f"<b><ansicyan>{text}</ansicyan></b>")
    return text


def repl_loop(pattern_code: str, session_id: str, llm_overrides: Dict[str, Any],
              persist: bool, verbose: int) -> None:
    """交互聊天主循环。"""
    sessions: Dict[str, Session] = {}
    store = _open_store(persist)

    session = _find_or_create(session_id, pattern_code, llm_overrides,
                              store, sessions)

    PtkPromptSession = _ptk_import()[0]
    ptk_session = PtkPromptSession() if PtkPromptSession else None
    completer = SlashCompleter().get_completer()

    print(dim("输入对话内容；/help 查看命令；Ctrl-D 或 /exit 退出"))
    while True:
        try:
            if ptk_session is not None and completer is not None:
                line = ptk_session.prompt(_prompt_text(session),
                                          completer=completer)
            elif ptk_session is not None:
                line = ptk_session.prompt(_prompt_text(session))
            else:
                line = input(_prompt_text(session))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        cmd = parse_slash_command(line)
        if cmd is None:
            if not line.strip():
                continue
            reply = run_turn(session, line.strip(), sessions, store, verbose)
            print(green(f"助手: {reply}"))
            continue

        name, arg = cmd["name"], cmd["arg"]
        if name == "unknown":
            print(red(f"未知命令 /{arg}，/help 查看命令列表"))
        elif name == "help":
            print(HELP_TEXT)
        elif name == "exit":
            break
        elif name == "reset":
            session = _do_reset(session, sessions, store, verbose)
        elif name == "slots":
            _print_slots(session)
        elif name == "new":
            session = _do_new(arg, sessions, store, llm_overrides, verbose)
            if session is None:
                continue
        elif name == "llm":
            _do_llm(session, arg)

    if store is not None:
        store.close()


def _do_reset(session: Session, sessions: Dict[str, Session], store, verbose: int):
    """/reset：同 pattern 重开新会话（沿用原 session_id 与 llm 配置）。"""
    llm_override = session.cxt.metadata.get("llm_override")
    new_session = build_session(session.session_id, session.pattern_code)
    if llm_override:
        new_session.cxt.metadata["llm_override"] = llm_override
    sessions[session.session_id] = new_session
    if store is not None:
        store.create_session(new_session)  # 同 id 视为新一代（launch_epoch+1）
    print(green(f"会话已重置: {session.session_id}"))
    return new_session


def _do_new(arg: str, sessions: Dict[str, Session], store, llm_overrides, verbose: int):
    """/new [pattern]：换 pattern 新会话。"""
    code = arg.strip() or _pick_pattern()
    if not code:
        print(yellow("未选择，保持当前会话"))
        return None
    pattern = pattern_registry.get(code)
    if pattern is None:
        print(red(f"pattern '{code}' 未注册，可用: {pattern_registry.list_codes()}"))
        return None
    new_id = f"{code}-{os.getpid()}" if store is not None else "cli"
    if new_id in sessions:
        new_id = f"{new_id}-{len(sessions)}"
    new_session = build_session(new_id, code, llm_overrides)
    sessions[new_id] = new_session
    if store is not None:
        store.create_session(new_session)
    print(green(f"新会话: {new_id} ({code})"))
    return new_session


def _do_llm(session: Session, arg: str) -> None:
    """/llm [code]：切换后续轮次的 provider/model。"""
    overrides = resolve_llm_choice(arg.strip(), "", interactive=True)
    picked = {k: v for k, v in overrides.items() if v}
    if not picked:
        # 「维持 config 配置」：删除 override，后续轮次按 yaml 三层解析
        session.cxt.metadata.pop("llm_override", None)
        print(green("LLM 已切回 config 配置"))
        return
    # 只写显式选择字段；连接字段（api_base 等）一律由解析时的
    # llm_providers[code] 段提供，不得铺入全量快照（防跨 provider 串台）
    session.cxt.metadata["llm_override"] = picked
    print(green(f"LLM 已切换: {overrides['code']} / {overrides['model'] or '默认model'}"))


def _print_slots(session: Session) -> None:
    cxt = session.cxt
    print(dim(f"  module: {cxt.current_module_code}  node: {cxt.current_node_code}"))
    if cxt.filled_slots:
        for k, v in sorted(cxt.filled_slots.items()):
            print(dim(f"  {k} = {v!r}"))
    else:
        print(dim("  (无 slots)"))


def _pick_pattern() -> str:
    patterns = pattern_registry.list_patterns()
    if not patterns:
        print(red("没有已注册的 pattern"))
        return ""
    return select_from_menu(
        "选择 pattern",
        [{"value": p.code, "label": f"{p.code} — {p.name}",
          "hint": getattr(p, "description", "")} for p in patterns],
    ) or ""


# ============================================================================
# fire 子命令
# ============================================================================

def _ensure_discovery() -> None:
    discover_builtin_patterns()
    discover_builtin_providers()
    discover_builtin_tools()


def chat(pattern: str = "", session_id: str = "cli", llm: str = "", model: str = "",
         verbose: int = 0, persist: bool = True) -> None:
    """交互 REPL 测试模板对话。

    Args:
        pattern: pattern code（缺省出选择菜单）
        session_id: 会话 id；带持久化时命中库中未过期会话则续聊
        llm: provider code（缺省出选择菜单）
        model: model 名（缺省且选了 provider 时出 model 菜单）
        verbose: 调试层级 0/1/2（命令行 -v/-vv 自动展开）
        persist: 落盘 data/dialogue.db（默认开）
    """
    _ensure_discovery()
    # 恢复优先序：显式 --pattern 新起会话；否则显式 --session-id 命中库中
    # 未过期会话则直接恢复（不弹菜单）；两者都没有才弹菜单选择。
    # fire 的默认参数值与用户显式传值不可区分，用 argv 检测显式传参。
    sid_specified = any(a == "--session-id" or a.startswith("--session-id=")
                        for a in sys.argv)
    restored_code = ""
    if not pattern and sid_specified:
        store = _open_store(persist)
        if store is not None:
            try:
                for restored, _ in store.load_active_sessions(7 * 24 * 3600):
                    if restored.session_id == session_id:
                        restored_code = restored.pattern_code
                        break
            finally:
                store.close()
    pattern_code = pattern or restored_code or _pick_pattern()
    if not pattern_code:
        print(yellow("未选择 pattern，退出"))
        return
    llm_overrides = resolve_llm_choice(llm, model, interactive=True)
    # 默认 session-id 拼进程号：避免命中库中任意旧会话把刚选的 pattern 换掉
    sid = session_id if (sid_specified or pattern) else f"{session_id}-{os.getpid()}"
    repl_loop(pattern_code, sid, llm_overrides, persist, verbose)


def ask(query: str, pattern: str = "", session_id: str = "cli-ask", llm: str = "",
        model: str = "", verbose: int = 0, persist: bool = True) -> None:
    """单问单答（--session-id 可续聊库中会话）。"""
    _ensure_discovery()
    pattern_code = pattern
    if not pattern_code:
        # one-shot 不出菜单：恢复已有会话时 pattern 从库中来；否则要求显式 --pattern
        store = _open_store(persist)
        if store is not None:
            for restored, _ in store.load_active_sessions(7 * 24 * 3600):
                if restored.session_id == session_id:
                    pattern_code = restored.pattern_code
                    break
            store.close()
        if not pattern_code:
            raise SystemExit(red("ask 需要显式 --pattern，或 --session-id 命中已有会话"))
    llm_overrides = resolve_llm_choice(llm, model, interactive=False)
    sessions: Dict[str, Session] = {}
    store = _open_store(persist)
    session = _find_or_create(session_id, pattern_code, llm_overrides,
                              store, sessions)
    reply = run_turn(session, query, sessions, store, verbose)
    print(reply)
    if store is not None:
        store.close()


def list_cmd(target: str = "all") -> None:
    # 注意：不能命名为 list——会遮蔽内置 list()，模块内 f-string 的
    # list(...) 会变成递归调用本函数。fire 子命令名在 fire.Fire dict 里映射。
    """列出已注册对象: patterns | llms | tools | all。"""
    _ensure_discovery()
    if target in ("patterns", "all"):
        patterns = pattern_registry.list_patterns()
        print(bold(f"patterns ({len(patterns)}):"))
        for p in patterns:
            print(f"  {p.code} — {p.name}")
            if getattr(p, "description", ""):
                print(dim(f"    {p.description}"))
    if target in ("llms", "all"):
        providers = llm_registry.list_providers()
        print(bold(f"llm providers ({len(providers)}):"))
        for prov in providers:
            print(f"  {prov.code} — {prov.name}")
            print(dim(f"    default_model={getattr(prov, 'default_model', '')} "
                      f"models={list(getattr(prov, 'models', None) or [])}"))
    if target in ("tools", "all"):
        names = tool_registry.get_all_tool_names()
        print(bold(f"tools ({len(names)}):"))
        for n in names:
            print(f"  {n}")


def sessions(pattern_code: str = "", limit: int = 20) -> None:
    """列出持久化会话（按 last_active_at 倒序）。"""
    try:
        store = SessionStore(get_session_db_path())
    except Exception as e:
        raise SystemExit(red(f"会话存储不可用: {e}"))
    try:
        rows = store.list_sessions(pattern_code=pattern_code or None,
                                   limit=limit)
    finally:
        store.close()
    if not rows:
        print(yellow("（无会话记录）"))
        return
    print(bold(f"{'session_id':24} {'pattern':22} {'node':16} {'msgs':>4}  last_active"))
    for r in rows:
        print(f"{r['session_id']:24} {r['pattern_code']:22} "
              f"{str(r['current_node_code']):16} {r['message_count']:>4}  "
              f"{_fmt_ts(r['last_active_at'])}")


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _expand_short_verbose(argv: List[str]) -> List[str]:
    """把 -v/-vv/-vvv 展开为 --verbose=N（fire 不支持计数短 flag）。"""
    mapping = {"-v": "--verbose=1", "-vv": "--verbose=2", "-vvv": "--verbose=3"}
    return [mapping.get(a, a) for a in argv]


if __name__ == "__main__":
    sys.argv[1:] = _expand_short_verbose(sys.argv[1:])
    fire.Fire({
        "chat": chat,
        "ask": ask,
        "list": list_cmd,
        "sessions": sessions,
    })
