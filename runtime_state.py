from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

from .protocols import MessageEventProtocol
from .signal_detectors import OPENER_DELIM, detect_cliches


# 状态文件里 avoid_openers 的上限（重复开头 + 套路词合计）
MAX_AVOID_OPENERS = 5
# 开头截断长度（前缀 ≤8 与普通开头同口径）
MAX_OPENER_LEN = 8
# 避用短语上限（超长短语注入会被截断成半截，入库即过滤，与注入口径一致）
MAX_OPEN_LEN = 20
DAY_SECONDS = 86400
# 同一 opener 在最近窗口内至少出现这么多次才视为重复信号（阈值 3：降低误报）
OPENER_REPEAT_THRESHOLD = 3
# 带次数阈值的结构信号（然而连发）需命中达这么多次，避免口语单次使用误报
CONSECUTIVE_THRESHOLD = 2
# natural-talk 密度口径基准：每 300 字为一个折算档位，更长回复按比例放宽上限


def _now() -> float:
    """当前时间戳（独立函数，便于测试注入时间轴）。"""
    return time.time()


@dataclass
class SessionState:
    avoid_openers: list[str] = field(default_factory=list)
    recent_openers: list[str] = field(default_factory=list)
    last_response_at: float | None = None
    updated_at: float | None = None


