"""信号检测器模块：分层检测 AI 腔信号。

将原 detect_cliches 的复杂逻辑拆分为独立检测器，降低圈复杂度。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from .constants import CONSECUTIVE_THRESHOLD, DENSITY_BASE

# natural-talk Tier 1：AI 自我暴露短语，任意位置精确命中即报（对齐 upstream dist/lexicon tier1_identity 高置信子集）
DEFAULT_AI_CLICHES: tuple[str, ...] = (
    "作为AI",
    "作为人工智能",
    "作为一个语言模型",
    "作为一个AI助手",
    "作为AI语言模型",
    "根据我的训练",
    "基于我的训练数据",
    "训练数据截至",
    "截至我的知识",
    "基于我所掌握的信息",
    "截至我的知识更新",
)

# natural-talk Tier 1/2：谄媚/预告/起手式开场，仅回复首部（首个标点前）命中
OPENING_CLICHES: tuple[str, ...] = (
    "好问题",
    "让我来",
    "感谢你的提问",
    "Great question",
    # C3 复述题目开场
    "关于你提到的",
    "关于您提到的",
    # D1 谄媚越界
    "你问到了核心",
    "你有很强的批判性思维",
    # C5 宏观开场
    "在当今快速发展的时代",
    "随着AI不断进步",
    # B10 起手式
    "说白了",
    "说穿了",
    "先说结论",
    # D5 元话语空预告（仅首部；"让我们先来理解背景"式空预告命中，"让我们先来点音乐吧"类
    # 实际动作不报——由后面的负例清单精确豁免）
    "让我们先来",
    "下面我将",
    "接下来我将",
    "众所周知",
)

# D6 模糊假归因（高置信度无出处假背书，任意位置精确命中）
DEFAULT_VAGUE_ATTRIBUTIONS: tuple[str, ...] = (
    "有研究表明",
    "业内普遍认为",
    "专家指出",
    "不少用户反馈",
)

# D5 负例：这些首部开头是真实动作/指令，不是空预告，命中 OPENING_CLICHES 后在此豁免
_OPENING_NEGATIVE_EXACT: frozenset[str] = frozenset(
    (
        "让我们先来点",
        "让我们先来听",
        "让我们先来看",
        "让我们先来试",
    )
)

# D1 谄媚越界（整句级高置信触发，任意位置命中；上游"主语替换检验法"无法用词表实现，
# 只收整句级高置信触发词，词太长故单独列表）
DEFAULT_SYMPATHY_CLICHES: tuple[str, ...] = (
    "我完全理解你的感受",
    "你说得太对了",
)

# 默认检测只保留高置信度末尾模板（upstream courtesy 高置信收尾 + 打气 + 万能收尾）
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
    "欢迎继续交流",
    "欢迎随时",
    # 空泛打气收尾
    "未来可期",
    "一起加油",
    "共同努力",
    "砥砺前行",
    "不忘初心",
    # D4 万能收尾
    "关键在于找到平衡",
    "关键在于平衡",
    "要结合实际情况",
    "需要综合考虑",
    "因人而异",
    "没有绝对的对错",
    # 收尾腔总结
    "综上所述",
    "由此可见",
    "I hope this helps",
)

# 末尾匹配前剔除的收尾标点/语气符
_TRAILING_PUNCT = "。．.!！?？~～…‥、,，;； \t\r\n"

# 切分正则（供 detect_opening_cliches 与 runtime_state.extract_opener 共用）
OPENER_DELIM = re.compile(r"[，,。.!！?？\n\r]")

# 密度项与 natural-talk 计数口径一致（连续化 scale=max(1,len/300)）
_DENSITY_CHECKS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("破折号", re.compile(r"[—–]"), 2),
    ("感叹号", re.compile(r"[！!]"), 3),
    (
        "路标词堆砌",
        re.compile(r"值得注意的是|需要强调的是|更关键的是|事实上|实际上|换句话说|说白了|本质上|归根结底|与此同时"),
        2,
    ),
)

# Tier3 铁律：结构性表演（精简高置信，去回溯风险：句内 [^。\n] 限长）
_TIER3_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"不是[^。\n]{0,30}(?:而是|而是说|而是要)"),
    re.compile(r"其实不是[^。\n]{0,30}只是"),
    re.compile(r"不仅是[^。\n]{0,20}更(?:是|关乎)"),
    re.compile(r"与其[^。\n]{0,16}不如"),
    re.compile(r"与其说[^。\n]{0,16}不如说"),
    re.compile(r"看似[^。\n]{0,12}实则"),
    re.compile(r"很久[^。\n]{0,6}久到|安静[^。\n]{0,4}静[到得]|沉默[^。\n]{0,4}沉默到"),
    re.compile(r"真正的问题是"),
    re.compile(r"(?:一句话总结|核心是|关键在于|原因如下|本质上|总结如下|具体分析如下|建议如下|分析如下)\s*[:：]"),
    re.compile(r"[—–]{1,2}(?:那就是|那是|原来|其实|也就是|这就是|正因如此|真正的原因)"),
)
_HEDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"可能.{0,4}(?:或许|大概|大致)"),
    re.compile(r"(?:通常来说|一般来说|通常情况下).{0,10}(?:可能|或许|大概|大致|也许)"),
)
# C6 空泛气氛总结：上游限定"具体描写后"的语境条件无法用词表表达，但这些短语本身极低频，
# 任意位置精确命中误报率可接受（进冻结评测集验证）
_ATMOSPHERE_CLICHES: tuple[str, ...] = (
    "声音填满空间",
    "空气仿佛凝固",
    "眼中闪过一丝",
    "时间仿佛静止",
    "世界仿佛安静",
)
_FENCED_CODE_RE = re.compile(r"(```|~~~).*?\1", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HEADING_MD = re.compile(r"^(#{1,6})\s+(.+)$")
_HEADING_BOLD = re.compile(r"^\*\*(.+)\*\*\s*$")
_NUM_PREFIX = re.compile(r"^(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十0-9]+[、，,\s])")


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\r\n]", " ", match.group(0))


def _normalize_text(text: str) -> str:
    """归一化文本：先屏蔽代码与 URL，再合并空白。"""
    return re.sub(r"\s+", " ", _mask_code(text or "")).strip()


def _mask_code(text: str) -> str:
    masked = _FENCED_CODE_RE.sub(_blank, text)
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    return _URL_RE.sub(_blank, masked)


def detect_ending_cliches(text: str) -> list[str]:
    """检测收尾模板（仅结尾命中）。"""
    tail = text.rstrip(_TRAILING_PUNCT)
    folded_tail = tail.casefold()
    for phrase in DEFAULT_ENDINGS:
        if folded_tail.endswith(phrase.casefold()):
            return [phrase]
    return []


def _find_contained_phrases(text: str, phrases: Sequence[str], *, casefold: bool = False) -> list[str]:
    """在文本中检索出现的短语集合（保序、精确子串）。"""
    if not text or not phrases:
        return []
    if casefold:
        target = text.casefold()
        return [p for p in phrases if p and p.casefold() in target]
    return [p for p in phrases if p and p in text]


def detect_ai_self_exposure(text: str) -> list[str]:
    """检测 AI 自我暴露短语（任意位置）。"""
    return _find_contained_phrases(text, DEFAULT_AI_CLICHES)


def detect_opening_cliches(text: str) -> list[str]:
    """检测开场套话（仅首部命中；D5 真实动作指令豁免）。"""
    first_clause = OPENER_DELIM.split(text, maxsplit=1)[0].casefold()
    if any(first_clause.startswith(neg) for neg in _OPENING_NEGATIVE_EXACT):
        return []
    return [phrase for phrase in OPENING_CLICHES if first_clause.startswith(phrase.casefold())]


def detect_custom_cliches(text: str, custom_cliches: tuple[str, ...]) -> list[str]:
    """检测自定义避用词（任意位置精确命中）。"""
    return _find_contained_phrases(text, custom_cliches, casefold=True)


def detect_sympathy_cliches(text: str) -> list[str]:
    """D1 谄媚越界整句触发（任意位置精确命中）。"""
    return _find_contained_phrases(text, DEFAULT_SYMPATHY_CLICHES)


def detect_vague_attributions(text: str) -> list[str]:
    """D6 模糊假归因（任意位置精确命中）。"""
    return _find_contained_phrases(text, DEFAULT_VAGUE_ATTRIBUTIONS)


def detect_atmosphere_cliches(text: str) -> list[str]:
    """C6 空泛气氛总结（任意位置精确命中，短语本身极低频）。"""
    return _find_contained_phrases(text, _ATMOSPHERE_CLICHES)


def detect_fixed_pattern_signals(text: str) -> list[str]:
    """检测固定次数模式（如"然而"连发）。"""
    return ["然而连发"] if text.count("然而") >= CONSECUTIVE_THRESHOLD else []


def detect_density_signals(text: str) -> list[str]:
    """检测密度类信号（按篇幅折算，上游 engine/detector 同口径，阶梯档位）。"""
    density_cap = max(1, math.ceil(len(text) / DENSITY_BASE))
    hits: list[str] = []
    for label, pattern, per_300 in _DENSITY_CHECKS:
        if len(pattern.findall(text)) > density_cap * per_300:
            hits.append(label)
    return hits


def detect_iron_rule(text: str) -> list[str]:
    """Tier3 铁律：先否定后肯定等结构性表演。代码块与 URL 由 _mask_code 遮罩。"""
    for pat in _TIER3_PATTERNS:
        if pat.search(text):
            return ["结构性表演"]
    return []


def detect_numbered_headings(text: str) -> list[str]:
    """B6：同级连续编号小标题 ≥3 才报；普通标题打断序列。"""
    heading_stream: list[tuple[int, bool]] = []
    for raw in _mask_code(text or "").splitlines():
        stripped = raw.strip()
        md = _HEADING_MD.match(stripped)
        if md:
            heading_stream.append((len(md.group(1)), bool(_NUM_PREFIX.match(md.group(2).strip()))))
            continue
        bold = _HEADING_BOLD.match(stripped)
        if bold:
            heading_stream.append((0, bool(_NUM_PREFIX.match(bold.group(1).strip()))))
    run = 0
    run_level: int | None = None
    for level, numbered in heading_stream:
        if numbered:
            if run and level != run_level:
                run = 0
            run += 1
            run_level = level
            if run >= 3:
                return ["编号小标题连发"]
        else:
            run = 0
            run_level = None
    return []


def detect_hedge(text: str) -> list[str]:
    """模糊叠加：可能或许等紧邻模糊词。"""
    for pat in _HEDGE_PATTERNS:
        if pat.search(text):
            return ["模糊叠加"]
    return []


def builtin_signal_names() -> frozenset[str]:
    """detect_cliches 可能吐出的内置命中名（词表面 + 聚合标签）。"""
    return frozenset(
        (
            *DEFAULT_AI_CLICHES,
            *OPENING_CLICHES,
            *DEFAULT_SYMPATHY_CLICHES,
            *DEFAULT_ENDINGS,
            *DEFAULT_VAGUE_ATTRIBUTIONS,
            *_ATMOSPHERE_CLICHES,
            "然而连发",
            "结构性表演",
            "模糊叠加",
            "破折号",
            "感叹号",
            "路标词堆砌",
            "编号小标题连发",
        )
    )


def detect_cliches(text: str, custom_cliches: tuple[str, ...] = ()) -> list[str]:
    """检测高置信度 AI 腔信号（去重、保序，分层对齐 upstream Tier1-6 精简）。

    内置末尾模板仅结尾命中；AI 自我暴露与谄媚整句任意位置；开场仅首部；custom_cliches 任意位置。
    Tier3 铁律（不是…而是/与其说…不如说/看似…实则等）与模糊叠加；密度按 300 字基准折算；
    C6 空泛气氛总结短语任意位置。
    """
    normalized = _normalize_text(text)
    if not normalized:
        return []

    hits: list[str] = []
    seen: set[str] = set()

    for signals in (
        detect_ending_cliches(normalized),
        detect_ai_self_exposure(normalized),
        detect_opening_cliches(normalized),
        detect_sympathy_cliches(normalized),
        detect_vague_attributions(normalized),
        detect_custom_cliches(normalized, custom_cliches),
        detect_fixed_pattern_signals(normalized),
        detect_iron_rule(normalized),
        detect_hedge(normalized),
        detect_atmosphere_cliches(normalized),
        detect_density_signals(normalized),
        detect_numbered_headings(text),
    ):
        for signal in signals:
            if signal not in seen:
                hits.append(signal)
                seen.add(signal)

    return hits


# 危害档位：1 = 损害回答可靠性（上游 D1 谄媚/D3 免责自我暴露），2 = 仅影响观感。
# avoid_openers 里混有词面（"作为AI"）与信号标签（"结构性表演"），两类都按此表排序。
_PRIORITY_1_SIGNALS: frozenset[str] = frozenset(
    (*DEFAULT_AI_CLICHES, *DEFAULT_SYMPATHY_CLICHES, *DEFAULT_VAGUE_ATTRIBUTIONS)
)


def signal_priority(name: str) -> int:
    """返回信号危害档位：1 = 可靠性损害，2 = 观感（默认）。"""
    return 1 if name in _PRIORITY_1_SIGNALS else 2
