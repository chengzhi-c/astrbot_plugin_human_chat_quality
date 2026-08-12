from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    build_runtime_hint,
    build_stable_rules,
    rewrite_context_injections,
    rewrite_stable_rules,
)
from .runtime_state import RuntimeStateStore, is_session_disabled, unified_origin


PLUGIN_ID = "astrbot_plugin_human_chat_quality"


def _version_from_lines(lines: list[str]) -> str:
    """纯函数：从 metadata.yaml 行文本提取 version（可测）。"""
    for line in lines:
        text = line.strip()
        if text.startswith("version:"):
            value = text.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return "0.0.0"


def _read_metadata_version() -> str:
    try:
        return _version_from_lines(Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8").splitlines())
    except OSError:
        return "0.0.0"


PLUGIN_VERSION = _read_metadata_version()


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
    """构造期一次性解析的配置快照（README：修改配置需重载插件生效）。"""

    enabled: bool = True
    inject_stable_rules: bool = True
    inject_runtime_state: bool = True
    debug_log: bool = False
    max_runtime_hint_chars: int = 600
    state_retention_days: int = 14
    recent_reply_window: int = 8
    custom_cliches: tuple[str, ...] = ()
    disabled_sessions: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: Any) -> AppConfig:
        """唯一解析点：dict 兼容读取（AstrBotConfig 是 dict 子类，MRO 实测确认）。"""
        raw = config if config is not None else {}

        def get(key: str, default: Any) -> Any:
            return raw.get(key, default)

        return cls(
            enabled=_parse_bool(get("enabled", True), True),
            inject_stable_rules=_parse_bool(get("inject_stable_rules", True), True),
            inject_runtime_state=_parse_bool(get("inject_runtime_state", True), True),
            debug_log=_parse_bool(get("debug_log", False), False),
            max_runtime_hint_chars=_parse_int(get("max_runtime_hint_chars", 600), 600, 80, 3000),
            state_retention_days=_parse_int(get("state_retention_days", 14), 14, 1, 365),
            recent_reply_window=_parse_int(get("recent_reply_window", 8), 8, 3, 50),
            custom_cliches=tuple(_parse_list(get("custom_cliches", []))),
            disabled_sessions=frozenset(item.lower() for item in _parse_list(get("disabled_sessions", []))),
        )


@lru_cache(maxsize=1)
def _probe_text_part_cls() -> Any | None:
    """探测 TextPart 类（进程内首次调用后缓存，热重载=新函数对象=缓存自然清空）。"""
    try:
        from astrbot.core.agent.message import TextPart

        return TextPart
    except Exception as e:
        if logger is not None:
            logger.warning(f"[HumanChatQuality] TextPart unavailable, temp extra injection disabled: {e}")
        return None


def _extract_response_text(resp: Any) -> str:
    """从 LLMResponse 中提取文本，兼容 completion_text 与 result_chain。"""
    completion = getattr(resp, "completion_text", None)
    if isinstance(completion, str):
        completion_text = completion.strip()
        if completion_text:
            return completion_text
    # 兜底：遍历 result_chain / message chain 中的文本 part
    return " ".join(part for part in (_extract_text_from_part(item) for item in _normalize_chain(resp)) if part).strip()


def _normalize_chain(resp: Any) -> list[Any]:
    """提取 result_chain / message chain 并归一化为 list。"""
    chain = getattr(resp, "result_chain", None) or getattr(resp, "message", None) or []
    chain_items = getattr(chain, "chain", None)
    if chain_items is not None:
        chain = chain_items
    if not isinstance(chain, list):
        chain = [chain]
    return chain


def _extract_text_from_part(item: Any) -> str:
    """从单个 part 提取文本；兜底路径只吸收模型输出，明确非 assistant 的 part 跳过。"""
    if item is None:
        return ""
    role = getattr(item, "role", None)
    if role is not None and role != "assistant":
        return ""
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    # 某些 provider 把文本放在 content 字段（仅接受 str，结构化内容不进入记录）
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content
    return ""