class RuntimeStateStore:
    def __init__(
        self,
        state_path: str | Path,
        retention_days: int,
        recent_reply_window: int,
        custom_cliches: Sequence[str] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.retention_days = max(1, int(retention_days or 14))
        # 窗口不得小于重复阈值，否则「≥N 次」永远达不到（静默死区）
        self.recent_reply_window = max(OPENER_REPEAT_THRESHOLD, int(recent_reply_window or 8))
        # 群主自定义词（任意位置精确命中即提示）；内置末尾模板另走 DEFAULT_ENDINGS。
        # 超长条目（>MAX_OPEN_LEN）在构造期过滤并告警，避免每轮命中又被丢弃的静默无效配置。
        cleaned: list[str] = []
        for item in custom_cliches or []:
            text = str(item).strip()
            if not text:
                continue
            if len(text) > MAX_OPEN_LEN:
                if logger is not None:
                    logger.warning(
                        f"[HumanChatQuality] custom_cliche {text[:12]!r}... exceeds {MAX_OPEN_LEN} chars, ignored"
                    )
                continue
            cleaned.append(text)
        self.custom_cliches: tuple[str, ...] = tuple(dict.fromkeys(cleaned))
        self.sessions: dict[str, SessionState] = {}
        # 运行时命令（/humanq off）写入的禁用会话；与配置键 disabled_sessions（静态黑名单）区分，避免同名歧义
        self.runtime_disabled: set[str] = set()
        self._state_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._generation = 0
        self._saved_generation = 0
        self._load()

    def get(self, session_id: str) -> SessionState:
        return self.sessions.get(session_id, SessionState())

    @property
    def has_pending_save(self) -> bool:
        return self._generation > self._saved_generation

    async def flush(self) -> bool:
        async with self._write_lock:
            async with self._state_lock:
                if not self.has_pending_save:
                    return True
                self._prune_expired()
                generation = self._generation
                payload = self._snapshot_unlocked()
            try:
                await asyncio.to_thread(self._write_snapshot_sync, payload)
            except Exception as e:
                if logger is not None:
                    logger.warning(f"[HumanChatQuality] state save failed: {e}")
                return False
            async with self._state_lock:
                self._saved_generation = max(self._saved_generation, generation)
            return True

    async def reset(self, session_id: str) -> bool:
        async with self._state_lock:
            if self.sessions.pop(session_id, None) is not None:
                self._generation += 1
            needs_flush = self.has_pending_save
        return await self.flush() if needs_flush else True

    def is_enabled(self, session_id: str) -> bool:
        return session_id not in self.runtime_disabled

    async def set_enabled(self, session_id: str, enabled: bool) -> bool:
        async with self._state_lock:
            not_before = session_id not in self.runtime_disabled
            if enabled != not_before:
                if enabled:
                    self.runtime_disabled.discard(session_id)
                else:
                    self.runtime_disabled.add(session_id)
                self._generation += 1
            needs_flush = self.has_pending_save
        return await self.flush() if needs_flush else True

    async def record_response(self, session_id: str, response_text: str) -> bool:
        text = re.sub(r"\s+", " ", (response_text or "")).strip()
        if not text:
            return not self.has_pending_save

        async with self._state_lock:
            state = self.sessions.get(session_id, SessionState())
            state.last_response_at = _now()
            state.updated_at = state.last_response_at
            opener = extract_opener(text)
            if opener:
                state.recent_openers = [opener, *state.recent_openers][: self.recent_reply_window]
            # 两路合并进动态提示清单：① 最近窗口里高频重复的开头；② 本轮命中的高置信度信号。
            repeated = repeated_items(state.recent_openers, limit=MAX_AVOID_OPENERS)
            cliches = detect_cliches(text, self.custom_cliches)
            merged: list[str] = []
            for item in [*repeated, *cliches]:
                if item and len(item) <= MAX_OPEN_LEN and item not in merged:
                    merged.append(item)
            state.avoid_openers = merged[:MAX_AVOID_OPENERS]

            self.sessions[session_id] = state
            self._generation += 1
        return await self.flush()

    def _load(self) -> None:
        """状态加载。损坏策略：顶层损坏（JSON 解析失败/根结构非预期）备份+全清；
        条目损坏（容器类型错误/单条 session 非 dict）备份+跳过坏键+warning，保留好数据。"""
        try:
            file_mtime = self.state_path.stat().st_mtime
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            self._backup_corrupt_state_file()
            self.sessions = {}
            self.runtime_disabled = set()
            return

        try:
            backed_up = False
            raw_disabled = raw.get("disabled_sessions") or []
            if isinstance(raw_disabled, list):
                self.runtime_disabled = {str(item) for item in raw_disabled}
            else:
                # 条目级畸形：备份现场并跳过，不触发全清（与 sessions 单条容错一致）
                if not backed_up:
                    self._backup_corrupt_state_file()
                    backed_up = True
                self.runtime_disabled = set()
                if logger is not None:
                    logger.warning(
                        f"[HumanChatQuality] disabled_sessions malformed ({type(raw_disabled).__name__}), skipped"
                    )
            sessions = raw.get("sessions", {})
            if not isinstance(sessions, dict):
                raise TypeError(f"sessions must be a dict, got {type(sessions).__name__}")
            loaded: dict[str, SessionState] = {}
            for session_id, value in sessions.items():
                if not isinstance(value, dict):
                    # 条目级畸形：备份现场一次并跳过坏键，其余会话不受影响
                    if not backed_up:
                        self._backup_corrupt_state_file()
                        backed_up = True
                    if logger is not None:
                        logger.warning(
                            f"[HumanChatQuality] session {session_id!r} malformed ({type(value).__name__}), skipped"
                        )
                    continue
                try:
                    state = _state_from_dict(value, self.recent_reply_window)
                except (AttributeError, TypeError, ValueError) as error:
                    if not backed_up:
                        self._backup_corrupt_state_file()
                        backed_up = True
                    if logger is not None:
                        logger.warning(f"[HumanChatQuality] session {session_id!r} malformed ({error}), skipped")
                    continue
                if state.updated_at is None and state.last_response_at is None:
                    state.updated_at = file_mtime
                loaded[str(session_id)] = state
            self.sessions = loaded
            self._prune_expired()
        except Exception as e:
            if not backed_up:
                self._backup_corrupt_state_file()
            self.sessions = {}
            self.runtime_disabled = set()
            if logger is not None:
                logger.warning(f"[HumanChatQuality] state file reset due to load failure: {e}")

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "disabled_sessions": sorted(self.runtime_disabled),
            "sessions": {
                session_id: _state_to_dict(state, self.recent_reply_window)
                for session_id, state in sorted(self.sessions.items())
            },
        }

    def _write_snapshot_sync(self, payload: dict[str, Any]) -> None:
        """原子写：临时文件 + os.replace；失败清理临时文件后交给 flush() 报告。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self.state_path)
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def _prune_expired(self) -> None:
        now = _now()
        cutoff = now - self.retention_days * DAY_SECONDS
        retained: dict[str, SessionState] = {}
        for session_id, state in self.sessions.items():
            timestamp = state.updated_at if state.updated_at is not None else state.last_response_at
            if timestamp is None or timestamp >= cutoff:
                retained[session_id] = state
        self.sessions = retained

    def _backup_corrupt_state_file(self) -> None:
        if not self.state_path.exists():
            return
        timestamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
        backup_path = self.state_path.with_name(f"{self.state_path.stem}.corrupt.{timestamp}{self.state_path.suffix}")
        try:
            shutil.copy2(self.state_path, backup_path)
        except Exception:
            return
        # 只保留最近 5 份损坏备份，防止磁盘被时间戳文件堆满
        # 按 mtime 排序（文件名串序在 time_ns 变长整数时与时间序不一致，可能删错）
        try:
            backups = sorted(
                self.state_path.parent.glob(f"{self.state_path.stem}.corrupt.*{self.state_path.suffix}"),
                key=lambda p: p.stat().st_mtime,
            )
            for old in backups[:-5]:
                old.unlink()
        except OSError:
            pass


# ===== 会话身份与禁用匹配 =====


def unified_origin(event: MessageEventProtocol) -> str:
    """从 event 取统一会话源标识（session_id / group 兜底共用）。"""
    return str(getattr(event, "unified_msg_origin", "") or "").strip()


def match_keys(session_id: str, group_id: str = "") -> frozenset[str]:
    """归一化禁用匹配键：完整来源、群号、group:/GroupMessage: 前缀、# 前后 base。"""
    candidates: set[str] = set()
    session_id = str(session_id or "").strip()
    group_id = str(group_id or "").strip()
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
    return frozenset(c.lower() for c in candidates if c)


