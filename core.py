from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any

from .constants import (
    MAX_RUNTIME_HINT_CHARS,
    MIN_RUNTIME_HINT_CHARS,
    PENDING_HINT_MAX_PER_SESSION,
    PENDING_HINT_TTL_SECONDS,
)
from .protocols import LLMResponseProtocol, MessageEventProtocol, ProviderRequestProtocol
from .quality_rules import (
    ContextRewriteResult,
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    StableRewriteResult,
    append_temp_text_part,
    build_runtime_hint,
    runtime_hint_items,
    rewrite_context_injections,
    rewrite_stable_rules,
)
from .runtime_state import RuntimeStateStore, is_session_disabled, unified_origin
from .signal_detectors import detect_cliches

logger = logging.getLogger(__name__)


@dataclass
class QualityStats:
    """质量层累计统计（进程内，不持久化）。"""

    # 注入统计
    total_injections: int = 0
    stable_rules_injected: int = 0
    runtime_hints_injected: int = 0

    # 信号统计
    avoid_openers_seen: int = 0
    runtime_hint_missed: int = 0
    cliche_hits: dict[str, int] = field(default_factory=dict)

    # 清理统计
    legacy_blocks_removed: int = 0
    stale_hints_removed: int = 0

    def record_cliche_hit(self, cliche: str) -> None:
        """记录信号命中。"""
        self.cliche_hits[cliche] = self.cliche_hits.get(cliche, 0) + 1

    def top_cliches(self, limit: int = 5) -> list[tuple[str, int]]:
        """返回命中最多的信号（降序）。"""
        return sorted(self.cliche_hits.items(), key=lambda x: (-x[1], x[0]))[:limit]


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


def _parse_custom_cliches(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value]
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines()]
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
        get = (config if config is not None else {}).get

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
            custom_cliches=tuple(_parse_custom_cliches(get("custom_cliches", []))),
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


def _event_text(event: MessageEventProtocol | None) -> str:
    if event is not None:
        for attr in ("get_message_str", "message_str", "message", "text"):
            try:
                v = getattr(event, attr, None)
                if callable(v):
                    v = v()
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:
                continue
    return ""


def _is_formal_writing_request(event: MessageEventProtocol | None) -> bool:
    text = _event_text(event)
    if not text:
        return False
    action = re.search(r"写|撰写|起草|拟定|润色|改(?:写|成)|生成|翻译|输出", text)
    artifact = re.search(
        r"论文|摘要|公文|演讲稿|营销文案|法律(?:文书|声明)|合同|会议纪要|(?:正式)?道歉声明|正式声明|新闻稿|采购申请|通知|研究计划|求职邮件",
        text,
    )
    return bool(action and artifact)


def _drop_expired_hints(pending: deque[tuple[float, tuple[str, ...]]], now: float) -> None:
    while pending and now - pending[0][0] > PENDING_HINT_TTL_SECONDS:
        pending.popleft()


