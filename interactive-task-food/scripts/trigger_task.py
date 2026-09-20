#!/usr/bin/env python3
"""hermes-nexus 领域交互子 skill 的触发/轮询客户端（纯标准库，无三方依赖）。

用法（两个子命令）：

  # 模版对齐：探活（必要时自动拉起服务）→ hash 对比 → 缺失/不一致时注册
  python trigger_task.py ensure --template references/template.json

  # 触发任务：先 ensure（可用 --no-ensure 跳过）→ POST /tasks → 轮询到终态
  python trigger_task.py run \
      --template references/template.json \
      --kickoff "你好，想订周五晚上7点4个人的位子" \
      --task-info-json '{"goal":"订位","party_size":4}' \
      --counterpart-mode llm --role-prompt "你是餐厅值班经理"

进度信息打 stderr；最终任务结果 JSON 打 stdout（供 skill 解析做后置判断）。
退出码：0 成功；1 失败（stderr 带原因与服务端全量校验错误）。
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_HERMES_PATH = str(Path.home() / "py_projects" / "hermes-nexus")
POLL_INTERVAL_MAX = 10.0


def log(msg: str) -> None:
    print(f"[trigger_task] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP 基元
# ---------------------------------------------------------------------------

def _request(method: str, url: str, payload=None, timeout: float = 30.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api(base_url: str, method: str, path: str, payload=None):
    return _request(method, base_url.rstrip("/") + path, payload)


# ---------------------------------------------------------------------------
# 服务探活与自动拉起（Q17）
# ---------------------------------------------------------------------------

def ensure_server(base_url: str, hermes_path: str, wait_seconds: float = 20.0) -> bool:
    try:
        api(base_url, "GET", "/api/v1/health")
        return True
    except Exception:
        pass

    hermes = Path(hermes_path).expanduser()
    main_py = hermes / "main.py"
    if not main_py.exists():
        log(f"hermes-nexus 未运行，且路径不存在: {hermes}")
        log(f"请手动启动: cd {hermes} && .venv/bin/python main.py")
        return False

    venv_python = hermes / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    log(f"服务未运行，尝试后台拉起: cd {hermes} && {python} main.py")
    log_file = hermes / "data" / "server.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as fh:
        subprocess.Popen(
            [python, "main.py"], cwd=str(hermes), stdout=fh, stderr=fh,
            start_new_session=True,
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            api(base_url, "GET", "/api/v1/health")
            log("服务已就绪")
            return True
        except Exception:
            time.sleep(0.5)
    log(f"自动拉起失败，日志见 {log_file}；请手动启动后重试")
    return False


# ---------------------------------------------------------------------------
# 模版对齐（Q13：本地为准）
# ---------------------------------------------------------------------------

def canonical_hash(tpl: dict) -> str:
    """与服务端 templates/store.py template_hash 完全一致的 canonical sha256。"""
    text = json.dumps(tpl, ensure_ascii=False, sort_keys=True, indent=2)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_template(template_path: str, base_url: str, hermes_path: str) -> int:
    tpl = json.loads(Path(template_path).read_text(encoding="utf-8"))
    code = tpl.get("code", "")
    local_hash = canonical_hash(tpl)

    if not ensure_server(base_url, hermes_path):
        return 1

    try:
        info = api(base_url, "GET", f"/api/v1/templates/{code}")
    except urllib.error.HTTPError as e:
        log(f"查询模版失败: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
        return 1

    if info.get("status"):
        data = info.get("data") or {}
        source = data.get("source")
        if source == "builtin":
            log(f"code '{code}' 与内置 pattern 冲突（服务端拒绝覆盖），请更换模版 code")
            return 1
        if data.get("hash") == local_hash:
            log(f"模版 '{code}' 已注册且 hash 一致，无需注册")
            return 0
        log(f"模版 '{code}' hash 不一致（本地新版），覆盖重注册")
    else:
        log(f"模版 '{code}' 未注册，注册中")

    try:
        result = api(base_url, "POST", "/api/v1/templates",
                     {"request_id": "trigger_task", "template": tpl})
    except urllib.error.HTTPError as e:
        log(f"注册失败: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
        return 1

    if not result.get("status"):
        log(f"注册被拒: {result.get('message')}")
        for issue in (result.get("data") or {}).get("errors", []):
            log(f"  error [{issue.get('code')}] {issue.get('path')}: {issue.get('message')}")
        for issue in (result.get("data") or {}).get("warnings", []):
            log(f"  warn  [{issue.get('code')}] {issue.get('path')}: {issue.get('message')}")
        return 1

    log(f"注册成功: {result.get('message')} hash={result['data']['hash'][:12]}…")
    for issue in (result.get("data") or {}).get("warnings", []):
        log(f"  warn  [{issue.get('code')}] {issue.get('path')}: {issue.get('message')}")
    return 0


# ---------------------------------------------------------------------------
# 触发与轮询（Q11/Q5/Q14）
# ---------------------------------------------------------------------------

def run_task(args) -> int:
    if not args.no_ensure:
        rc = ensure_template(args.template, args.base_url, args.hermes_path)
        if rc != 0:
            return rc

    counterpart = {"mode": args.counterpart_mode}
    if args.counterpart_mode == "scripted":
        script = json.loads(args.script_json) if args.script_json else []
        if not script:
            log("scripted 对端需要 --script-json（非空数组）")
            return 1
        counterpart["script"] = [str(s) for s in script]
    if args.role_prompt:
        counterpart["role_prompt"] = args.role_prompt

    task_info = json.loads(args.task_info_json) if args.task_info_json else {}
    payload = {
        "request_id": "trigger_task",
        "pattern_code": args.pattern_code,
        "task_info": task_info,
        "counterpart": counterpart,
        "max_turns": args.max_turns,
        "timeout_s": args.timeout_s,
    }
    if args.kickoff:
        payload["kickoff"] = args.kickoff
    if args.llm_override_json:
        # 调试字段：pattern 侧 LLM 走覆盖配置（离线测试/换模型）
        payload["llm_override"] = json.loads(args.llm_override_json)

    try:
        created = api(args.base_url, "POST", "/api/v1/tasks", payload)
    except urllib.error.HTTPError as e:
        log(f"触发失败: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
        return 1
    if not created.get("status"):
        log(f"触发失败: {created.get('message')}")
        return 1

    task_id = created["data"]["task_id"]
    session_id = created["data"]["session_id"]
    log(f"任务已触发: task_id={task_id} session_id={session_id}")
    log(f"会话历史: GET {args.base_url}/api/v1/sessions/{session_id}/messages")

    interval = 1.0
    while True:
        try:
            view = api(args.base_url, "GET", f"/api/v1/tasks/{task_id}")
        except urllib.error.HTTPError as e:
            log(f"轮询失败: HTTP {e.code}")
            return 1
        if not view.get("status"):
            log(f"轮询失败: {view.get('message')}")
            return 1
        data = view["data"]
        status = data.get("status")
        if status in ("done", "failed"):
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0 if status == "done" else 1
        log(f"running: turns={data.get('turn_count')} "
            f"module={data.get('current_module')}")
        time.sleep(interval)
        interval = min(interval * 1.6, POLL_INTERVAL_MAX)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="hermes-nexus 领域任务触发客户端")

    # 公共参数挂到主解析器与两个子命令上（允许出现在子命令之后）
    def _add_common(p):
        p.add_argument("--base-url", default=DEFAULT_BASE_URL)
        p.add_argument("--hermes-path", default=DEFAULT_HERMES_PATH)

    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ensure = sub.add_parser("ensure", help="模版对齐（探活/hash/注册）")
    p_ensure.add_argument("--template", required=True, help="template.json 路径")
    _add_common(p_ensure)

    p_run = sub.add_parser("run", help="确保模版后触发任务并轮询到终态")
    p_run.add_argument("--template", required=True)
    p_run.add_argument("--pattern-code", help="默认取 template.json 的 code")
    p_run.add_argument("--kickoff", help="首条喂给 pattern 的消息；缺省=task_info JSON")
    p_run.add_argument("--task-info-json", default="{}", help="任务信息 JSON 字符串")
    p_run.add_argument("--counterpart-mode", choices=["scripted", "llm"], default="llm")
    p_run.add_argument("--script-json", help='scripted 对端脚本，如 \'["好的","稍等"]\'')
    p_run.add_argument("--role-prompt", help="llm 对端角色 prompt（缺省用模版 counterpart_hint）")
    p_run.add_argument("--max-turns", type=int, default=20)
    p_run.add_argument("--timeout-s", type=float, default=600.0)
    p_run.add_argument("--llm-override-json",
                       help='pattern 侧 LLM 覆盖配置（调试），如 \'{"code":"...","model":"..."}\'')
    p_run.add_argument("--no-ensure", action="store_true", help="跳过模版对齐（已确保注册时）")
    _add_common(p_run)

    args = parser.parse_args(argv)
    if args.command == "ensure":
        return ensure_template(args.template, args.base_url, args.hermes_path)

    if not args.pattern_code:
        tpl = json.loads(Path(args.template).read_text(encoding="utf-8"))
        args.pattern_code = tpl.get("code", "")
    return run_task(args)


if __name__ == "__main__":
    sys.exit(main())