def is_session_disabled(disabled: frozenset[str], session_id: str, event: MessageEventProtocol | None = None) -> bool:
    """配置禁用列表是否命中本会话（event 可提供更准的 group_id）。"""
    if not disabled:
        return False
    group_id = group_id_from_event(event) if event is not None else _parse_group_id_from_origin(session_id)
    return not match_keys(session_id, group_id).isdisjoint(disabled)


def group_id_from_event(event: MessageEventProtocol) -> str:
    """从事件提取群号：平台标准接口优先，origin 解析兜底（两级）。

    4.23.x 全部群消息适配器上 get_group_id() 或 unified_msg_origin 至少一条可用；
    旧版按属性名碰运气提取的中间层与其冗余，已移除。
    """
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None and str(value).strip():
                return str(value).strip()
        except Exception as e:
            if logger is not None:
                logger.debug(f"[HumanChatQuality] get_group_id failed: {e}")
    return _parse_group_id_from_origin(unified_origin(event))


def _parse_group_id_from_origin(origin: str) -> str:
    parts = str(origin or "").strip().split(":", 2)
    if len(parts) >= 3 and "group" in parts[1].lower():
        return parts[2].strip()
    if len(parts) == 2 and "group" in parts[0].lower():
        return parts[1].strip()
    return ""


# opener 前缀（命中即返回，长度均 ≤MAX_OPENER_LEN）
_OPENER_PREFIXES: tuple[str, ...] = ("我会", "好的", "可以", "没问题", "没事", "别急", "明白", "行吧", "好嘞")


def extract_opener(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for prefix in _OPENER_PREFIXES:
        if text.startswith(prefix):
            return prefix
    first = OPENER_DELIM.split(text, maxsplit=1)[0].strip()
    if not first:
        return ""
    # 单字开头（我/你/这/就…）噪声大且无重复信号价值，不纳入
    if len(first) <= 1:
        return ""
    return first[:MAX_OPENER_LEN]


def repeated_items(items: list[str], limit: int, threshold: int = OPENER_REPEAT_THRESHOLD) -> list[str]:
    """返回在窗口内出现达 threshold 次的项（保首次重复顺序）。"""
    counts: dict[str, int] = {}
    repeated: list[str] = []
    for item in items:
        counts[item] = counts.get(item, 0) + 1
        if counts[item] == threshold and item not in repeated:
            repeated.append(item)
    return repeated[:limit]


def _state_from_dict(data: dict[str, Any], recent_reply_window: int) -> SessionState:
    """从字典加载状态（兼容新旧格式）。

    新格式（v2，紧凑）：{"a": [...], "r": "x,y,z", "t": 123456}
    旧格式（v1）：{"avoid_openers": [...], "recent_openers": [...], "updated_at": 123.456, ...}
    """
    # 新格式（紧凑）
    if "a" in data:
        recent_str = data.get("r", "")
        if not isinstance(recent_str, str):
            raise TypeError(f"r must be a string, got {type(recent_str).__name__}")
        recent = [s for s in recent_str.split(",") if s] if recent_str else []
        return SessionState(
            avoid_openers=_list_of_str(data.get("a", []), MAX_AVOID_OPENERS),
            recent_openers=[item[:MAX_OPENER_LEN] for item in recent[:recent_reply_window]],
            last_response_at=None,
            updated_at=float(data.get("t", 0)) if data.get("t") else None,
        )

    # 旧格式（向后兼容）
    return SessionState(
        avoid_openers=_list_of_str(data.get("avoid_openers", []), MAX_AVOID_OPENERS),
        recent_openers=[
            item[:MAX_OPENER_LEN] for item in _list_of_str(data.get("recent_openers", []), recent_reply_window)
        ],
        last_response_at=_optional_float(data.get("last_response_at")),
        updated_at=_optional_float(data.get("updated_at")),
    )


def _state_to_dict(state: SessionState, recent_reply_window: int) -> dict[str, Any]:
    """保存状态（仅使用新格式）。

    紧凑格式：
    - a = avoid_openers (list)
    - r = recent_openers (comma-separated string)
    - t = updated_at (integer timestamp, seconds)
    """
    timestamp = state.updated_at if state.updated_at is not None else state.last_response_at
    return {
        "a": state.avoid_openers[:MAX_AVOID_OPENERS],
        "r": ",".join(state.recent_openers[:recent_reply_window]),
        "t": int(timestamp) if timestamp else 0,
    }


def _list_of_str(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()][:limit]


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
