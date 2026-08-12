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


# 状态文件里 avoid_openers 的上限（重复开头 + 套路词合计）
MAX_AVOID_OPENERS = 5
# opener 入库/加载/提取的截断长度（前缀 ≤8 与普通开头同口径）
MAX_OPENER_LEN = 8
# 重复开头/套路词入库与注入的长度上限（超长短语注入会被截断成半截，入库即过滤，与注入口径一致）
MAX_OPEN_LEN = 20
DAY_SECONDS = 86400
# 同一 opener 在最近窗口内至少出现这么多次才视为重复信号（阈值 3：降低误报）
OPENER_REPEAT_THRESHOLD = 3
# 带次数阈值的结构信号（破折号连发/然而连发）需命中达这么多次，避免口语单次使用误报
CONSECUTIVE_THRESHOLD = 2
# natural-talk：路标词限 ≤2 次/全文，同一条回复累计 ≥3 次视为堆砌（高置信度）
ROAD_SIGN_THRESHOLD = 3


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
        # 保护文件 I/O 的锁，避免并发写同一状态文件
        self._lock = asyncio.Lock()
        self._load()

    def get(self, session_id: str) -> SessionState:
        return self.sessions.get(session_id, SessionState())

    async def reset(self, session_id: str) -> None:
        async with self._lock:
            self.sessions.pop(session_id, None)
            await self._save_unlocked()

    def is_enabled(self, session_id: str) -> bool:
        return session_id not in self.runtime_disabled

    async def set_enabled(self, session_id: str, enabled: bool) -> None:
        async with self._lock:
            not_before = session_id not in self.runtime_disabled
            if enabled == not_before:
                return  # 状态无变化，不触发写盘（高频 toggle 友好）
            if enabled:
                self.runtime_disabled.discard(session_id)
            else:
                self.runtime_disabled.add(session_id)
            await self._save_unlocked()

    async def record_response(self, session_id: str, response_text: str) -> None:
        text = _normalize_text(response_text)
        if not text:
            return

        # 不变量：内存 read-modify-write 与写盘共享同一把锁；临界区内不得插入其它 await
        async with self._lock:
            state = self.sessions.get(session_id, SessionState())
            state.last_response_at = time.time()
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
            await self._save_unlocked()

    def _load(self) -> None:
        """状态加载。损坏策略：顶层损坏（JSON 解析失败/根结构非预期）备份+全清；
        条目损坏（容器类型错误/单条 session 非 dict）备份+跳过坏键+warning，保留好数据。"""
        try:
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
                loaded[str(session_id)] = _state_from_dict(value, self.recent_reply_window)
            self.sessions = loaded
            self._prune_expired()
        except Exception as e:
            self._backup_corrupt_state_file()
            self.sessions = {}
            self.runtime_disabled = set()
            if logger is not None:
                logger.warning(f"[HumanChatQuality] state file reset due to load failure: {e}")

    async def _save_unlocked(self) -> None:
        """保存实现（调用方须已持有 _lock）：先 prune 再构建快照 + 工作线程写盘。"""
        self._prune_expired()
        payload = {
            "disabled_sessions": sorted(self.runtime_disabled),
            "sessions": {
                session_id: _state_to_dict(state, self.recent_reply_window)
                for session_id, state in sorted(self.sessions.items())
            },
        }
        try:
            await asyncio.to_thread(self._write_snapshot_sync, payload)
        except Exception as e:
            if logger is not None:
                logger.warning(f"[HumanChatQuality] state save failed: {e}")

    def _write_snapshot_sync(self, payload: dict[str, Any]) -> None:
        """原子写：临时文件 + os.replace；失败清理临时文件后重新抛出（由 _save_unlocked 兜底）。"""
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
        cutoff = time.time() - self.retention_days * DAY_SECONDS
        self.sessions = {
            session_id: state
            for session_id, state in self.sessions.items()
            if (state.updated_at or state.last_response_at or time.time()) >= cutoff
        }

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

# 结构级 AI 腔信号：仅保留强信号、低误报的连发模式
# 每项独立阈值：连发类 2 次；路标词按 natural-talk"≤2 次/全文"标准取 3 次
_PATTERN_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # 两条及以上"——"才算连发；单个破折号可能是正常停顿
    ("破折号连发", re.compile(r"——"), CONSECUTIVE_THRESHOLD),
    # "然而"是口语高频转折词，单次使用不提示，连发才提示
    ("然而连发", re.compile(r"然而"), CONSECUTIVE_THRESHOLD),
    # natural-talk 路标词：限 ≤2 次/全文，同一回复累计 ≥3 次报堆砌
    ("路标词堆砌", re.compile(r"事实上|实际上|换句话说|本质上|归根结底|与此同时"), ROAD_SIGN_THRESHOLD),
)


def detect_cliches(text: str, custom_cliches: tuple[str, ...] = ()) -> list[str]:
    """检测高置信度 AI 腔信号，返回命中标签（去重、保序）。

    内置末尾模板仅在回复结尾命中；AI 自我暴露短语任意位置精确命中；
    谄媚/预告式开场仅回复首部命中；custom_cliches 是管理员显式词库，
    任意位置一次精确命中即提示。信号来源：natural-talk v2.0.0（MIT）。
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
    for label, pattern, threshold in _PATTERN_CHECKS:
        if label in hits:
            continue
        if len(pattern.findall(normalized)) >= threshold:
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