class HumanChatQualityCore:
    """核心逻辑：在请求阶段注入规则/状态，在响应阶段更新状态。"""

    def __init__(
        self,
        config: Any,
        store: RuntimeStateStore,
        text_part_factory: Any | None = None,
    ) -> None:
        # 接受 AppConfig 或原始 config dict（后者就地解析，夹具兼容）
        self.cfg = config if isinstance(config, AppConfig) else AppConfig.from_config(config)
        self.store = store
        # factory 优先；缺失时经 _probe_text_part_cls 探测宿主 TextPart（进程级缓存，热重载即重探）
        self.text_part_factory = text_part_factory or _probe_text_part_cls()
        self.injection_count = 0

    async def on_llm_request(self, event: Any, req: Any) -> None:
        session_id = unified_origin(event)
        if not session_id:
            # 无来源事件不参与状态管理，避免全部挤进同一会话互相污染
            return
        if not self._is_effectively_active(session_id, event):
            return

        injected_rules = False
        injected_hint = ""
        removed_stale = False
        avoid_openers: list[str] | None = None

        hint = ""
        if self.cfg.inject_runtime_state:
            state = self.store.get(session_id)
            avoid_openers = state.avoid_openers
            hint = build_runtime_hint(state, max_chars=self.cfg.max_runtime_hint_chars)

        context_result = rewrite_context_injections(req, hint or None)
        removed_stale = context_result.stable_removed or context_result.runtime_removed
        if context_result.runtime_replaced:
            injected_hint = hint
        elif hint and not context_result.runtime_satisfied and not context_result.runtime_ambiguous:
            if append_temp_text_part(req, hint, self.text_part_factory, marker=RUNTIME_HINT_MARKER):
                injected_hint = hint

        before = getattr(req, "system_prompt", "") or ""
        stable_result = rewrite_stable_rules(before, enabled=self.cfg.inject_stable_rules)
        if stable_result.text != before:
            req.system_prompt = stable_result.text
        injected_rules = stable_result.injected
        removed_stale = removed_stale or stable_result.removed

        if injected_rules or injected_hint:
            self.injection_count += 1
        if (injected_rules or injected_hint or removed_stale) and self.cfg.debug_log:
            self._log_injection(session_id, injected_rules, injected_hint, removed_stale, avoid_openers)

    def _log_injection(
        self,
        session_id: str,
        injected_rules: bool,
        injected_hint: str,
        removed_stale: bool,
        avoid_openers: list[str] | None,
    ) -> None:
        logger.debug(f"[HumanChatQuality] injected quality hints for {session_id}")
        if injected_rules:
            logger.debug(f"[HumanChatQuality] stable rules injected into system_prompt (marker={STABLE_RULE_MARKER})")
        if injected_hint:
            logger.debug(f"[HumanChatQuality] runtime hint injected:\n{injected_hint}")
            logger.debug(f"[HumanChatQuality] runtime state for {session_id}: avoid_openers={avoid_openers}")
        if removed_stale:
            logger.debug(f"[HumanChatQuality] stale injection block removed from contexts for {session_id}")

    async def on_llm_response(self, event: Any, resp: Any) -> None:
        session_id = unified_origin(event)
        if not session_id:
            return
        if not self._is_effectively_active(session_id, event):
            return
        # 优先读取 completion_text，兼容 result_chain / message 对象
        text = _extract_response_text(resp)
        if not text:
            return
        await self.store.record_response(session_id, text)
        if self.cfg.debug_log:
            state = self.store.get(session_id)
            logger.debug(f"[HumanChatQuality] recorded response for {session_id}: avoid_openers={state.avoid_openers}")

    async def set_session_enabled(self, session_id: str, enabled: bool) -> None:
        await self.store.set_enabled(session_id, enabled)

    async def reset_session(self, session_id: str) -> None:
        await self.store.reset(session_id)

    def status_text(self, session_id: str, event: Any | None = None) -> str:
        if not self._is_effectively_active(session_id, event):
            # 非 active：不展示历史重复开头，避免"关闭 + 最近重复开头：某词"的误导组合
            return "Human Chat Quality 状态：\n- 当前会话：关闭\n- 无运行时状态"
        state = self.store.get(session_id)
        avoid = "、".join(state.avoid_openers) if state.avoid_openers else "无"
        return (
            "Human Chat Quality 状态：\n"
            f"- 当前会话：启用\n"
            f"- 稳定规则：{'启用' if self.cfg.inject_stable_rules else '关闭'}（system_prompt）\n"
            f"- 运行时提示：{'启用' if self.cfg.inject_runtime_state else '关闭'}\n"
            f"- 自启动以来累计注入：{self.injection_count} 次\n"
            f"- 最近重复开头：{avoid}"
        )

    def _is_active(self, session_id: str) -> bool:
        if not self.cfg.enabled:
            return False
        return self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: Any | None = None) -> bool:
        if not self._is_active(session_id):
            return False
        return not is_session_disabled(self.cfg.disabled_sessions, session_id, event)


class HumanChatQualityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        cfg = AppConfig.from_config(config)
        data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self.store = RuntimeStateStore(
            data_dir / "runtime_state.json",
            retention_days=cfg.state_retention_days,
            recent_reply_window=cfg.recent_reply_window,
            custom_cliches=cfg.custom_cliches,
        )
        self.core = HumanChatQualityCore(cfg, self.store)
        logger.info(
            f"[HumanChatQuality] plugin loaded, version={PLUGIN_VERSION}, "
            f"sessions={len(self.store.sessions)}, custom_cliches={len(self.store.custom_cliches)}"
        )

    @filter.on_llm_request(priority=-100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 质量层是增强功能：任何内部异常都不应阻断消息主链
        try:
            await self.core.on_llm_request(event, req)
        except Exception as e:
            logger.error(f"[HumanChatQuality] on_llm_request failed: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        try:
            await self.core.on_llm_response(event, resp)
        except Exception as e:
            logger.error(f"[HumanChatQuality] on_llm_response failed: {e}")

    @filter.command_group("humanq")
    def humanq(self):
        """指令组 humanq：质量层会话控制。全部子命令仅管理员可用，作用范围均为当前会话。"""
        pass

    @permission_type(PermissionType.ADMIN)
    @humanq.command("status")
    async def humanq_status(self, event: AstrMessageEvent):
        """查看当前会话的质量层状态与累计注入次数"""
        yield event.plain_result(self.core.status_text(event.unified_msg_origin, event))

    @permission_type(PermissionType.ADMIN)
    @humanq.command("on")
    async def humanq_on(self, event: AstrMessageEvent):
        """启用当前会话的质量层"""
        await self.core.set_session_enabled(event.unified_msg_origin, True)
        yield event.plain_result("Human Chat Quality 已启用当前会话。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("off")
    async def humanq_off(self, event: AstrMessageEvent):
        """关闭当前会话的质量层，直到再次执行 on"""
        await self.core.set_session_enabled(event.unified_msg_origin, False)
        yield event.plain_result("Human Chat Quality 已关闭当前会话。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("reset")
    async def humanq_reset(self, event: AstrMessageEvent):
        """清空当前会话的提醒记录（重复开头与避用词）"""
        await self.core.reset_session(event.unified_msg_origin)
        yield event.plain_result("Human Chat Quality 已清空当前会话的轻量状态。")

    @permission_type(PermissionType.ADMIN)
    @humanq.command("rules")
    async def humanq_rules(self, event: AstrMessageEvent):
        """查看固定规则原文"""
        yield event.plain_result(build_stable_rules())

    async def terminate(self) -> None:
        logger.info(
            "[HumanChatQuality] terminated, total injections this run: "
            f"{self.core.injection_count}; marker={STABLE_RULE_MARKER}"
        )
