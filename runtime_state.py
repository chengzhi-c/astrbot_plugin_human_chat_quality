from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
    ) -> None:
        self.state_path = Path(state_path)
        self.retention_days = max(1, int(retention_days or 14))
        self.recent_reply_window = max(1, int(recent_reply_window or 8))
        self.sessions: dict[str, SessionState] = {}
        self.disabled_sessions: set[str] = set()
        # 保护文件 I/O 的锁，避免并发写同一状态文件
        self._lock = asyncio.Lock()
        self._load()

    def get(self, session_id: str) -> SessionState:
        return self.sessions.get(session_id, SessionState())

    async def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        await self._save()

    def is_enabled(self, session_id: str) -> bool:
        return session_id not in self.disabled_sessions

    async def set_enabled(self, session_id: str, enabled: bool) -> None:
        if enabled:
            self.disabled_sessions.discard(session_id)
        else:
            self.disabled_sessions.add(session_id)
        await self._save()

    async def record_response(self, session_id: str, response_text: str) -> None:
        text = _normalize_text(response_text)
        if not text:
            return

        state = self.sessions.get(session_id, SessionState())
        state.last_response_at = time.time()
        state.updated_at = state.last_response_at
        opener = extract_opener(text)
        if opener:
            state.recent_openers = [opener, *state.recent_openers][: self.recent_reply_window]
        # 两路合并进避用列表：① 最近窗口里重复出现的开头；② 本轮命中的 AI 套路词。
        # 后者让运行时提示即使没有精确重复也能带电，避免常年空转。
        repeated = repeated_items(state.recent_openers, limit=5)
        cliches = detect_cliches(text)
        merged: list[str] = []
        for item in [*repeated, *cliches]:
            if item and item not in merged:
                merged.append(item)
        state.avoid_openers = merged[:5]

        self.sessions[session_id] = state
        self._prune_expired()
        await self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            self._backup_corrupt_state_file()
            self.sessions = {}
            self.disabled_sessions = set()
            return

        try:
            self.disabled_sessions = set(str(item) for item in raw.get("disabled_sessions", []))
            sessions = raw.get("sessions", {})
            self.sessions = {
                str(session_id): _state_from_dict(value)
                for session_id, value in sessions.items()
                if isinstance(value, dict)
            }
            self._prune_expired()
        except Exception:
            self._backup_corrupt_state_file()
            self.sessions = {}
            self.disabled_sessions = set()

    async def _save(self) -> None:
        """异步保存状态，使用锁避免并发写文件冲突；写盘失败不影响回复链路。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "disabled_sessions": sorted(self.disabled_sessions),
            "sessions": {
                session_id: _state_to_dict(state)
                for session_id, state in sorted(self.sessions.items())
            },
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        async with self._lock:
            temp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
            try:
                temp_path.write_text(data, encoding="utf-8")
                os.replace(temp_path, self.state_path)
            except Exception:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise

    def _prune_expired(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
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


def extract_opener(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for prefix in ("我会", "好的", "可以", "没事", "别急", "明白"):
        if text.startswith(prefix):
            return prefix
    first = re.split(r"[，,。.!！?？\n\r]", text, maxsplit=1)[0].strip()
    if not first:
        return ""
    return first[:8]


# 日常聊天里最常见的 AI 套路词/收尾腔，命中即提示模型本轮避开。
# 这些是“说明模型正在犯 AI 腔”的信号，不是要禁掉的功能词，故按短语精确匹配。
DEFAULT_CLICHES: tuple[str, ...] = (
    "希望能帮到你",
    "希望这能帮到你",
    "如果还有问题",
    "总之",
    "综上所述",
    "未来可期",
    "一起加油",
    "需要注意的是",
    "值得一提的是",
    "首先",
    "作为 AI",
    "作为AI",
    "让我们",
)


def detect_cliches(text: str, cliches: tuple[str, ...] = DEFAULT_CLICHES) -> list[str]:
    """检测回复中出现的 AI 套路词，返回命中的短语（去重、保序）。"""
    normalized = _normalize_text(text)
    if not normalized:
        return []
    hits: list[str] = []
    for phrase in cliches:
        if phrase and phrase in normalized and phrase not in hits:
            hits.append(phrase)
    return hits


def repeated_items(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for item in items:
        if item in seen and item not in repeated:
            repeated.append(item)
        seen.add(item)
    return repeated[:limit]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _state_from_dict(data: dict[str, Any]) -> SessionState:
    return SessionState(
        avoid_openers=_list_of_str(data.get("avoid_openers", []), 5),
        recent_openers=_list_of_str(data.get("recent_openers", []), 20),
        last_response_at=_optional_float(data.get("last_response_at")),
        updated_at=_optional_float(data.get("updated_at")),
    )


def _state_to_dict(state: SessionState) -> dict[str, Any]:
    data = asdict(state)
    data["avoid_openers"] = state.avoid_openers[:5]
    data["recent_openers"] = [item[:8] for item in state.recent_openers[:20]]
    return data


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
