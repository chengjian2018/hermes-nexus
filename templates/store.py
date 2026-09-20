"""模版落盘存储与启动重放（Q9：data/templates/{code}.json + 重启重放）。

- 落盘为规范 JSON（sort_keys + indent），文件内容哈希即模版哈希——
  子 skill 用同一算法对本地副本计算 hash 与服务端对齐（Q13 本地为准）
- 原子写（tmp + os.replace），读失败/坏文件不抛出（返回 None 由调用方跳过）
- replay_templates 供 startup 调用：逐个编译注册，单个失败仅告警不阻断
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def canonical_dumps(tpl: Dict[str, Any]) -> str:
    """模版的规范序列化（哈希基准）：sort_keys + ensure_ascii=False。"""
    return json.dumps(tpl, ensure_ascii=False, sort_keys=True, indent=2)


def template_hash(tpl: Dict[str, Any]) -> str:
    """模版内容 sha256（基于 canonical_dumps）。"""
    return hashlib.sha256(canonical_dumps(tpl).encode("utf-8")).hexdigest()


class TemplateStore:
    """data/templates/ 目录下的 {code}.json 文件存储。"""

    def __init__(self, dir_path: str):
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, code: str) -> Path:
        return self._dir / f"{code}.json"

    def save(self, tpl: Dict[str, Any]) -> str:
        """原子写入模版文件，返回内容哈希。"""
        path = self._path(tpl["code"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(canonical_dumps(tpl) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return template_hash(tpl)

    def load(self, code: str) -> Optional[Dict[str, Any]]:
        """读取模版；文件不存在/解析失败返回 None（坏文件告警由调用方处理）。"""
        path = self._path(code)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("模版文件解析失败，跳过: %s", path)
            return None

    def hash_of(self, code: str) -> Optional[str]:
        tpl = self.load(code)
        return template_hash(tpl) if tpl is not None else None

    def list_codes(self) -> List[str]:
        return sorted(p.stem for p in self._dir.glob("*.json"))

    def summarize(self, code: str) -> Optional[Dict[str, Any]]:
        """GET /templates/{code} 的摘要信息（不含完整模版体）。"""
        tpl = self.load(code)
        if tpl is None:
            return None
        return {
            "code": tpl.get("code"),
            "name": tpl.get("name"),
            "description": tpl.get("description"),
            "recommended_form": tpl.get("recommended_form"),
            "version": tpl.get("version"),
            "hash": template_hash(tpl),
        }


def replay_templates(store: TemplateStore, pattern_registry: Any) -> List[str]:
    """启动重放：把落盘模版逐个编译并注册进 PatternRegistry。

    单个模版编译失败仅记日志跳过（与 discover_builtin_patterns 的容错
    语义一致），不让一个坏文件阻断整个服务启动。

    Returns:
        成功重放注册的 code 列表。
    """
    from templates.compiler import compile_template

    restored: List[str] = []
    for code in store.list_codes():
        tpl = store.load(code)
        if tpl is None:
            continue
        try:
            pattern_registry.register(compile_template(tpl))
            restored.append(code)
        except Exception:
            logger.exception("重放模版失败，跳过: %s", code)
    if restored:
        logger.info("启动重放模版 %d 个: %s", len(restored), restored)
    return restored
