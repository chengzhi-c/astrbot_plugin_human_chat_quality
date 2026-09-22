from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    DEFAULT_RECENT_REPLY_WINDOW,
    DEFAULT_STATE_RETENTION_DAYS,
    MAX_RECENT_REPLY_WINDOW,
    MAX_RUNTIME_HINT_CHARS,
    MAX_STATE_RETENTION_DAYS,
    MIN_RECENT_REPLY_WINDOW,
    MIN_RUNTIME_HINT_CHARS,
    MIN_STATE_RETENTION_DAYS,
    PENDING_SESSION_CAP,
    YIELD_STICKY_TTL_SECONDS,
)
from .protocols import LLMResponseProtocol, MessageEventProtocol, ProviderRequestProtocol
from .quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    ContextRewriteResult,
    StableRewriteResult,
    append_temp_text_part,
    render_runtime_hint,
    rewrite_context_injections,
    rewrite_stable_rules,
    select_runtime_hint_names,
)
from .runtime_state import RuntimeStateStore, is_session_disabled, unified_origin
from .scene_guard import event_text, is_creative_writing_request, is_formal_writing_request, is_sticky_followup
from .signal_detectors import detect_cliches, signal_priority

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
    cliche_hits: dict[str, int] = field(default_factory=dict)

    # 清理统计
    legacy_blocks_removed: int = 0
    stale_hints_removed: int = 0

    def record_cliche_hit(self, cliche: str) -> None:
        """记录信号命中。"""
        self.cliche_hits[cliche] = self.cliche_hits.get(cliche, 0) + 1

    def record_request(self, stable_injected: bool, hint_injected: bool) -> None:
        """记录一次请求的注入结果。"""
        if stable_injected:
            self.stable_rules_injected += 1
        if hint_injected:
            self.runtime_hints_injected += 1
        if stable_injected or hint_injected:
            self.total_injections += 1

    def record_cleanup(self, stable_removed: int, runtime_removed: int) -> None:
        """记录一次请求清理掉的旧块数量。"""
        self.legacy_blocks_removed += stable_removed
        self.stale_hints_removed += runtime_removed

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


@dataclass(frozen=True)
class AppConfig:
    """Construct-once configuration snapshot."""

    enabled: bool = True
    inject_stable_rules: bool = True
    inject_runtime_state: bool = True
    debug_log: bool = False
    max_runtime_hint_chars: int = MAX_RUNTIME_HINT_CHARS
    state_retention_days: int = DEFAULT_STATE_RETENTION_DAYS
    recent_reply_window: int = DEFAULT_RECENT_REPLY_WINDOW
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
            state_retention_days=_parse_int(
                get("state_retention_days", DEFAULT_STATE_RETENTION_DAYS),
                DEFAULT_STATE_RETENTION_DAYS,
                MIN_STATE_RETENTION_DAYS,
                MAX_STATE_RETENTION_DAYS,
            ),
            recent_reply_window=_parse_int(
                get("recent_reply_window", DEFAULT_RECENT_REPLY_WINDOW),
                DEFAULT_RECENT_REPLY_WINDOW,
                MIN_RECENT_REPLY_WINDOW,
                MAX_RECENT_REPLY_WINDOW,
            ),
            custom_cliches=tuple(_parse_list(get("custom_cliches", []))),
            disabled_sessions=frozenset(item.lower() for item in _parse_list(get("disabled_sessions", []))),
        )


def extract_response_text(resp: LLMResponseProtocol) -> str:
    """宿主响应对象形状随版本变化，逐属性探测是有意的。"""
    completion = getattr(resp, "completion_text", None)
    if isinstance(completion, str) and completion.strip():
        return completion.strip()
    chain = getattr(resp, "result_chain", None) or getattr(resp, "message", None) or ()
    items = getattr(chain, "chain", chain)
    if not isinstance(items, (list, tuple)):
        items = (items,)
    parts: list[str] = []
    for item in items:
        if item is None or getattr(item, "role", "assistant") != "assistant":
            continue
        val = getattr(item, "text", None)
        if not isinstance(val, str):
            val = getattr(item, "content", "")
        if isinstance(val, str) and val:
            parts.append(val)
    return " ".join(parts).strip()


_YIELD_REASONS = {
    "formal": "- 正式写作场景让位（不注入对话层约束）",
    "creative": "- 创作场景让位（不注入对话层约束）",
}


