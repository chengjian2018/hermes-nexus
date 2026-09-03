"""Channel registry —— 第 4 个 registry，与 pattern/tool/llm 同构。

每个渠道文件模块级 ``registry.register(Spec())`` 自注册；AST 扫描自动
发现并 import（包内走 importlib.import_module，包外目录——如测试 tmp——
走 spec_from_file_location）。框架文件（base/register/webhooks）在排除
清单里，不会被当渠道 import。
"""

import ast
import importlib
import importlib.util
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")  # 用在 URL 路径里：小写 snake/kebab


def _is_registry_register_call(node: ast.AST) -> bool:
    """True 当 *node* 是模块顶层 ``registry.register(...)`` 表达式。"""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_channel(module_path: Path) -> bool:
    """True 当模块含模块级 registry.register() 调用（文本预过滤 + AST）。"""
    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "registry" not in source or "register" not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        return False
    return any(_is_registry_register_call(stmt) for stmt in tree.body)


_EXCLUDED = {"__init__.py", "register.py", "base.py", "webhooks.py"}
_CHANNEL_PKG = "src.channel"


class ChannelRegistry:
    """渠道注册表：name -> spec。register 时做结构校验，坏声明 import 期拦住。"""

    def __init__(self):
        self._channels: dict = {}
        self._lock = threading.RLock()

    def register(self, spec: Any) -> Any:
        """注册渠道 spec；name 非法/重名/钩子不可调用抛 ValueError。"""
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"channel name 非法（须匹配 {_NAME_RE.pattern}）: {name!r}")
        for hook in ("parse", "build_reply"):
            if not callable(getattr(spec, hook, None)):
                raise ValueError(f"channel '{name}' 的 {hook} 不可调用")
        with self._lock:
            if name in self._channels:
                raise ValueError(f"channel '{name}' 已注册（防同名冲突）")
            self._channels[name] = spec
        logger.info("Registered channel: %s", name)
        return spec

    def get(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._channels.get(name)

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._channels.keys())

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._channels


def _import_channel_module(path: Path) -> Optional[str]:
    """import 单个渠道文件，返回模块名；失败 warning 返回 None。

    包内文件走 import_module（可被重复 import 幂等）；包外（测试 tmp 目
    录）走 spec_from_file_location，模块名用 ``_channel_ext_<stem>`` 防与
    真模块撞名。
    """
    try:
        if _CHANNEL_PKG + "." + path.stem in sys.modules or (
            path.parent == Path(__file__).resolve().parent
        ):
            mod_name = importlib.import_module(f"{_CHANNEL_PKG}.{path.stem}").__name__
            return mod_name
        mod_name = f"_channel_ext_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return mod_name
    except Exception as e:
        logger.warning("Could not import channel module %s: %s", path, e)
        return None


def discover_builtin_channels(channels_dir: Optional[Path] = None) -> List[str]:
    """扫描渠道目录，import 含模块级 registry.register() 的文件。

    Returns: 成功 import 的模块名列表（import 失败的单文件 warning 跳过）。
    """
    channels_path = (
        Path(channels_dir) if channels_dir is not None
        else Path(__file__).resolve().parent
    )
    imported: List[str] = []
    for path in sorted(channels_path.glob("*.py")):
        if path.name in _EXCLUDED or not _module_registers_channel(path):
            continue
        mod_name = _import_channel_module(path)
        if mod_name is not None:
            imported.append(mod_name)
    return imported


registry = ChannelRegistry()
