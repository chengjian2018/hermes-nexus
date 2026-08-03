"""
对话编排模块。

负责整个对话流程的编排：
1. 创建 system_prompt
2. 发起沟通线程（打开渠道）
3. 多轮对话（调用 LLM）
4. 判断沟通是否结束
5. 结束后关闭沟通线程
6. 组织消息记录，返回

LLM 调用基于 OpenAI 兼容 SDK，默认使用 DeepSeek。
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from openai import OpenAI

from src.build_prompt import build_system_prompt
from src.channel import TerminalChannel

# ============================================================================
# 配置
# ============================================================================

CONVERSATION_COMPLETE_MARKER = "[CONVERSATION_COMPLETE]"
DEFAULT_MAX_TURNS = 30

# DeepSeek 默认配置（API key 需通过环境变量 LLM_API_KEY 或参数传入）
DEFAULT_API_KEY = ""  # 不硬编码，通过环境变量或参数传入
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


def _get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ============================================================================
# ChatSession
# ============================================================================


class ChatSession:
    """对话会话编排器。

    管理一次完整的用户对话：组装 prompt → 开启渠道 → 多轮对话 → 结束。

    使用 OpenAI 兼容 SDK 调用 LLM，默认指向 DeepSeek。
    无 API key 时自动 fallback 到 mock 模式。

    Usage:
        session = ChatSession()
        result = session.run(
            request_data={"sections": [...], "task_id": "..."},
            mock_mode=True,
            mock_responses=["好的", "没问题"],
        )
        # result = {"task_id": "...", "messages": [...], "status": "completed"}
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout: float = 60.0,
    ):
        """初始化对话会话。

        Args:
            api_key: LLM API 密钥。为空时依次从环境变量 LLM_API_KEY、默认值读取。
            base_url: LLM API 地址。为空时从环境变量 LLM_BASE_URL 读取，默认 DeepSeek。
            model: 模型名称。为空时从环境变量 LLM_MODEL 读取，默认 deepseek-v4-pro。
            timeout: HTTP 请求超时秒数。
        """
        self.api_key = api_key or _get_env("LLM_API_KEY", DEFAULT_API_KEY)
        self.base_url = (base_url or _get_env("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or _get_env("LLM_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

        # 初始化 OpenAI 客户端
        self._client: Optional[OpenAI] = None

    @property
    def client(self) -> OpenAI:
        """懒初始化 OpenAI 客户端。"""
        if self._client is None:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    @property
    def use_mock_llm(self) -> bool:
        """是否使用 mock LLM 模式（无 API key 时自动 fallback）。"""
        return not self.api_key

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------

    def run(
        self,
        request_data: dict,
        *,
        mock_mode: bool = False,
        mock_responses: list[str] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> dict:
        """运行一次完整的对话会话。

        Args:
            request_data: 请求数据，需包含 sections 字段，
                          可选 task_id / interaction_objects / summary。
            mock_mode: 是否为渠道使用 mock 模式（不读取真实终端）。
            mock_responses: 渠道 mock 模式下的预设用户响应。
            max_turns: 最大对话轮数。

        Returns:
            dict:
                - task_id: 任务 ID
                - messages: 消息列表（不含 system prompt），
                  每条为 {"role": "user"|"assistant", "content": "..."}
                - status: "completed" | "max_turns_reached" | "error"
        """
        # 1. 构建 system_prompt
        sections = request_data.get("sections", [])
        interaction_objects = request_data.get("interaction_objects", [])
        summary = request_data.get("summary", "")
        task_id = request_data.get("task_id", "")

        system_prompt = build_system_prompt(
            sections=sections,
            interaction_objects=interaction_objects,
            summary=summary,
            task_id=task_id,
        )

        # 2. 打开渠道
        channel = TerminalChannel(
            mock_mode=mock_mode,
            mock_responses=mock_responses,
        )
        channel.open()

        # 3. 初始化消息列表（与 OpenAI API 格式一致）
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "您好，餐馆预定，请问您有什么需要？"}
        ]

        status = "completed"

        try:
            # 4. 发送首条问候
            greeting = self._call_llm(messages)
            if greeting:
                channel.send(greeting)
                messages.append({"role": "assistant", "content": greeting})

            # 5. 多轮对话循环
            for _ in range(max_turns):
                if not channel.is_active():
                    break

                user_input = channel.receive()
                if user_input is None:
                    if channel.should_close():
                        break
                    continue  # 空输入，忽略

                messages.append({"role": "user", "content": user_input})

                response = self._call_llm(messages)
                if response is None:
                    status = "error"
                    break

                # 6. 检查是否对话结束
                if CONVERSATION_COMPLETE_MARKER in response:
                    response = response.replace(CONVERSATION_COMPLETE_MARKER, "").strip()
                    channel.send(response)
                    messages.append({"role": "assistant", "content": response})
                    channel.signal_close()
                    break

                channel.send(response)
                messages.append({"role": "assistant", "content": response})
            else:
                # 达到最大轮数
                status = "max_turns_reached"

        except Exception as exc:
            status = "error"
            messages.append({"role": "error", "content": str(exc)})

        finally:
            # 7. 关闭渠道
            channel.close()

        # 8. 返回消息记录（不含 system prompt）
        return {
            "task_id": task_id,
            "messages": [m for m in messages if m["role"] != "system"],
            "status": status,
        }

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, messages: list[dict]) -> Optional[str]:
        """调用 LLM API 获取回复。

        Args:
            messages: 当前对话消息列表，格式 [{"role": "...", "content": "..."}, ...]

        Returns:
            LLM 的回复文本，出错时返回 None。
        """
        if self.use_mock_llm:
            return self._mock_llm_response(messages)

        return self._real_llm_call(messages)

    def _real_llm_call(self, messages: list[dict]) -> Optional[str]:
        """通过 OpenAI 兼容 SDK 调用真实 LLM（默认 DeepSeek）。

        参考 llm_chat_demo.py 的调用方式：
            response = client.chat.completions.create(model=..., messages=...)
            reply = response.choices[0].message.content
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=1024,
            )
            content = response.choices[0].message.content
            if content is None:
                print("[WARN] LLM 返回空内容", flush=True)
                return None
            # 确保返回纯文本字符串（防御性处理）
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            return str(content).strip()

        except UnicodeDecodeError as exc:
            print(f"[ERROR] LLM 响应编码错误 ({self.model} @ {self.base_url}): {exc}", flush=True)
            print(f"[ERROR] 请检查 API 返回的编码格式，预期为 UTF-8", flush=True)
            return None
        except Exception as exc:
            print(f"[WARN] LLM API 调用失败 ({self.model} @ {self.base_url}): {exc}", flush=True)
            return None

    # ------------------------------------------------------------------
    # Mock LLM 响应
    # ------------------------------------------------------------------

    def _mock_llm_response(self, messages: list[dict]) -> str:
        """生成 mock LLM 回复，用于无 API key 时的测试。

        模拟公司行政人员打电话给餐厅预定座位的风格。
        """
        last_msg = messages[-1] if messages else {}
        last_role = last_msg.get("role", "")
        last_content = str(last_msg.get("content", ""))

        # 提取餐厅名称（从 system prompt 中）
        restaurant_name = "餐厅"
        sys_msg = messages[0] if messages else {}
        if sys_msg.get("role") == "system":
            match = re.search(r'打电话给\*\*(.+?)\*\*预定', sys_msg.get("content", ""))
            if match:
                restaurant_name = match.group(1)

        # 如果是 system prompt，返回打电话开场白
        if last_role == "system":
            return (
                f"喂～你好，请问是{restaurant_name}吗？"
                f"我想预定一下明天的团建聚餐，方便聊两句吗？"
            )

        # 检查是否为最终确认（短句 + 关键词）
        done_keywords = ["确认", "没问题", "可以", "好的", "行", "对", "就这样", "定了", "预定成功"]
        user_lower = last_content.lower().strip()

        if any(kw in user_lower for kw in done_keywords) and len(user_lower) < 30:
            return (
                f"好的好的，那就这么定了！明天晚六点，23个人，包间。"
                f"麻烦帮我准备几个儿童座椅。"
                f"那到时候见，谢谢您啊！"
                f" {CONVERSATION_COMPLETE_MARKER}"
            )

        # 餐厅工作人员提问（检测问号）
        question_keywords = ["吗", "？", "?"]
        if any(kw in user_lower for kw in question_keywords):
            return (
                f"嗯对的，我们明天周一晚上六点到，"
                f"23个人团建聚餐。菜的话家常菜就行，"
                f"口味不要太辣，因为有小孩。"
                f"您看包间能坐下吗？"
            )

        # 默认回复：自然地接话 + 补充一个需求
        return (
            f"好的收到。对了，还想确认一下，"
            f"咱们包间有没有最低消费呀？"
            f"我们预算大概人均一百到四百的样子。"
        )


# ============================================================================
# 快捷函数
# ============================================================================


def create_session(
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> ChatSession:
    """快捷创建 ChatSession。

    Args:
        api_key: API 密钥，为空使用默认 DeepSeek key。
        base_url: API 地址，为空使用默认 DeepSeek 地址。
        model: 模型名称，为空使用默认 deepseek-v4-pro。

    Returns:
        ChatSession 实例。
    """
    return ChatSession(api_key=api_key, base_url=base_url, model=model)
