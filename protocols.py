"""Type protocols for host-independent contracts.

定义插件核心接口的 Protocol，用于类型标注而不引入运行时依赖。
"""

from __future__ import annotations

from typing import Any, Protocol


class TextPartProtocol(Protocol):
    """临时文本 part 契约（对应 astrbot.core.agent.message.TextPart）。"""

    text: str


class TextPartFactoryProtocol(Protocol):
    """TextPart 工厂函数契约。"""

    def __call__(self, *, text: str) -> TextPartProtocol: ...


class ProviderRequestProtocol(Protocol):
    """LLM 请求对象契约（对应 astrbot.api.provider.ProviderRequest）。"""

    system_prompt: str | None
    contexts: list[dict[str, Any]] | None
    extra_user_content_parts: list[Any] | None


class LLMResponseProtocol(Protocol):
    """LLM 响应对象契约（对应 astrbot.api.provider.LLMResponse）。"""

    completion_text: str | None
    result_chain: Any | None


class MessageEventProtocol(Protocol):
    """消息事件契约（对应 astrbot.api.event.AstrMessageEvent）。"""

    unified_msg_origin: str

    def get_group_id(self) -> str | None: ...
