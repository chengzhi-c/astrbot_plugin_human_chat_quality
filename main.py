from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core import AppConfig, HumanChatQualityCore
from .quality_rules import STABLE_RULE_MARKER, build_stable_rules
from .runtime_state import RuntimeStateStore


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
        self.core = HumanChatQualityCore(cfg, self.store, text_part_factory=_probe_text_part_cls())
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
        saved = await self.core.set_session_enabled(event.unified_msg_origin, True)
        if saved:
            yield event.plain_result("Human Chat Quality 已启用当前会话。")
        else:
            yield event.plain_result(
                "Human Chat Quality 当前进程内已启用，但状态文件写入失败；已保留待重试状态，请再次执行命令或检查数据目录。"
            )

    @permission_type(PermissionType.ADMIN)
    @humanq.command("off")
    async def humanq_off(self, event: AstrMessageEvent):
        """关闭当前会话的质量层，直到再次执行 on"""
        saved = await self.core.set_session_enabled(event.unified_msg_origin, False)
        if saved:
            yield event.plain_result("Human Chat Quality 已关闭当前会话。")
        else:
            yield event.plain_result(
                "Human Chat Quality 当前进程内已关闭，但状态文件写入失败；已保留待重试状态，请再次执行命令或检查数据目录。"
            )

    @permission_type(PermissionType.ADMIN)
    @humanq.command("reset")
    async def humanq_reset(self, event: AstrMessageEvent):
        """清空当前会话的提醒记录（重复开头与避用词）"""
        saved = await self.core.reset_session(event.unified_msg_origin)
        if saved:
            yield event.plain_result("Human Chat Quality 已清空当前会话的轻量状态。")
        else:
            yield event.plain_result(
                "Human Chat Quality 当前进程内已清空，但状态文件写入失败；已保留待重试状态，请再次执行命令或检查数据目录。"
            )

    @permission_type(PermissionType.ADMIN)
    @humanq.command("rules")
    async def humanq_rules(self, event: AstrMessageEvent):
        """查看固定规则原文"""
        yield event.plain_result(build_stable_rules())

    @permission_type(PermissionType.ADMIN)
    @humanq.command("stats")
    async def humanq_stats(self, event: AstrMessageEvent):
        """查看质量层累计统计"""
        stats = self.core.stats
        top_cliches = stats.top_cliches(5)

        lines = [
            "Human Chat Quality 统计（本次运行）：",
            f"- 累计注入：{stats.total_injections} 次",
            f"  └ 固定规则：{stats.stable_rules_injected} 次",
            f"  └ 动态提醒：{stats.runtime_hints_injected} 次",
            f"- 重复开头避免：{stats.repeated_openers_avoided} 次",
            f"- 历史块清理：{stats.legacy_blocks_removed} 个旧规则 + {stats.stale_hints_removed} 个旧提示",
        ]

        if top_cliches:
            lines.append("- 高频信号 TOP 5：")
            for cliche, count in top_cliches:
                lines.append(f"  {count:>3} 次 {cliche}")

        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        if not await self.store.flush():
            logger.warning("[HumanChatQuality] pending runtime state could not be persisted during terminate")
        logger.info(
            "[HumanChatQuality] terminated, total injections this run: "
            f"{self.core.injection_count}; marker={STABLE_RULE_MARKER}"
        )
