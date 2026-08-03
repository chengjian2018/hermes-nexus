"""
Hermes Nexus — 人机交互 Mock 服务 (FastAPI)。

提供 REST API 接口：
- POST /api/v1/chat      发起对话
- POST /api/v1/chat/stream  流式对话（SSE）
- GET  /api/v1/health     健康检查
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.build_prompt import build_system_prompt
from src.chat import TerminalChannel
from src.chat import ChatSession as _ChatSession

# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(
    title="Hermes Nexus",
    description="人机交互 Mock 服务 — 智能用餐需求收集助手",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# 数据模型
# ============================================================================


class SectionItem(BaseModel):
    field: str = ""
    label: str = ""
    value: Any = None
    source: str = ""
    note: Optional[str] = None


class Section(BaseModel):
    name: str = ""
    label: str = ""
    description: str = ""
    status: str = "pending"
    items: list[SectionItem] = []
    completion_criteria: str = ""
    criteria_met: bool = False
    dialogue_summary: str = ""


class InteractionObject(BaseModel):
    object_id: str = ""
    name: str = ""
    address: str = ""
    phone: str = ""
    extra_info: dict = {}


class ChatRequest(BaseModel):
    """对话请求体。"""
    task_id: str = Field(default="", description="任务 ID")
    task_type: str = Field(default="food-finding", description="任务类型")
    summary: str = Field(default="", description="任务摘要")
    sections: list[dict] = Field(default_factory=list, description="信息收集 sections")
    interaction_objects: Optional[list[dict]] = Field(
        default=None, description="候选餐厅/交互对象"
    )
    # mock 相关参数
    mock_mode: bool = Field(default=False, description="是否启用渠道 mock 模式")
    mock_responses: Optional[list[str]] = Field(
        default=None, description="mock 模式下的预设用户响应"
    )
    max_turns: int = Field(default=30, ge=1, le=100, description="最大对话轮数")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatResponse(BaseModel):
    """对话响应体。"""
    task_id: str = ""
    messages: list[ChatMessage] = []
    status: str = "completed"


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "hermes-nexus"
    version: str = "0.1.0"


# ============================================================================
# API 端点
# ============================================================================


@app.get("/api/v1/health", response_model=HealthResponse)
async def health_check():
    """健康检查端点。"""
    return HealthResponse()


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """发起对话。

    接收任务请求，运行多轮对话，返回消息记录（不含 system prompt）。

    Request Body:
        - sections: 必填，信息收集的 sections 列表
        - task_id: 任务标识
        - mock_mode: 是否 mock 模式（默认 false）
        - mock_responses: mock 模式下的预设用户响应

    Returns:
        ChatResponse: 包含 task_id、消息列表、状态
    """
    try:
        session = _ChatSession()
        result = session.run(
            request_data=request.model_dump(exclude_none=True),
            mock_mode=request.mock_mode,
            mock_responses=request.mock_responses,
            max_turns=request.max_turns,
        )
        # 安全打印：忽略无法编码的字符
        try:
            print(result["messages"])
        except UnicodeEncodeError:
            print(f"[INFO] 对话完成，共 {len(result['messages'])} 条消息 (含非 ASCII 字符)")
        return ChatResponse(
            task_id=result["task_id"],
            messages=[ChatMessage(**m) for m in result["messages"]],
            status=result["status"],
        )

    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"编码错误: {str(exc)}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"对话执行失败: {str(exc)}")


@app.get("/api/v1/prompt/preview")
async def preview_prompt(
    task_id: str = Query(default="", description="任务 ID"),
    summary: str = Query(default="", description="任务摘要"),
):
    """预览组装后的 system prompt（不发起对话）。

    用于调试 prompt 组装效果。
    """
    from src.build_prompt import build_system_prompt

    # 返回默认示例
    prompt = build_system_prompt(
        sections=[],
        interaction_objects=[],
        summary=summary or "示例任务摘要",
        task_id=task_id or "preview_task",
    )
    return {"task_id": task_id, "prompt": prompt, "prompt_length": len(prompt)}


# ============================================================================
# 启动入口
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
