from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.event.filter import PermissionType, permission_type
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context, Star, StarTools, register
except Exception:  # pragma: no cover
    AstrBotConfig = Any  # type: ignore
    AstrMessageEvent = Any  # type: ignore
    LLMResponse = Any  # type: ignore
    ProviderRequest = Any  # type: ignore
    Context = Any  # type: ignore

    class PermissionType:  # type: ignore
        ADMIN = "admin"

    def permission_type(permission: Any):  # type: ignore
        def decorator(func: Any) -> Any:
            setattr(func, "_permission_type", permission)
            return func

        return decorator

    class Star:  # type: ignore
        def __init__(self, context: Any) -> None:
            self.context = context

    class StarTools:  # type: ignore
        @staticmethod
        def get_data_dir(*_args: Any, **_kwargs: Any) -> str:
            return "."

    class _Logger:
        def info(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def debug(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def error(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    logger = _Logger()  # type: ignore

    class _Filter:
        def on_llm_request(self, *_args: Any, **_kwargs: Any):
            return lambda func: func

        def on_llm_response(self, *_args: Any, **_kwargs: Any):
            return lambda func: func

        def command_group(self, *_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                def command_wrapper(*_c_args: Any, **_c_kwargs: Any):
                    return lambda nested: nested

                func.command = command_wrapper
                return func

            return decorator

    filter = _Filter()  # type: ignore

    def register(*_args: Any, **_kwargs: Any):
        return lambda cls: cls

try:
    from .quality_rules import (
        RUNTIME_HINT_MARKER,
        STABLE_RULE_MARKER,
        build_runtime_hint,
        inject_stable_rules,
        make_text_part,
    )
    from .runtime_state import RuntimeStateStore
except ImportError:  # pragma: no cover
    from quality_rules import (
        RUNTIME_HINT_MARKER,
        STABLE_RULE_MARKER,
        build_runtime_hint,
        inject_stable_rules,
        make_text_part,
    )
    from runtime_state import RuntimeStateStore


PLUGIN_ID = "astrbot_plugin_human_chat_quality"
PLUGIN_VERSION = "0.1.0"


def config_get(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(config, key, default)


def config_bool(config: Any, key: str, default: bool) -> bool:
    value = config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启", "enabled"}


def config_int(config: Any, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(config_get(config, key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def config_list(config: Any, key: str) -> list[str]:
    value = config_get(config, key, [])
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    return []


def _extract_response_text(resp: Any) -> str:
    """从 LLMResponse 中提取文本，兼容 completion_text 与 result_chain。"""
    completion = getattr(resp, "completion_text", None)
    if completion:
        return str(completion).strip()
    # 兜底：遍历 result_chain / message chain 中的文本 part
    chain = getattr(resp, "result_chain", None) or getattr(resp, "message", None) or []
    chain_items = getattr(chain, "chain", None)
    if chain_items is not None:
        chain = chain_items
    if not isinstance(chain, list):
        chain = [chain]
    parts: list[str] = []
    for item in chain:
        if item is None:
            continue
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        # 某些 provider 把文本放在 content 字段
        content = getattr(item, "content", None)
        if content is not None:
            parts.append(str(content))
    return " ".join(parts).strip()


class HumanChatQualityCore:
    """核心逻辑：在请求阶段注入规则/状态，在响应阶段更新状态。"""

    def __init__(
        self,
        config: Any,
        store: RuntimeStateStore,
        text_part_factory: Any | None = None,
    ) -> None:
        self.config = config or {}
        self.store = store
        self.text_part_factory = text_part_factory
        self.injection_count = 0

    async def on_llm_request(self, event: Any, req: Any) -> None:
        session_id = self._session_id(event)
        if not self._is_effectively_active(session_id, event):
            return

        # 阶段 1：system_prompt 注入稳定规则
        # 阶段 2：extra_user_content_parts 注入本轮运行时提示
        injected_rules = False
        injected_hint = ""
        if config_bool(self.config, "inject_stable_rules", True):
            before = getattr(req, "system_prompt", "") or ""
            after = inject_stable_rules(before)
            if after != before:
                req.system_prompt = after
                injected_rules = True

        state = self.store.get(session_id)
        if config_bool(self.config, "inject_runtime_state", True):
            hint = build_runtime_hint(
                state,
                max_chars=config_int(self.config, "max_runtime_hint_chars", 600, 80, 3000),
            )
            if hint and self._append_runtime_hint(req, hint):
                injected_hint = hint

        if injected_rules or injected_hint:
            self.injection_count += 1
            if config_bool(self.config, "debug_log", False):
                logger.debug(f"[HumanChatQuality] injected quality hints for {session_id}")
                if injected_rules:
                    logger.debug(f"[HumanChatQuality] stable rules injected (marker={STABLE_RULE_MARKER})")
                if injected_hint:
                    logger.debug(f"[HumanChatQuality] runtime hint injected:\n{injected_hint}")
                logger.debug(
                    f"[HumanChatQuality] runtime state for {session_id}: "
                    f"avoid_openers={state.avoid_openers}"
                )

    async def on_llm_response(self, event: Any, resp: Any) -> None:
        session_id = self._session_id(event)
        if not self._is_effectively_active(session_id, event):
            return
        # 优先读取 completion_text，兼容 result_chain / message 对象
        text = _extract_response_text(resp)
        if not text.strip():
            return
        await self.store.record_response(session_id, text)
        if config_bool(self.config, "debug_log", False):
            state = self.store.get(session_id)
            logger.debug(
                f"[HumanChatQuality] recorded response for {session_id}: "
                f"avoid_openers={state.avoid_openers}"
            )

    async def set_session_enabled(self, session_id: str, enabled: bool) -> None:
        await self.store.set_enabled(session_id, enabled)

    async def reset_session(self, session_id: str) -> None:
        await self.store.reset(session_id)

    def status_text(self, session_id: str, event: Any | None = None) -> str:
        state = self.store.get(session_id)
        status = "启用" if self._is_effectively_active(session_id, event) else "关闭"
        avoid = "、".join(state.avoid_openers) if state.avoid_openers else "无"
        return (
            "Human Chat Quality 状态：\n"
            f"- 当前会话：{status}\n"
            f"- 稳定规则：{'启用' if config_bool(self.config, 'inject_stable_rules', True) else '关闭'}\n"
            f"- 运行时提示：{'启用' if config_bool(self.config, 'inject_runtime_state', True) else '关闭'}\n"
            f"- 本轮运行累计注入：{self.injection_count} 次\n"
            f"- 最近避用开头：{avoid}"
        )

    def _append_runtime_hint(self, req: Any, hint: str) -> bool:
        # 运行时提示以临时 part 形式追加，避免污染长期记忆
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            return False
        if any(hint_part_has_marker(part) for part in parts):
            return False
        parts.append(make_text_part(hint, self.text_part_factory))
        return True

    def _is_active(self, session_id: str) -> bool:
        if not config_bool(self.config, "enabled", True):
            return False
        return self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: Any | None = None) -> bool:
        if not self._is_active(session_id):
            return False
        disabled = set(config_list(self.config, "disabled_sessions"))
        if not disabled:
            return True
        candidates = disabled_match_candidates(event) if event is not None else disabled_match_candidates_from_session(session_id)
        disabled_lower = {item.lower() for item in disabled}
        return not any(candidate.lower() in disabled_lower for candidate in candidates)

    @staticmethod
    def _session_id(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "unknown")


def hint_part_has_marker(part: Any) -> bool:
    return RUNTIME_HINT_MARKER in str(getattr(part, "text", ""))


def disabled_match_candidates(event: Any) -> set[str]:
    session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
    group_id = group_id_from_event(event)
    candidates: set[str] = set()
    if session_id:
        candidates.add(session_id)
    if group_id:
        candidates.add(group_id)
        candidates.add(f"group:{group_id}")
        candidates.add(f"GroupMessage:{group_id}")
        base_group_id = group_id.split("#", 1)[0].strip()
        if base_group_id and base_group_id != group_id:
            candidates.add(base_group_id)
            candidates.add(f"group:{base_group_id}")
            candidates.add(f"GroupMessage:{base_group_id}")
    return {candidate for candidate in candidates if candidate}


def disabled_match_candidates_from_session(session_id: str) -> set[str]:
    candidates: set[str] = set()
    session_id = str(session_id or "").strip()
    if session_id:
        candidates.add(session_id)
    group_id = group_id_from_session_id(session_id)
    if group_id:
        candidates.add(group_id)
        candidates.add(f"group:{group_id}")
        candidates.add(f"GroupMessage:{group_id}")
    return {candidate for candidate in candidates if candidate}


def group_id_from_event(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None and str(value).strip():
                return str(value).strip()
        except Exception:
            pass

    message_obj = getattr(event, "message_obj", None)
    for owner in (event, message_obj):
        if owner is None:
            continue
        for attr in ("group_id", "group"):
            group_id = extract_group_id(getattr(owner, attr, None))
            if group_id:
                return group_id

    return group_id_from_session_id(str(getattr(event, "unified_msg_origin", "") or ""))


def group_id_from_session_id(session_id: str) -> str:
    parts = str(session_id or "").strip().split(":", 2)
    if len(parts) >= 3 and "group" in parts[1].lower():
        return parts[2].strip()
    return ""


def extract_group_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("group_id", "id", "qq", "uin"):
            if value.get(key) is not None and str(value[key]).strip():
                return str(value[key]).strip()
        return ""
    for attr in ("group_id", "id", "qq", "uin"):
        attr_value = getattr(value, attr, None)
        if attr_value is not None and str(attr_value).strip():
            return str(attr_value).strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


@register(
    PLUGIN_ID,
    "Codex",
    "轻量聊天人性化质量层：隐藏去模板腔规则与本轮运行时提示。",
    PLUGIN_VERSION,
)
class HumanChatQualityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self.store = RuntimeStateStore(
            data_dir / "runtime_state.json",
            retention_days=config_int(self.config, "state_retention_days", 14, 1, 365),
            recent_reply_window=config_int(self.config, "recent_reply_window", 8, 1, 50),
        )
        self.core = HumanChatQualityCore(self.config, self.store)

    @filter.on_llm_request(priority=-100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await self.core.on_llm_request(event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        await self.core.on_llm_response(event, resp)

    @filter.command_group("humanq")
    def humanq(self):
        pass

    @humanq.command("status")
    async def humanq_status(self, event: AstrMessageEvent):
        yield event.plain_result(self.core.status_text(event.unified_msg_origin, event))

    @permission_type(PermissionType.ADMIN)
    @humanq.command("on")
    async def humanq_on(self, event: AstrMessageEvent):
        await self.core.set_session_enabled(event.unified_msg_origin, True)
        yield event.plain_result("Human Chat Quality 已启用当前会话。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("off")
    async def humanq_off(self, event: AstrMessageEvent):
        await self.core.set_session_enabled(event.unified_msg_origin, False)
        yield event.plain_result("Human Chat Quality 已关闭当前会话。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("reset")
    async def humanq_reset(self, event: AstrMessageEvent):
        await self.core.reset_session(event.unified_msg_origin)
        yield event.plain_result("Human Chat Quality 已清空当前会话的轻量状态。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("rules")
    async def humanq_rules(self, event: AstrMessageEvent):
        from .quality_rules import build_stable_rules
        yield event.plain_result(build_stable_rules())

    @permission_type(PermissionType.ADMIN)
    @humanq.command("preview")
    async def humanq_preview(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        state = self.core.store.get(session_id)
        hint = build_runtime_hint(
            state,
            max_chars=config_int(self.core.config, "max_runtime_hint_chars", 600, 80, 3000),
        )
        lines = [
            "[Human Chat Quality 预览]",
            f"session_id: {session_id}",
            f"enabled: {self.core._is_effectively_active(session_id, event)}",
            f"avoid_openers: {', '.join(state.avoid_openers) or '无'}",
            "",
            "[本次将注入的运行时提示词]",
            hint or "（无）",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        logger.info(
            f"[HumanChatQuality] terminated, total injections this run: {self.core.injection_count}; marker={STABLE_RULE_MARKER}"
        )
