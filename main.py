from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

# 双导入约定：except 分支服务于 tests 以顶层模块方式导入（仓库目录名带 -main 后缀无法包导入）；
# 新增导入名须两处同步。真实 AstrBot 宿主以包方式加载时走相对导入。
try:
    from .quality_rules import (
        RUNTIME_HINT_MARKER,
        STABLE_RULE_MARKER,
        append_temp_text_part,
        build_runtime_hint,
        build_stable_rules,
        inject_stable_rules,
        remove_marker_in_contexts,
        replace_marker_in_contexts,
        scan_request_markers,
    )
    from .runtime_state import RuntimeStateStore
except ImportError:  # pragma: no cover — 测试等无宿主环境以顶层模块运行时的导入回退
    from quality_rules import (
        RUNTIME_HINT_MARKER,
        STABLE_RULE_MARKER,
        append_temp_text_part,
        build_runtime_hint,
        build_stable_rules,
        inject_stable_rules,
        remove_marker_in_contexts,
        replace_marker_in_contexts,
        scan_request_markers,
    )
    from runtime_state import RuntimeStateStore


PLUGIN_ID = "astrbot_plugin_human_chat_quality"
PLUGIN_VERSION = "0.6.2"


def config_get(config: Any, key: str, default: Any) -> Any:
    """dict 兼容读取（AstrBotConfig 是 dict 子类，MRO 实测确认）。"""
    if config is None:
        return default
    return config.get(key, default)