def _yield_kind(event: MessageEventProtocol | None) -> str | None:
    if is_formal_writing_request(event):
        return "formal"
    if is_creative_writing_request(event):
        return "creative"
    return None


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
        self._pending_yield: dict[str, tuple[float, str]] = {}

    async def on_llm_request(self, event: MessageEventProtocol, req: ProviderRequestProtocol) -> None:
        session_id = unified_origin(event)
        effective_active = (
            bool(session_id)
            and not self._yield_reason(session_id, event, update=True)
            and self._is_effectively_active(session_id, event)
        )
        injected_hint = ""
        avoid_openers: list[str] | None = None

        hint = ""
        selected_names: tuple[str, ...] = ()
        if effective_active and self.cfg.inject_runtime_state and self.text_part_factory is not None:
            state = self.store.get(session_id)
            avoid_openers = state.avoid_openers
            # 危害排序：可靠性损害信号（档位 1）优先装入提示，其余按原顺序
            avoid_sorted = [
                item for _, item in sorted(enumerate(avoid_openers), key=lambda p: (signal_priority(p[1]), p[0]))
            ]
            selected_names = tuple(select_runtime_hint_names(avoid_sorted, self.cfg.max_runtime_hint_chars))
            hint = render_runtime_hint(selected_names)

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
        if stable_result.injected or injected_hint:
            self.stats.record_request(stable_result.injected, bool(injected_hint))
        self.stats.record_cleanup(stable_result.removed + context_result.stable_removed, context_result.runtime_removed)

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
        if (
            not session_id
            # 回复正文不参与让位分类，避免模型复述体裁词改写本轮请求写入的粘性原因。
            or self._yield_reason(session_id, event, update=False)
            or not self._is_effectively_active(session_id, event)
        ):
            return
        text = extract_response_text(resp)
        if not text:
            return

        cliches = detect_cliches(text, self.store.custom_cliches)

        for cliche in cliches:
            self.stats.record_cliche_hit(cliche)

        # 新增避用项计数由 store 合并时直接给出，避免调用侧再做前后快照差分
        new_avoid = await self.store.record_response(session_id, text, tuple(cliches))
        self.stats.avoid_openers_seen += new_avoid

        if self.cfg.debug_log:
            logger.debug("response recorded for %s: +%d avoid items", session_id, new_avoid)

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
        yield_reason = self._yield_reason(session_id, event)
        if yield_reason:
            reasons.append(yield_reason)
        if reasons:
            return "\n".join(["Human Chat Quality 状态：", *reasons, "- 无运行时状态", f"- 状态持久化：{persistence}"])
        state = self.store.get(session_id)
        avoid = "、".join(state.avoid_openers) if state.avoid_openers else "无（尚未形成重复或套话信号）"
        if not self.cfg.inject_runtime_state:
            runtime_line = "- 运行时提示：配置关闭"
        elif self.text_part_factory is None:
            runtime_line = "- 运行时提示：已配置，但宿主临时文本部件不可用"
        else:
            runtime_line = "- 运行时提示：启用"
        lines = [
            "Human Chat Quality 状态：",
            "- 当前会话：启用",
            f"- 稳定规则：{'启用' if self.cfg.inject_stable_rules else '配置关闭'}（system_prompt）",
            runtime_line,
            f"- 下一轮避用：{avoid}",
        ]
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

    def _yield_reason(self, session_id: str, event: MessageEventProtocol | None, *, update: bool = False) -> str | None:
        kind = _yield_kind(event)
        now = time.monotonic()
        if kind:
            if update and session_id:
                self._pending_yield[session_id] = (now, kind)
                self._evict_yield_if_needed()
            return _YIELD_REASONS[kind]
        sticky = self._pending_yield.get(session_id) if session_id else None
        if sticky and now - sticky[0] <= YIELD_STICKY_TTL_SECONDS and is_sticky_followup(event_text(event)):
            if update:
                self._pending_yield[session_id] = (now, sticky[1])
                self._evict_yield_if_needed()
            return _YIELD_REASONS[sticky[1]]
        if update and session_id:
            self._pending_yield.pop(session_id, None)
        return None

    def _evict_yield_if_needed(self) -> None:
        # 有界淘汰：超 cap 逐出最旧会话；yield 被逐最坏多注一次（保守方向）
        while len(self._pending_yield) > PENDING_SESSION_CAP:
            self._pending_yield.pop(next(iter(self._pending_yield)))

    def _is_active(self, session_id: str) -> bool:
        return self.cfg.enabled and self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: MessageEventProtocol | None = None) -> bool:
        return self._is_active(session_id) and not is_session_disabled(self.cfg.disabled_sessions, session_id, event)
