"""信号检测器模块：分层检测 AI 腔信号。

将原 detect_cliches 的复杂逻辑拆分为独立检测器，降低圈复杂度。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable


# natural-talk Tier 1：AI 自我暴露短语，任意位置精确命中即报
DEFAULT_AI_CLICHES: tuple[str, ...] = (
    "作为AI",
    "根据我的训练",
    "截至我的知识更新",
)

# natural-talk Tier 1/2：谄媚/预告式开场，仅回复首部（首个标点前）命中
OPENING_CLICHES: tuple[str, ...] = (
    "好问题",
    "让我来",
    "感谢你的提问",
    "Great question",
)

# 默认检测只保留高置信度末尾模板
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
    # 收尾腔总结
    "综上所述",
    "由此可见",
    "I hope this helps",
)

# 末尾匹配前剔除的收尾标点/语气符
_TRAILING_PUNCT = "。．.!！?？~～…‥、,，;； \t\r\n"

# 切分正则
_OPENER_DELIM = re.compile(r"[，,。.!！?？\n\r]")

# 固定计数类信号
_FIXED_PATTERN_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("然而连发", re.compile(r"然而"), 2),
)

# 密度项与 natural-talk 计数口径一致
_DENSITY_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("破折号", re.compile(r"[—–]"), 2),
    ("感叹号", re.compile(r"[！!]"), 3),
    ("路标词堆砌", re.compile(r"事实上|实际上|换句话说|本质上|归根结底|与此同时"), 2),
)


def _normalize_text(text: str) -> str:
    """归一化文本：合并空白。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def detect_ending_cliches(text: str) -> list[str]:
    """检测收尾模板（仅结尾命中）。"""
    tail = text.rstrip(_TRAILING_PUNCT)
    folded_tail = tail.casefold()
    for phrase in DEFAULT_ENDINGS:
        if folded_tail.endswith(phrase.casefold()):
            return [phrase]
    return []


def detect_ai_self_exposure(text: str) -> list[str]:
    """检测 AI 自我暴露短语（任意位置）。"""
    return [phrase for phrase in DEFAULT_AI_CLICHES if phrase in text]


def detect_opening_cliches(text: str) -> list[str]:
    """检测开场套话（仅首部命中）。"""
    first_clause = _OPENER_DELIM.split(text, maxsplit=1)[0].casefold()
    return [phrase for phrase in OPENING_CLICHES if first_clause.startswith(phrase.casefold())]


def detect_custom_cliches(text: str, custom_cliches: tuple[str, ...]) -> list[str]:
    """检测自定义避用词（任意位置精确命中）。"""
    return [phrase for phrase in custom_cliches if phrase and phrase in text]


def detect_fixed_pattern_signals(text: str) -> list[str]:
    """检测固定次数模式（如"然而"连发）。"""
    hits: list[str] = []
    for label, pattern, threshold in _FIXED_PATTERN_CHECKS:
        if len(pattern.findall(text)) >= threshold:
            hits.append(label)
    return hits


def detect_density_signals(text: str) -> list[str]:
    """检测密度类信号（按篇幅折算上限）。"""
    density_cap = max(1, math.ceil(len(text) / 300))
    hits: list[str] = []
    for label, pattern, per_300 in _DENSITY_CHECKS:
        if len(pattern.findall(text)) > density_cap * per_300:
            hits.append(label)
    return hits


@dataclass(frozen=True)
class SignalDetector:
    """信号检测器抽象。"""

    name: str
    detect: Callable[[str], list[str]]
    priority: int


# 检测器列表（按优先级排序）
_DETECTORS: tuple[SignalDetector, ...] = (
    SignalDetector("ending", detect_ending_cliches, 1),
    SignalDetector("ai_self", detect_ai_self_exposure, 2),
    SignalDetector("opening", detect_opening_cliches, 3),
    SignalDetector("custom", lambda t: [], 4),  # 占位符，实际使用时传入 custom_cliches
    SignalDetector("fixed", detect_fixed_pattern_signals, 5),
    SignalDetector("density", detect_density_signals, 6),
)


def detect_cliches(text: str, custom_cliches: tuple[str, ...] = ()) -> list[str]:
    """检测高置信度 AI 腔信号（去重、保序）。

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
    seen: set[str] = set()

    for detector in _DETECTORS:
        if detector.name == "custom":
            signals = detect_custom_cliches(normalized, custom_cliches)
        else:
            signals = detector.detect(normalized)

        for signal in signals:
            if signal not in seen:
                hits.append(signal)
                seen.add(signal)

    return hits
