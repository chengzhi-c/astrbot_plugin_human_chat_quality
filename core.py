from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from .protocols import LLMResponseProtocol, MessageEventProtocol, ProviderRequestProtocol
from .quality_rules import (
    MAX_RUNTIME_HINT_CHARS,
    MIN_RUNTIME_HINT_CHARS,
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    build_runtime_hint,
    rewrite_context_injections,
    rewrite_stable_rules,
)
from .runtime_state import RuntimeStateStore, is_session_disabled, unified_origin

logger = logging.getLogger(__name__)


@dataclass
class QualityStats:
    """质量层累计统计（进程内，不持久化）。"""

    # 注入统计
    total_injections: int = 0
    stable_rules_injected: int = 0
    runtime_hints_injected: int = 0

    # 信号统计
    repeated_openers_avoided: int = 0
    cliche_hits: dict[str, int] = field(default_factory=dict)

    # 清理统计
    legacy_blocks_removed: int = 0
    stale_hints_removed: int = 0

    def record_cliche_hit(self, cliche: str) -> None:
        """记录信号命中。"""
        self.cliche_hits[cliche] = self.cliche_hits.get(cliche, 0) + 1

    def top_cliches(self, limit: int = 5) -> list[tuple[str, int]]:
        """返回命中最多的信号（降序）。"""
        return sorted(self.cliche_hits.items(), key=lambda x: x[1], reverse=True)[:limit]


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    return []


@dataclass(frozen=True)
class AppConfig:
    """Construct-once configuration snapshot."""

    enabled: bool = True
    inject_stable_rules: bool = True
    inject_runtime_state: bool = True
    debug_log: bool = False
    max_runtime_hint_chars: int = MAX_RUNTIME_HINT_CHARS
    state_retention_days: int = 14
    recent_reply_window: int = 8
    custom_cliches: tuple[str, ...] = ()
    disabled_sessions: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: Any) -> AppConfig:
        raw = config if config is not None else {}

        def get(key: str, default: Any) -> Any:
            return raw.get(key, default)

        return cls(
            enabled=_parse_bool(get("enabled", True), True),
            inject_stable_rules=_parse_bool(get("inject_stable_rules", True), True),
            inject_runtime_state=_parse_bool(get("inject_runtime_state", True), True),
            debug_log=_parse_bool(get("debug_log", False), False),
            max_runtime_hint_chars=_parse_int(
                get("max_runtime_hint_chars", MAX_RUNTIME_HINT_CHARS),
                MAX_RUNTIME_HINT_CHARS,
                MIN_RUNTIME_HINT_CHARS,
                MAX_RUNTIME_HINT_CHARS,
            ),
            state_retention_days=_parse_int(get("state_retention_days", 14), 14, 1, 365),
            recent_reply_window=_parse_int(get("recent_reply_window", 8), 8, 3, 50),
            custom_cliches=tuple(_parse_list(get("custom_cliches", []))),
            disabled_sessions=frozenset(item.lower() for item in _parse_list(get("disabled_sessions", []))),
        )


def extract_response_text(resp: LLMResponseProtocol) -> str:
    completion = getattr(resp, "completion_text", None)
    if isinstance(completion, str):
        completion_text = completion.strip()
        if completion_text:
            return completion_text
    return " ".join(part for part in (_extract_text_from_part(item) for item in _normalize_chain(resp)) if part).strip()


def _normalize_chain(resp: LLMResponseProtocol) -> list[Any]:
    chain = getattr(resp, "result_chain", None) or getattr(resp, "message", None) or []
    chain_items = getattr(chain, "chain", None)
    if chain_items is not None:
        chain = chain_items
    return chain if isinstance(chain, list) else [chain]


def _extract_text_from_part(item: Any) -> str:
    if item is None:
        return ""
    role = getattr(item, "role", None)
    if role is not None and role != "assistant":
        return ""
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(item, "content", None)
    return content if isinstance(content, str) else ""


