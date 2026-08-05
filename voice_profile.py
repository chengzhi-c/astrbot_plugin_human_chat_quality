from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:
    from .quality_rules import INJECTED_MARKER_PREFIX
except ImportError:  # pragma: no cover
    from quality_rules import INJECTED_MARKER_PREFIX

VOICE_MARKER = f"{INJECTED_MARKER_PREFIX} Voice]"

# 样本不足时不注入，避免单条消息带偏风格判断
MIN_SAMPLES = 5
# 短消息阈值（字符数）
SHORT_MSG_THRESHOLD = 20
# 风格样本只取最近 N 条用户消息，控制每轮统计成本
MAX_SAMPLES = 60

# 覆盖常见 emoji 与颜文字常用符号（含代理对、地区旗帜）
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\U0001F000-\U0001F02F\u2600-\u27BF\u2B50\u2764\uFE0F\u2190-\u21FF]"
)
_TONE_WORDS = ("吧", "呢", "嘛", "啊", "哦", "啦", "哈")
# 引用消息前缀：(昵称): 内容 / （昵称）：内容
_QUOTE_PREFIX_RE = re.compile(r"^[(（][^)）]*[)）]\s*[:：]?\s*")
# @ 提及前缀：@昵称 内容
_AT_PREFIX_RE = re.compile(r"^@[^\s:：]+[:：]?\s*")


def _is_injected_text(text: str) -> bool:
    return INJECTED_MARKER_PREFIX in text


def _clean_sample(raw: str) -> str:
    """去掉引用/@ 前缀后返回风格样本；空或注入文本返回空串。"""
    text = raw.strip()
    if not text or _is_injected_text(text):
        return ""
    text = _QUOTE_PREFIX_RE.sub("", text, count=1).strip()
    text = _AT_PREFIX_RE.sub("", text, count=1).strip()
    return text


def extract_user_texts(contexts: Any, limit: int = MAX_SAMPLES) -> list[str]:
    """从 OpenAI 格式 contexts 中提取用户消息文本，只取最近 limit 条（反向收集，历史很长时不用全量遍历）。"""
    texts: list[str] = []
    for ctx in reversed(contexts or []):
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content", "")
        if isinstance(content, str):
            pieces = [content]
        elif isinstance(content, list):
            pieces = [
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
        else:
            continue
        for raw in pieces:
            text = _clean_sample(raw)
            if not text:
                continue
            texts.append(text)
            if limit > 0 and len(texts) >= limit:
                texts.reverse()
                return texts
    texts.reverse()
    return texts


@dataclass
class VoiceProfile:
    sample_count: int = 0
    avg_msg_len: float = 0.0
    short_ratio: float = 0.0
    emoji_ratio: float = 0.0
    tone_words: list[str] = field(default_factory=list)
    openers: list[str] = field(default_factory=list)


def analyze_profile(texts: list[str]) -> VoiceProfile | None:
    """统计用户消息的风格特征。样本不足返回 None。"""
    if len(texts) < MIN_SAMPLES:
        return None
    lengths = [len(t) for t in texts]
    avg = sum(lengths) / len(lengths)
    short_ratio = sum(1 for n in lengths if n <= SHORT_MSG_THRESHOLD) / len(lengths)
    emoji_ratio = sum(1 for t in texts if _EMOJI_RE.search(t)) / len(texts)

    tone_counts: dict[str, int] = {}
    for t in texts:
        for word in _TONE_WORDS:
            count = t.count(word)
            if count:
                tone_counts[word] = tone_counts.get(word, 0) + count
    opener_counts: dict[str, int] = {}
    for t in texts:
        opener = t[:2]
        # 单字符消息（嗯/哈/哦）不构成"常以 X 开头"特征，避免诱导复读单字
        if len(opener) >= 2:
            opener_counts[opener] = opener_counts.get(opener, 0) + 1

    return VoiceProfile(
        sample_count=len(texts),
        avg_msg_len=round(avg, 1),
        short_ratio=round(short_ratio, 2),
        emoji_ratio=round(emoji_ratio, 2),
        tone_words=_top_keys(tone_counts, 3),
        openers=_top_keys(opener_counts, 3),
    )


def build_voice_hint(
    profile: VoiceProfile,
    max_chars: int = 200,
    exclude_openers: set[str] | None = None,
) -> str:
    """把风格特征压成 1-3 句轻量提示；无可用特征时返回空串。

    exclude_openers：与 runtime 避用开头重叠时排除，避免同轮注入矛盾指令
    （voice 说"常以 X 开头"而 runtime 说"本轮别用 X"）。
    """
    traits: list[str] = []
    if profile.avg_msg_len <= 15:
        traits.append("消息偏短")
    elif profile.avg_msg_len <= 40:
        traits.append("长短适中")
    else:
        traits.append("消息偏长")
    if profile.short_ratio >= 0.5:
        traits.append("爱用短句")
    if profile.emoji_ratio >= 0.3:
        traits.append("常带表情")
    elif profile.emoji_ratio <= 0.05:
        traits.append("基本不用表情")
    if profile.tone_words:
        traits.append(f"句末常带语气词{'/'.join(profile.tone_words)}")
    excluded = {a for a in (exclude_openers or set()) if a}
    # 前缀归一化："好的"与"好的，我来"这类变体视为重叠，避免同轮矛盾指令
    openers = [
        o
        for o in profile.openers
        if o and not any(a.startswith(o) or o.startswith(a) for a in excluded)
    ]
    if openers:
        traits.append(f"常以{'/'.join(openers)}开头")
    if not traits:
        return ""

    hint = (
        f"{VOICE_MARKER}\n"
        f"本会话风格参考：{'，'.join(traits)}（基于最近 {profile.sample_count} 条消息）。"
        "回复节奏向这个风格靠拢，但保持你自己的表达，别机械模仿。"
    )
    return _clip_hint(hint, max_chars)


def _clip_hint(text: str, max_chars: int) -> str:
    """与 quality_rules._clip 同语义的截断（独立实现避免跨模块私有依赖）。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3].rstrip() + "..."


def _top_keys(counts: dict[str, int], limit: int) -> list[str]:
    return [key for key, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
