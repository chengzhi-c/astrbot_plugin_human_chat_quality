from __future__ import annotations

import asyncio
import json
import math
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


# 状态文件里 avoid_openers 的上限（重复开头 + 套路词合计）
MAX_AVOID_OPENERS = 5
# opener 入库/加载/提取的截断长度（前缀 ≤8 与普通开头同口径）
MAX_OPENER_LEN = 8
# 重复开头/套路词入库与注入的长度上限（超长短语注入会被截断成半截，入库即过滤，与注入口径一致）
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
        text = _normalize_text(response_text)
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
            raw_disabled = raw.get("disabled_sessions") or []
            if isinstance(raw_disabled, list):
                self.runtime_disabled = {str(item) for item in raw_disabled}
            else:
                # 条目级畸形：备份现场并跳过，不触发全清（与 sessions 单条容错一致）
                self._backup_corrupt_state_file()
                self.runtime_disabled = set()
                if logger is not None:
                    logger.warning(
                        f"[HumanChatQuality] disabled_sessions malformed ({type(raw_disabled).__name__}), skipped"
                    )
            sessions = raw.get("sessions", {})
            if not isinstance(sessions, dict):
                raise TypeError(f"sessions must be a dict, got {type(sessions).__name__}")
            loaded: dict[str, SessionState] = {}
            backed_up = False
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
                state = _state_from_dict(value, self.recent_reply_window)
                if state.updated_at is None and state.last_response_at is None:
                    state.updated_at = file_mtime
                loaded[str(session_id)] = state
            self.sessions = loaded
            self._prune_expired()
        except Exception as e:
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


def unified_origin(event: Any) -> str:
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


def is_session_disabled(disabled: frozenset[str], session_id: str, event: Any | None = None) -> bool:
    """配置禁用列表是否命中本会话（event 可提供更准的 group_id）。"""
    if not disabled:
        return False
    group_id = group_id_from_event(event) if event is not None else _parse_group_id_from_origin(session_id)
    return not match_keys(session_id, group_id).isdisjoint(disabled)


def group_id_from_event(event: Any) -> str:
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
    return ""


# opener 前缀（命中即返回，长度均 ≤MAX_OPENER_LEN）；切分正则编译一次，避免每次响应重建
_OPENER_PREFIXES: tuple[str, ...] = ("我会", "好的", "可以", "没问题", "没事", "别急", "明白", "行吧", "好嘞")
_OPENER_DELIM = re.compile(r"[，,。.!！?？\n\r]")


def extract_opener(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for prefix in _OPENER_PREFIXES:
        if text.startswith(prefix):
            return prefix
    first = _OPENER_DELIM.split(text, maxsplit=1)[0].strip()
    if not first:
        return ""
    # 单字开头（我/你/这/就…）噪声大且无重复信号价值，不纳入
    if len(first) <= 1:
        return ""
    return first[:MAX_OPENER_LEN]


# natural-talk Tier 1：AI 自我暴露短语，任意位置精确命中即报（近零误报）
DEFAULT_AI_CLICHES: tuple[str, ...] = (
    "作为AI",
    "根据我的训练",
    "截至我的知识更新",
)

# natural-talk Tier 1/2：谄媚/预告式开场，仅回复首部（首个标点前）命中；
# 刻意不用宽泛前缀（如"感谢你"），避免"感谢你的建议"等人话误报
OPENING_CLICHES: tuple[str, ...] = (
    "好问题",
    "让我来",
    "感谢你的提问",
)

# 默认检测只保留高置信度末尾模板：仅当回复以这些短语收尾时命中，
# 正文中出现不再误报。普通连接词（此外/事实上/总之等）与黑话词已移除，
# 用户特殊需求用 custom_cliches（任意位置精确命中）。
# 分段注释仅便于阅读：客服收尾 → 空泛打气收尾 → 收尾腔总结；对外只暴露 DEFAULT_ENDINGS。
DEFAULT_ENDINGS: tuple[str, ...] = (
    # 客服收尾
    "希望能帮到你",
    "希望这能帮到你",
    "希望对你有帮助",
    "希望对您有帮助",
    "希望对你有所帮助",
    "如果还有问题",
    "如果还有其他问题",
    "有任何问题随时",
    "随时联系我",
    "随时问我",
    # 空泛打气收尾
    "未来可期",
    "一起加油",
    "共同努力",
    "砥砺前行",
    "不忘初心",
    # 收尾腔总结（natural-talk Tier 2：收尾腔 = 0；仅结尾命中，正文不报）
    "综上所述",
    "由此可见",
)

# 末尾匹配前剔除的收尾标点/语气符
_TRAILING_PUNCT = "。．.!！?？~～…‥、,，;； \t\r\n"

# 结构级 AI 腔信号分两类：固定计数类（插件自有强信号）与密度类（对齐 natural-talk 计数口径）
_FIXED_PATTERN_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # "然而"是口语高频转折词，单次使用不提示，连发才提示
    ("然而连发", re.compile(r"然而"), CONSECUTIVE_THRESHOLD),
)
# 密度项与 natural-talk 计数口径一致：上限 = 每 300 字基准 × 全文档位，超过才报
# （破折号/路标词 ≤2 次、感叹号 ≤3 次，均按出现次数计；em dash 与 en dash 都算破折号）
_DENSITY_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("破折号", re.compile(r"[—–]"), 2),
    ("感叹号", re.compile(r"[！!]"), 3),
    ("路标词堆砌", re.compile(r"事实上|实际上|换句话说|本质上|归根结底|与此同时"), 2),
)