def config_bool(config: Any, key: str, default: bool) -> bool:
    value = config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
    if isinstance(completion, str):
        completion_text = completion.strip()
        if completion_text:
            return completion_text
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
        # 兜底路径只吸收模型输出：明确非 assistant 的 part（如平台消息里的用户输入）跳过
        role = getattr(item, "role", None)
        if role is not None and role != "assistant":
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        # 某些 provider 把文本放在 content 字段（仅接受 str，结构化内容不进入记录）
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
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
        # 配置快照：README 约定"修改配置需重载插件生效"，构造时解析一次，
        # 热路径只读属性，不再每轮重复解析
        self._enabled = config_bool(self.config, "enabled", True)
        self._inject_stable = config_bool(self.config, "inject_stable_rules", True)
        self._inject_runtime = config_bool(self.config, "inject_runtime_state", True)
        self._debug_log = config_bool(self.config, "debug_log", False)
        self._max_hint_chars = config_int(self.config, "max_runtime_hint_chars", 600, 80, 3000)
        self._disabled_sessions = frozenset(item.lower() for item in config_list(self.config, "disabled_sessions"))
        # stable 迁移清理按会话只执行一次：v0.6.0 起稳定规则只写 system 不入历史
        self._stable_migration_checked: set[str] = set()

    async def on_llm_request(self, event: Any, req: Any) -> None:
        session_id = self._session_id(event)
        if not session_id:
            # 无来源事件不参与状态管理，避免全部挤进同一会话互相污染
            return
        if not self._is_effectively_active(session_id, event):
            return

        # v0.6.0：稳定规则幂等写 system_prompt（不再入历史）；runtime 提示走 temp extra，
        # 历史旧块原位替换、清空后同步移除（改写随本次保存写回历史）。
        injected_rules = False
        injected_hint = ""
        removed_stale = False

        if self._inject_stable:
            # 迁移：清掉落入历史的旧规则块（每会话只检查一次，历史恢复干净后不再全扫）
            if session_id not in self._stable_migration_checked:
                if remove_marker_in_contexts(req, STABLE_RULE_MARKER):
                    removed_stale = True
                self._stable_migration_checked.add(session_id)
            before = getattr(req, "system_prompt", "") or ""
            after = inject_stable_rules(before)
            if after != before:
                req.system_prompt = after
                injected_rules = True

        if self._inject_runtime:
            state = self.store.get(session_id)
            # 单次遍历收集本轮已存在的注入标记（仅 runtime 分支消费；双关时零扫描）
            found = scan_request_markers(req, (RUNTIME_HINT_MARKER,))
            hint = build_runtime_hint(state, max_chars=self._max_hint_chars)
            if hint:
                # 历史已有旧块则原位替换（每轮更新重复开头清单，且不累积）；
                # 首轮（历史无块）走 append；发现 marker 但不可安全替换时不追加，防累积。
                if replace_marker_in_contexts(req, RUNTIME_HINT_MARKER, hint) or (
                    RUNTIME_HINT_MARKER not in found
                    and append_temp_text_part(req, hint, self.text_part_factory, marker=RUNTIME_HINT_MARKER)
                ):
                    injected_hint = hint
            elif RUNTIME_HINT_MARKER in found and remove_marker_in_contexts(req, RUNTIME_HINT_MARKER):
                # hint 已清空：同步移除历史中的旧动态块，失效约束不留给模型
                removed_stale = True

        if injected_rules or injected_hint:
            self.injection_count += 1
        if (injected_rules or injected_hint or removed_stale) and self._debug_log:
            logger.debug(f"[HumanChatQuality] injected quality hints for {session_id}")
            if injected_rules:
                logger.debug(
                    f"[HumanChatQuality] stable rules injected into system_prompt (marker={STABLE_RULE_MARKER})"
                )
            if injected_hint:
                logger.debug(f"[HumanChatQuality] runtime hint injected:\n{injected_hint}")
                # 仅注入 runtime 提示时才有本轮状态可读（state 在 runtime 分支内定义）
                logger.debug(f"[HumanChatQuality] runtime state for {session_id}: avoid_openers={state.avoid_openers}")
            if removed_stale:
                logger.debug(f"[HumanChatQuality] stale injection block removed from contexts for {session_id}")

    async def on_llm_response(self, event: Any, resp: Any) -> None:
        session_id = self._session_id(event)
        if not session_id:
            return
        if not self._is_effectively_active(session_id, event):
            return
        # 优先读取 completion_text，兼容 result_chain / message 对象
        text = _extract_response_text(resp)
        if not text:
            return
        await self.store.record_response(session_id, text)
        if self._debug_log:
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
            f"- 稳定规则：{'启用' if self._inject_stable else '关闭'}（system_prompt）\n"
            f"- 运行时提示：{'启用' if self._inject_runtime else '关闭'}\n"
            f"- 自启动以来累计注入：{self.injection_count} 次\n"
            f"- 最近重复开头：{avoid}"
        )

    def _is_active(self, session_id: str) -> bool:
        if not self._enabled:
            return False
        return self.store.is_enabled(session_id)

    def _is_effectively_active(self, session_id: str, event: Any | None = None) -> bool:
        if not self._is_active(session_id):
            return False
        if not self._disabled_sessions:
            return True
        candidates = (
            disabled_match_candidates(event)
            if event is not None
            else disabled_match_candidates_from_session(session_id)
        )
        return not any(candidate in self._disabled_sessions for candidate in candidates)

    @staticmethod
    def _session_id(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()


def _disabled_candidates(session_id: str, group_id: str) -> set[str]:
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
    return {candidate.lower() for candidate in candidates if candidate}


def disabled_match_candidates(event: Any) -> set[str]:
    return _disabled_candidates(
        str(getattr(event, "unified_msg_origin", "") or "").strip(),
        group_id_from_event(event),
    )


def disabled_match_candidates_from_session(session_id: str) -> set[str]:
    session_id = str(session_id or "").strip()
    return _disabled_candidates(session_id, group_id_from_session_id(session_id))


def group_id_from_event(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None and str(value).strip():
                return str(value).strip()
        except Exception as e:
            if logger is not None:
                logger.debug(f"[HumanChatQuality] get_group_id failed: {e}")

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
            group_id = normalize_group_id(value.get(key))
            if group_id:
                return group_id
        return ""
    for attr in ("group_id", "id", "qq", "uin"):
        group_id = normalize_group_id(getattr(value, attr, None))
        if group_id:
            return group_id
    return normalize_group_id(value)


def normalize_group_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


@register(
    PLUGIN_ID,
    "chengzhi-c",
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
            custom_cliches=config_list(self.config, "custom_cliches"),
        )
        self.core = HumanChatQualityCore(self.config, self.store)
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
            f"[HumanChatQuality] terminated, total injections this run: {self.core.injection_count}; marker={STABLE_RULE_MARKER}"
        )