class HumanChatQualityCore:
    """Host-independent request, response, and session behavior."""

    def __init__(
        self,
        config: Any,
        store: RuntimeStateStore,
        text_part_factory: Any | None = None,
    ) -> None:
        self.cfg = config if isinstance(config, AppConfig) else AppConfig.from_config(config)
        self.store = store
        self.text_part_factory = text_part_factory
        self.injection_count = 0
        self.stats = QualityStats()

    async def on_llm_request(self, event: MessageEventProtocol, req: ProviderRequestProtocol) -> None:
        session_id = unified_origin(event)
        effective_active = bool(session_id) and self._is_effectively_active(session_id, event)
        injected_hint = ""
        removed_stale = False
        avoid_openers: list[str] | None = None

        hint = ""
        if effective_active and self.cfg.inject_runtime_state:
            state = self.store.get(session_id)
            avoid_openers = state.avoid_openers
            hint = build_runtime_hint(state, max_chars=self.cfg.max_runtime_hint_chars)

        context_result = rewrite_context_injections(req, hint or None)
        removed_stale = context_result.stable_removed or context_result.runtime_removed
        ambiguous_kept = context_result.runtime_ambiguous
        if context_result.runtime_replaced:
            injected_hint = hint
        elif hint and not context_result.runtime_satisfied and not context_result.runtime_ambiguous:
            if append_temp_text_part(req, hint, self.text_part_factory, marker=RUNTIME_HINT_MARKER):
                injected_hint = hint

        before = getattr(req, "system_prompt", "") or ""
        stable_result = rewrite_stable_rules(before, enabled=effective_active and self.cfg.inject_stable_rules)
        if stable_result.text != before:
            req.system_prompt = stable_result.text
        removed_stale = removed_stale or stable_result.removed
        ambiguous_kept = ambiguous_kept or stable_result.ambiguous

        # 统计收集
        if stable_result.injected:
            self.stats.stable_rules_injected += 1
        if injected_hint:
            self.stats.runtime_hints_injected += 1
        if stable_result.injected or injected_hint:
            self.injection_count += 1
            self.stats.total_injections += 1
        if stable_result.removed:
            self.stats.legacy_blocks_removed += 1
        if context_result.runtime_removed:
            self.stats.stale_hints_removed += 1

        if (stable_result.injected or injected_hint or removed_stale or ambiguous_kept) and self.cfg.debug_log:
            self._log_injection(
                session_id or "<unknown>",
                stable_result.injected,
                injected_hint,
                removed_stale,
                ambiguous_kept,
                avoid_openers,
            )

    def _log_injection(
        self,
        session_id: str,
        injected_rules: bool,
        injected_hint: str,
        removed_stale: bool,
        ambiguous_kept: bool,
        avoid_openers: list[str] | None,
    ) -> None:
        logger.debug("injection rewrite for %s", session_id)
        if injected_rules:
            logger.debug("stable rules injected into system_prompt (marker=%s)", STABLE_RULE_MARKER)
        if injected_hint:
            logger.debug("runtime hint injected: %s; avoid_openers=%s", injected_hint, avoid_openers)
        if removed_stale:
            logger.debug("stale owned injection removed for %s", session_id)
        if ambiguous_kept:
            logger.debug("ambiguous owned marker kept for %s", session_id)

    async def on_llm_response(self, event: MessageEventProtocol, resp: LLMResponseProtocol) -> None:
        session_id = unified_origin(event)
        if not session_id or not self._is_effectively_active(session_id, event):
            return
        text = extract_response_text(resp)
        if not text:
            return

        # 记录响应前先检测信号（用于统计）
        from .signal_detectors import detect_cliches
        cliches = detect_cliches(text, self.store.custom_cliches)
        for cliche in cliches:
            self.stats.record_cliche_hit(cliche)

        # 记录到状态存储
        await self.store.record_response(session_id, text)

        # 统计避用项数量
        state = self.store.get(session_id)
        if state.avoid_openers:
            self.stats.repeated_openers_avoided += len(state.avoid_openers)

        if self.cfg.debug_log:
            logger.debug("response recorded for %s: %s", session_id, state.avoid_openers)

    async def set_session_enabled(self, session_id: str, enabled: bool) -> bool:
        return await self.store.set_enabled(session_id, enabled)

    async def reset_session(self, session_id: str) -> bool:
        return await self.store.reset(session_id)

    def status_text(self, session_id: str, event: MessageEventProtocol | None = None) -> str:
        persistence = "待重试" if self.store.has_pending_save else "正常"
        if not self._is_effectively_active(session_id, event):
            return f"Human Chat Quality 状态：\n- 当前会话：关闭\n- 无运行时状态\n- 状态持久化：{persistence}"
        state = self.store.get(session_id)
        avoid = "、".join(state.avoid_openers) if state.avoid_openers else "无（尚未形成重复或套话信号）"
        lines = [
            "Human Chat Quality 状态：",
            "- 当前会话：启用",
            f"- 稳定规则：{'启用' if self.cfg.inject_stable_rules else '关闭'}（system_prompt）",
            f"- 运行时提示：{'启用' if self.cfg.inject_runtime_state else '关闭'}",
            f"- 下一轮避用：{avoid}",
        ]
        if state.avoid_openers and self.cfg.inject_runtime_state:
            lines.append("- 下一轮请求会带上动态提醒")
        lines.append(f"- 自启动以来累计注入：{self.injection_count} 次")
        lines.append(f"- 状态持久化：{persistence}")
        return "\n".join(lines)

    def _is_active(self, session_id: str) -> bool:
        return self.cfg.enabled and self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: MessageEventProtocol | None = None) -> bool:
        return self._is_active(session_id) and not is_session_disabled(self.cfg.disabled_sessions, session_id, event)