def detect_cliches(text: str, custom_cliches: tuple[str, ...] = ()) -> list[str]:
    """检测高置信度 AI 腔信号，返回命中标签（去重、保序）。

    内置末尾模板仅在回复结尾命中；AI 自我暴露短语任意位置精确命中；
    谄媚/预告式开场仅回复首部命中；custom_cliches 是管理员显式词库，
    任意位置一次精确命中即提示。
    结构信号：固定计数类（然而连发 ≥2）；密度类对齐 natural-talk 计数口径
    （破折号/路标词 ≤2 次、感叹号 ≤3 次，300 字基准按篇幅折算，超上限才报）。
    """
    normalized = _normalize_text(text)
    if not normalized:
        return []
    hits: list[str] = []
    tail = normalized.rstrip(_TRAILING_PUNCT)
    # 各 ending 互斥（同一结尾不可能同时以两个短语收尾），取首个命中即足够
    for phrase in DEFAULT_ENDINGS:
        if tail.endswith(phrase):
            hits.append(phrase)
            break
    # natural-talk Tier 1：自我暴露短语，任意位置精确命中即报
    for phrase in DEFAULT_AI_CLICHES:
        if phrase in normalized and phrase not in hits:
            hits.append(phrase)
    # natural-talk Tier 1/2：谄媚/预告式开场，仅首部（首个标点前）命中
    first_clause = _OPENER_DELIM.split(normalized, maxsplit=1)[0]
    for phrase in OPENING_CLICHES:
        if phrase not in hits and first_clause.startswith(phrase):
            hits.append(phrase)
    for phrase in custom_cliches:
        if phrase and phrase in normalized and phrase not in hits:
            hits.append(phrase)
    for label, pattern, threshold in _FIXED_PATTERN_CHECKS:
        if label in hits:
            continue
        if len(pattern.findall(normalized)) >= threshold:
            hits.append(label)
    # 密度项：上限随篇幅折算，超过上限才报（与 natural-talk 计数口径一致）
    density_cap = max(1, math.ceil(len(normalized) / 300))
    for label, pattern, per_300 in _DENSITY_CHECKS:
        if label in hits:
            continue
        if len(pattern.findall(normalized)) > density_cap * per_300:
            hits.append(label)
    return hits


def repeated_items(items: list[str], limit: int, threshold: int = OPENER_REPEAT_THRESHOLD) -> list[str]:
    """返回在窗口内出现达 threshold 次的项（保首次重复顺序）。"""
    counts: dict[str, int] = {}
    repeated: list[str] = []
    for item in items:
        counts[item] = counts.get(item, 0) + 1
        if counts[item] == threshold and item not in repeated:
            repeated.append(item)
    return repeated[:limit]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _state_from_dict(data: dict[str, Any], recent_reply_window: int) -> SessionState:
    return SessionState(
        avoid_openers=_list_of_str(data.get("avoid_openers", []), MAX_AVOID_OPENERS),
        # 加载侧 clamp：与 save 侧共同维持"条目 ≤MAX_OPENER_LEN 字"不变量（防外部编辑塞入超长条目）
        recent_openers=[
            item[:MAX_OPENER_LEN] for item in _list_of_str(data.get("recent_openers", []), recent_reply_window)
        ],
        last_response_at=_optional_float(data.get("last_response_at")),
        updated_at=_optional_float(data.get("updated_at")),
    )


def _state_to_dict(state: SessionState, recent_reply_window: int) -> dict[str, Any]:
    # 手工构建替代 asdict：avoid_openers/recent_openers 需切片，避免 asdict 深拷贝后覆盖的重复分配
    return {
        "avoid_openers": state.avoid_openers[:MAX_AVOID_OPENERS],
        "recent_openers": state.recent_openers[:recent_reply_window],
        "last_response_at": state.last_response_at,
        "updated_at": state.updated_at,
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