def _contains_hint_item(text: str, items: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    for item in items:
        candidate = item.casefold()
        if candidate.isascii():
            if re.search(rf"\b{re.escape(candidate)}\b", lowered):
                return True
        elif candidate in lowered:
            return True
    return False


class HumanChatQualityCore:
    """Host-independent request, response, and session behavior."""

    def __init__(
        self,
        config: AppConfig,
        store: RuntimeStateStore,
        text_part_factory: Any | None = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.text_part_factory = text_part_factory
        self.stats = QualityStats()
        self._pending_hints: dict[str, deque[tuple[float, tuple[str, ...]]]] = {}

    async def on_llm_request(self, event: MessageEventProtocol, req: ProviderRequestProtocol) -> None:
        session_id = unified_origin(event)
        effective_active = (
            bool(session_id)
            and not _is_formal_writing_request(event)
            and self._is_effectively_active(session_id, event)
        )
        injected_hint = ""
        avoid_openers: list[str] | None = None

        hint = ""
        if effective_active and self.cfg.inject_runtime_state and self.text_part_factory is not None:
            state = self.store.get(session_id)
            avoid_openers = state.avoid_openers
            hint = build_runtime_hint(state.avoid_openers, max_chars=self.cfg.max_runtime_hint_chars)

        context_result = rewrite_context_injections(req, hint or None)
        if (
            hint
            and not context_result.runtime_satisfied
            and not context_result.runtime_ambiguous
            and append_temp_text_part(req, hint, self.text_part_factory, marker=RUNTIME_HINT_MARKER)
        ):
            injected_hint = hint

        before = getattr(req, "system_prompt", "") or ""
        stable_result = rewrite_stable_rules(before, enabled=effective_active and self.cfg.inject_stable_rules)
        if stable_result.text != before:
            req.system_prompt = stable_result.text

        # 统计收集
        if stable_result.injected:
            self.stats.stable_rules_injected += 1
        if injected_hint:
            self.stats.runtime_hints_injected += 1
        if stable_result.injected or injected_hint:
            self.stats.total_injections += 1
        if session_id:
            pending = self._pending_hints.get(session_id)
            if pending is None or pending.maxlen != PENDING_HINT_MAX_PER_SESSION:
                pending = deque(pending or (), maxlen=PENDING_HINT_MAX_PER_SESSION)
                self._pending_hints[session_id] = pending
            now = time.monotonic()
            _drop_expired_hints(pending, now)
            pending.append((now, runtime_hint_items(injected_hint)))
        self.stats.legacy_blocks_removed += stable_result.removed + context_result.stable_removed
        self.stats.stale_hints_removed += context_result.runtime_removed

        if self.cfg.debug_log and (
            stable_result.injected
            or injected_hint
            or stable_result.removed
            or stable_result.ambiguous
            or context_result.stable_removed
            or context_result.runtime_removed
            or context_result.runtime_ambiguous
        ):
            self._log_injection(
                session_id or "<unknown>",
                stable_result,
                context_result,
                injected_hint,
                avoid_openers,
            )

    def _log_injection(
        self,
        session_id: str,
        stable_result: StableRewriteResult,
        context_result: ContextRewriteResult,
        injected_hint: str,
        avoid_openers: list[str] | None,
    ) -> None:
        logger.debug("injection rewrite for %s", session_id)
        if stable_result.injected:
            logger.debug("stable rules injected into system_prompt (marker=%s)", STABLE_RULE_MARKER)
        if injected_hint:
            logger.debug("runtime hint injected: %s; avoid_openers=%s", injected_hint, avoid_openers)
        if stable_result.removed or context_result.stable_removed or context_result.runtime_removed:
            logger.debug("stale owned injection removed for %s", session_id)
        if stable_result.ambiguous or context_result.runtime_ambiguous:
            logger.debug("ambiguous owned marker kept for %s", session_id)

    async def on_llm_response(self, event: MessageEventProtocol, resp: LLMResponseProtocol) -> None:
        session_id = unified_origin(event)
        pending = self._pending_hints.get(session_id)
        if pending:
            _drop_expired_hints(pending, time.monotonic())
        hinted_items = pending.popleft()[1] if pending else ()
        if pending is not None and not pending:
            self._pending_hints.pop(session_id, None)
        if not session_id or _is_formal_writing_request(event) or not self._is_effectively_active(session_id, event):
            return
        text = extract_response_text(resp)
        if not text:
            return

        # 效果观测：上一轮带提醒的请求，若本轮回复仍出现避用项，计一次忽略（英文用词边界避免子串误伤）
        if hinted_items and _contains_hint_item(text, hinted_items):
            self.stats.runtime_hint_missed += 1

        # 记录响应前先检测信号（用于统计）
        cliches = detect_cliches(text, self.store.custom_cliches)
        for cliche in cliches:
            self.stats.record_cliche_hit(cliche)

        # 记录前快照，用于 delta 统计（避免重复清单重复计数膨胀）
        before_avoid = set(self.store.get(session_id).avoid_openers)
        # 记录到状态存储
        await self.store.record_response(session_id, text, tuple(cliches))

        # 统计避用项数量：仅计新增项（delta），避免同一清单停留多轮重复膨胀
        state = self.store.get(session_id)
        if state.avoid_openers:
            new_items = set(state.avoid_openers) - before_avoid
            self.stats.avoid_openers_seen += len(new_items)

        if self.cfg.debug_log:
            logger.debug("response recorded for %s: %s", session_id, state.avoid_openers)

    async def set_session_enabled(self, session_id: str, enabled: bool) -> bool:
        return await self.store.set_enabled(session_id, enabled)

    async def reset_session(self, session_id: str) -> bool:
        return await self.store.reset(session_id)

    def status_text(self, session_id: str, event: MessageEventProtocol | None = None) -> str:
        persistence = "待重试" if self.store.has_pending_save else "正常"
        reasons: list[str] = []
        if not self.cfg.enabled:
            reasons.append("- 全局配置：关闭")
        if not self.store.is_enabled(session_id):
            reasons.append("- 当前会话：已通过 /humanq off 关闭")
        if is_session_disabled(self.cfg.disabled_sessions, session_id, event):
            reasons.append("- 配置静态禁用：当前会话命中禁用列表")
        if reasons:
            return "\n".join(["Human Chat Quality 状态：", *reasons, "- 无运行时状态", f"- 状态持久化：{persistence}"])
        state = self.store.get(session_id)
        avoid = "、".join(state.avoid_openers) if state.avoid_openers else "无（尚未形成重复或套话信号）"
        lines = [
            "Human Chat Quality 状态：",
            "- 当前会话：启用",
            f"- 稳定规则：{'启用' if self.cfg.inject_stable_rules else '配置关闭'}（system_prompt）",
            f"- 下一轮避用：{avoid}",
        ]
        if not self.cfg.inject_runtime_state:
            lines.insert(3, "- 运行时提示：配置关闭")
        elif self.text_part_factory is None:
            lines.insert(3, "- 运行时提示：已配置，但宿主临时文本部件不可用")
        else:
            lines.insert(3, "- 运行时提示：启用")
        if self.store.custom_cliches_ignored:
            ignored = dict(self.store.custom_cliches_ignored_reasons)
            details = "、".join(
                f"{label} {ignored[reason]} 项"
                for reason, label in (("empty", "空值"), ("duplicate", "重复"), ("too_long", "过长"))
                if ignored.get(reason)
            )
            lines.append(f"- 配置忽略：{self.store.custom_cliches_ignored} 项（{details}）")
        if state.avoid_openers and self.cfg.inject_runtime_state and self.text_part_factory is not None:
            lines.append("- 下一轮请求会带上动态提醒")
        lines.append(f"- 自启动以来累计注入：{self.stats.total_injections} 次")
        lines.append(f"- 状态持久化：{persistence}")
        return "\n".join(lines)

    def _is_active(self, session_id: str) -> bool:
        return self.cfg.enabled and self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: MessageEventProtocol | None = None) -> bool:
        return self._is_active(session_id) and not is_session_disabled(self.cfg.disabled_sessions, session_id, event)
