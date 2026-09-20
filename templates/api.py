"""模版 API 路由：validate / register / get / list（Q3/Q4/Q9/Q13/Q16）。

依赖经参数注入（channel 式，不反向 import main）：

- ``get_store: Callable[[], Optional[TemplateStore]]``——运行期解析，允许
  main 在 startup 才初始化 store、测试直接替换
- 注册流程：collect-all 校验（有 error 即拒，全量返回）→ 编译 → 动态注册
  （原生同 code 覆盖）→ 原子落盘；GET 返回 canonical sha256 供子 skill
  对齐本地副本（Q13 本地为准：hash 不一致即重注册覆盖）
"""

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from templates.compiler import compile_template
from templates.store import TemplateStore
from templates.validator import validate_template

logger = logging.getLogger(__name__)


class TemplatePayload(BaseModel):
    request_id: str = ""
    template: Dict[str, Any]


class ApiEnvelope(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, Any] = {}


def _load_config_safe() -> Optional[Dict[str, Any]]:
    try:
        from config.config import load_config
        return load_config()
    except Exception:
        return None


def _validate(tpl: Dict[str, Any], store: TemplateStore):
    """collect-all 校验（引用层带上模版来源 code 集合，区分 template/builtin）。"""
    from dialogue.register import registry as pattern_registry
    return validate_template(
        tpl,
        pattern_registry=pattern_registry,
        template_codes=set(store.list_codes()),
        config=_load_config_safe(),
    )


def build_templates_router(
    get_store: Callable[[], Optional[TemplateStore]],
) -> APIRouter:
    router = APIRouter()

    def _require_store() -> Optional[TemplateStore]:
        store = get_store()
        if store is None:
            logger.warning("模版存储未启用（初始化失败），拒绝请求")
        return store

    @router.post("/api/v1/templates/validate")
    def validate_template_endpoint(payload: TemplatePayload, request: Request) -> ApiEnvelope:
        """validate-only：只校验不注册，全量返回 errors/warnings（元 skill G5 硬门槛）。"""
        logger.info("templates/validate: request_id=%s client=%s",
                    payload.request_id, request.client.host if request.client else "?")
        store = _require_store()
        if store is None:
            return ApiEnvelope(code="500", status=False, message="模版存储未启用")
        result = _validate(payload.template, store)
        if result.ok:
            return ApiEnvelope(
                code="0", status=True,
                message=f"校验通过（{len(result.warnings)} 条告警）",
                data=result.to_dict())
        return ApiEnvelope(
            code="400", status=False,
            message=f"校验发现 {len(result.errors)} 个错误",
            data=result.to_dict())

    @router.post("/api/v1/templates")
    def register_template_endpoint(payload: TemplatePayload, request: Request) -> ApiEnvelope:
        """注册：校验（全量报错）→ 编译 → 落盘 → 动态注册（同 code 覆盖）。

        落盘先于注册：save 失败（磁盘满/权限）时返回 500 且内存未生效，
        不再出现"客户端以为失败重试、新会话却已用新版"的分裂；注册在
        校验+编译成功后实际不会再失败，万一失败重启重放会补齐。
        """
        logger.info("templates/register: request_id=%s client=%s",
                    payload.request_id, request.client.host if request.client else "?")
        store = _require_store()
        if store is None:
            return ApiEnvelope(code="500", status=False, message="模版存储未启用")

        result = _validate(payload.template, store)
        if not result.ok:
            return ApiEnvelope(
                code="400", status=False,
                message=f"校验发现 {len(result.errors)} 个错误，未注册",
                data=result.to_dict())

        from dialogue.register import registry as pattern_registry
        try:
            compiled = compile_template(payload.template)
            digest = store.save(payload.template)
            pattern = pattern_registry.register(compiled)
        except Exception as e:
            logger.exception("模版编译/落盘/注册失败")
            return ApiEnvelope(code="500", status=False,
                               message=f"模版注册失败: {e}")
        return ApiEnvelope(
            code="0", status=True,
            message=f"模版 '{pattern.code}' 注册成功（同 code 覆盖，新会话生效）",
            data={"code": pattern.code, "hash": digest,
                  "warnings": [w.to_dict() for w in result.warnings]})

    @router.get("/api/v1/templates/{code}")
    def get_template_endpoint(code: str) -> ApiEnvelope:
        """查询：子 skill 的模版对齐入口（Q13）。

        - 落盘模版：exists + source=template + canonical hash
        - 仅内置 pattern：exists + source=builtin + hash=None（子 skill 应报冲突而非覆盖）
        """
        store = _require_store()
        if store is None:
            return ApiEnvelope(code="500", status=False, message="模版存储未启用")

        summary = store.summarize(code)
        if summary is not None:
            summary["exists"] = True
            summary["source"] = "template"
            return ApiEnvelope(code="0", status=True, message="success", data=summary)

        from dialogue.register import registry as pattern_registry
        pattern = pattern_registry.get(code)
        if pattern is not None:
            return ApiEnvelope(
                code="0", status=True, message="success",
                data={"exists": True, "source": "builtin", "hash": None,
                      "code": code, "name": pattern.name,
                      "description": pattern.description})
        return ApiEnvelope(
            code="404", status=False,
            message=f"模版/Pattern '{code}' 不存在",
            data={"exists": False, "code": code})

    @router.get("/api/v1/templates")
    def list_templates_endpoint() -> ApiEnvelope:
        store = _require_store()
        if store is None:
            return ApiEnvelope(code="500", status=False, message="模版存储未启用")
        templates = []
        for code in store.list_codes():
            summary = store.summarize(code)
            if summary is not None:
                summary["exists"] = True
                summary["source"] = "template"
                templates.append(summary)
        return ApiEnvelope(code="0", status=True, message="success",
                           data={"templates": templates})

    return router
