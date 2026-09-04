from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

from .constants import MAX_AVOID_ITEM_LEN, MAX_AVOID_ITEMS
from .protocols import ProviderRequestProtocol, TextPartFactoryProtocol

# 所有注入 marker 的公共前缀
INJECTED_MARKER_PREFIX = "[Human Chat Quality"
# 规则版本：升级 natural-talk 时 +1。3.0.0 起 v1–v8 剥离签名表已退役：
# 旧块按普通文本保留；reset 与保留期只清状态文件，不剥历史里的旧规则正文。
RULES_VERSION = 11
STABLE_RULE_MARKER = f"{INJECTED_MARKER_PREFIX} Rules v{RULES_VERSION}]"
RUNTIME_HINT_MARKER = f"{INJECTED_MARKER_PREFIX} Runtime]"
_RUNTIME_INSTRUCTION = "本轮避开这些重复项，换种自然说法，别提本提示："
_RUNTIME_PREFIX = f"{RUNTIME_HINT_MARKER}\n{_RUNTIME_INSTRUCTION}\n"
_RUNTIME_ITEM_SEPARATOR = "、"
_LITE_CORE = """natural-talk 轻量注入版（规范源：SKILL.md 日常对话与问答核心准绳）

原则：不知即说，不编造，不加免责包装；评价只针对内容客体，不针对人；被问身份如实简答；代码、公式、URL、YAML frontmatter、表格与资料引用禁改。

日常对话与回答问题自查：
- 首句直接给结论或第一个事实，末句落在事实、建议或排查边界上，删客服客套（好问题/希望能帮到你/随时联系我） [D2]
- 严禁复述题目开场：首句从答案开始，严禁用“关于你提到的……”把用户问题重念一遍 [C3]
- 视觉不过度结构化：短回答优先呼吸自然的段落展开；各项间有连贯推演逻辑的，严禁碎片化拆成列表或滥用粗体；用户明确要求清单除外 [C1]
- 评价只针对内容：检验法——主语换成"这个问题/这段代码"，换不了的即越界；倾诉安慰场景坦陈真实主观感受，不做虚假心理按摩 [D1]
- 问什么答什么并明确给出倾向；确属两难时写明判断条件与权衡基准，不得以"关键在于平衡""因人而异"消解信息 [D4]
- 用户给出数量时严格按原量词执行：“给3步/3条”=恰好3，“至少3”=不少于3，“至多3”=不超过3；不借机扩成教程 [D5]
- 严禁模糊假归因：删掉无出处的"有研究表明/业内普遍认为/不少用户反馈"，没来源直接陈述事实 [D6]

句式检查：
- 删"说白了""说穿了""先说结论"，直接给判断 [B10]
- 彻底封杀翻案腔：严禁自立靶子搞“先否定再肯定”（不是……而是/与其说……不如说）；删掉前半句否定与转折，直接正面陈述肯定事实 [B1]
- 删"一句话总结：/核心是：/总结如下：/建议如下："等空转提示语加冒号引列表，自然承接 [B4]
- 非首段的评论式开头必须能指出明确承接对象，恢复具体主语或删掉空评论，不机械补"这" [B3]
- 消除泛滥破折号：严禁使用“——”制造刻意揭晓或后置补充（如“核心只有一个——缓存”）；改成常规标点（逗号/句号）或自然融入整句，除代码外不打叙述破折号 [B5]
- 拆解过长前置定语与前置从句 [B8]；同句严禁"可能或许大致"多个不确定推测词堆叠 [C4]

禁止当作问题修改的内容（防误杀反清单）：
- 操作指导中的正文顺序步骤词（首先……其次……最后）、启发性设问句、原理解释的机制比喻、行业与工程技术名词（幂等、解耦等）。

底线：懂行同行的平视交流质感；惯例降低频率而非归零。
"""

_PLUGIN_EXTRAS = (
    "插件附加（不改变上述原则）：\n"
    "- 保留事实、限制条件、安全提示和不确定性表述\n"
    "- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先\n"
    "- 不要把这些约束写进回复\n"
    "- 铁律：先否定后肯定（不是/与其/看似/很久…久到）删否定留肯定，直接说肯定面；角色引号内除外\n"
    "- 铁律：日常对话严禁泛滥使用破折号（——）制造刻意停顿与揭晓"
)
_STABLE_MARKERS = frozenset((STABLE_RULE_MARKER,))
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_LEADING_SEPARATOR_RE = re.compile(r"^(?:(?:\r\n|\r|\n)){2}")
_TRAILING_SEPARATOR_RE = re.compile(r"(?:(?:\r\n|\r|\n)){2}$")


@dataclass(frozen=True)
class StableRewriteResult:
    text: str
    injected: bool
    removed: int
    ambiguous: bool


@dataclass(frozen=True)
class ContextRewriteResult:
    stable_removed: int = 0
    runtime_satisfied: bool = False
    runtime_removed: int = 0
    runtime_ambiguous: int = 0


def build_stable_rules() -> str:
    """稳定规则：natural-talk 轻量注入版（规范源：SKILL.md 日常对话与问答核心准绳）+ 插件附加条款。"""
    return f"{STABLE_RULE_MARKER}\n遵循 natural-talk 原则（natural-talk MIT）：\n\n{_LITE_CORE}\n\n{_PLUGIN_EXTRAS}"


def _signature(text: str) -> tuple[int, str]:
    normalized = _NEWLINE_RE.sub("\n", text)
    return len(normalized.splitlines()), hashlib.sha256(normalized.encode()).hexdigest()


_STABLE_SIGNATURES = {
    STABLE_RULE_MARKER: frozenset({_signature(build_stable_rules())}),
}


def rewrite_stable_rules(system_prompt: str | None, *, enabled: bool) -> StableRewriteResult:
    text = system_prompt if isinstance(system_prompt, str) else ""
    matches, current_present, ambiguous = _find_stable_blocks(text)
    current_kept = False
    removals: list[tuple[int, int]] = []

    for start, end, marker in matches:
        if marker == STABLE_RULE_MARKER and enabled and not current_kept:
            current_kept = True
            continue
        removals.append(_expand_stable_removal(text, start, end))

    if removals:
        text = _remove_spans(text, removals)

    injected = False
    if enabled and not current_present:
        rules = build_stable_rules()
        if text:
            newline = _first_newline(text)
            if text.endswith(newline * 2):
                separator = ""
            elif text.endswith(newline):
                separator = newline
            else:
                separator = newline * 2
            text = f"{text}{separator}{rules}"
        else:
            text = rules
        injected = True

    return StableRewriteResult(text, injected, len(removals), ambiguous)


def rewrite_context_injections(req: ProviderRequestProtocol, runtime_text: str | None) -> ContextRewriteResult:
    """清理历史注入块，并在当前请求 extra parts 中保留至多一个匹配提示。"""
    result = ContextRewriteResult()
    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        for ctx in contexts:
            if not isinstance(ctx, dict) or ctx.get("role") != "user":
                continue
            content = ctx.get("content")
            if isinstance(content, str):
                rewritten, item_result = _rewrite_history_text(content)
                if rewritten != content:
                    ctx["content"] = rewritten
                result = _merge_context_results(result, item_result)
            elif isinstance(content, list):
                rewritten, item_result = _rewrite_history_parts(content)
                if rewritten != content:
                    ctx["content"] = rewritten
                result = _merge_context_results(result, item_result)

    parts = getattr(req, "extra_user_content_parts", None)
    if isinstance(parts, list):
        rewritten, item_result = _rewrite_extra_parts(parts, runtime_text)
        if rewritten != parts:
            req.extra_user_content_parts = rewritten
        result = _merge_context_results(result, item_result)
    return result


def _normalize_newlines(text: str) -> str:
    return _NEWLINE_RE.sub("\n", text)


def _find_stable_blocks(text: str) -> tuple[list[tuple[int, int, str]], bool, bool]:
    """返回 (签名匹配的可剥离块, 当前版本块是否已存在, 是否含编辑过的当前块)。

    当前版本 marker：无论签名是否匹配都视为已注入（被用户编辑过的当前规则块保留，不重复注入）；
    签名匹配的块仍进 matches，由调用方决定保留首个还是剥离（关闭时清理）。
    """
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    matches: list[tuple[int, int, str]] = []
    current_present = False
    ambiguous = False
    for index, line in enumerate(lines):
        marker = line.rstrip("\r\n")
        if marker not in _STABLE_MARKERS:
            continue
        if marker == STABLE_RULE_MARKER:
            current_present = True
        matched = False
        for line_count, expected_hash in _STABLE_SIGNATURES.get(marker, ()):
            last = index + line_count - 1
            if last >= len(lines):
                continue
            end = starts[last] + len(lines[last].rstrip("\r\n"))
            candidate = _normalize_newlines(text[starts[index] : end])
            if hashlib.sha256(candidate.encode()).hexdigest() == expected_hash:
                matches.append((starts[index], end, marker))
                matched = True
                break
        if not matched:
            ambiguous = True
    return matches, current_present, ambiguous


def _expand_stable_removal(text: str, start: int, end: int) -> tuple[int, int]:
    before = text[:start]
    after = text[end:]
    preceding = _TRAILING_SEPARATOR_RE.search(before)
    if preceding:
        return preceding.start(), end
    if start == 0:
        following = _LEADING_SEPARATOR_RE.match(after)
        if following:
            return start, end + following.end()
    return start, end


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _first_newline(text: str) -> str:
    match = _NEWLINE_RE.search(text)
    return match.group(0) if match else "\n"


def _text_value(part: Any) -> str | None:
    value = getattr(part, "text", None)
    if value is None and isinstance(part, dict):
        value = part.get("text")
    return value if isinstance(value, str) else None


def _is_known_stable_text(text: str) -> bool:
    normalized = _normalize_newlines(text)
    lines = normalized.splitlines()
    if not lines:
        return False
    for line_count, expected_hash in _STABLE_SIGNATURES.get(lines[0], ()):
        if len(lines) == line_count and hashlib.sha256(normalized.encode()).hexdigest() == expected_hash:
            return True
    return False


def _runtime_kind(text: str) -> str:
    normalized = _normalize_newlines(text)
    lines = normalized.splitlines()
    if not lines or lines[0] != RUNTIME_HINT_MARKER:
        return "ordinary"
    if _is_complete_runtime(normalized):
        return "owned"
    return "ambiguous"


def _is_complete_runtime(text: str) -> bool:
    if not text.startswith(_RUNTIME_PREFIX):
        return False
    items = text[len(_RUNTIME_PREFIX) :].split(_RUNTIME_ITEM_SEPARATOR)
    return 1 <= len(items) <= MAX_AVOID_ITEMS and all(
        0 < len(item) <= MAX_AVOID_ITEM_LEN and "\n" not in item for item in items
    )


def _rewrite_history_text(text: str) -> tuple[str, ContextRewriteResult]:
    if _is_known_stable_text(text):
        return "", ContextRewriteResult(stable_removed=1)
    kind = _runtime_kind(text)
    if kind == "ordinary":
        return text, ContextRewriteResult()
    if kind == "ambiguous":
        return text, ContextRewriteResult(runtime_ambiguous=1)
    return "", ContextRewriteResult(runtime_removed=1)


def _rewrite_history_parts(parts: list[Any]) -> tuple[list[Any], ContextRewriteResult]:
    rewritten: list[Any] = []
    result = ContextRewriteResult()
    for part in parts:
        text = _text_value(part)
        if text is None:
            rewritten.append(part)
            continue
        replacement, flags = _rewrite_history_text(text)
        if replacement:
            rewritten.append(part)
        result = _merge_context_results(result, flags)
    return rewritten, result


def _rewrite_extra_parts(parts: list[Any], runtime_text: str | None) -> tuple[list[Any], ContextRewriteResult]:
    rewritten: list[Any] = []
    result = ContextRewriteResult()
    for part in parts:
        text = _text_value(part)
        if text is None:
            rewritten.append(part)
            continue
        if _is_known_stable_text(text):
            result = _merge_context_results(result, ContextRewriteResult(stable_removed=1))
            continue
        kind = _runtime_kind(text)
        if kind == "ordinary":
            rewritten.append(part)
            continue
        if kind == "ambiguous":
            rewritten.append(part)
            result = _merge_context_results(result, ContextRewriteResult(runtime_ambiguous=1))
            continue
        if (
            runtime_text
            and not result.runtime_satisfied
            and _normalize_newlines(text) == _normalize_newlines(runtime_text)
        ):
            rewritten.append(part)
            result = _merge_context_results(result, ContextRewriteResult(runtime_satisfied=True))
            continue
        flags = ContextRewriteResult(runtime_removed=1)
        result = _merge_context_results(result, flags)
    return rewritten, result


def _merge_context_results(left: ContextRewriteResult, right: ContextRewriteResult) -> ContextRewriteResult:
    return ContextRewriteResult(
        stable_removed=left.stable_removed + right.stable_removed,
        runtime_satisfied=left.runtime_satisfied or right.runtime_satisfied,
        runtime_removed=left.runtime_removed + right.runtime_removed,
        runtime_ambiguous=left.runtime_ambiguous + right.runtime_ambiguous,
    )


_SIGNAL_HINT_MAP: dict[str, str] = {
    "结构性表演": "先否定后肯定句式",
    "模糊叠加": "可能或许等推测词堆叠",
    "编号小标题连发": "机械编号列表",
    "然而连发": "连用然而",
    "路标词堆砌": "堆砌路标词",
    "破折号": "别用破折号（——），改用常规标点",
    "感叹号": "多用感叹号",
}


def build_runtime_hint(openers: Sequence[str], max_chars: int) -> str:
    # 超长自定义词不注入（record 入库侧已按 MAX_AVOID_ITEM_LEN 过滤，此处兜底旧状态文件里残留的超长词）
    openers = [item for item in openers[:MAX_AVOID_ITEMS] if item and len(item) <= MAX_AVOID_ITEM_LEN]
    if not openers:
        return ""

    prefix_len = len(_RUNTIME_PREFIX)
    sep_len = len(_RUNTIME_ITEM_SEPARATOR)
    selected: list[str] = []
    current_len = prefix_len
    for raw_item in openers:
        item = _SIGNAL_HINT_MAP.get(raw_item, raw_item)
        # 增量：分隔符（非首项）+ 当前项
        increment = (sep_len if selected else 0) + len(item)
        if current_len + increment > max_chars:
            break
        selected.append(item)
        current_len += increment
    return _RUNTIME_PREFIX + _RUNTIME_ITEM_SEPARATOR.join(selected) if selected else ""


def runtime_hint_items(text: str) -> tuple[str, ...]:
    normalized = _normalize_newlines(text)
    if not normalized.startswith(_RUNTIME_PREFIX):
        return ()
    payload = normalized[len(_RUNTIME_PREFIX) :]
    items = payload.split(_RUNTIME_ITEM_SEPARATOR)
    if not 1 <= len(items) <= MAX_AVOID_ITEMS:
        return ()
    if not all(0 < len(item) <= MAX_AVOID_ITEM_LEN and "\n" not in item for item in items):
        return ()
    return tuple(items)


def make_text_part(text: str, factory: TextPartFactoryProtocol | None = None) -> Any | None:
    """构造临时文本 part；factory 为 None 时返回 None（调用方自行降级）。

    TextPart 暂无 astrbot.api 公开导出，探测由 Core 构造时完成并注入 factory。
    provider 对未知 part 类型会直接抛错，故失败时不得产出伪 part。
    """
    if factory is None:
        return None
    try:
        return factory(text=text)
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] make_text_part failed: {e}")
        return None


def append_temp_text_part(
    req: ProviderRequestProtocol,
    text: str,
    factory: TextPartFactoryProtocol | None = None,
    *,
    marker: str | None = None,
) -> bool:
    """构造并追加 temp extra；去重和历史判定由 rewrite_context_injections 负责。

    契约：注入文本必须以 marker 开头（幂等的前提），违反时拒绝注入并告警。
    历史 contexts 的幂等与所有权判定由调用方 rewrite_context_injections 负责。
    """
    if not text.strip():
        return False
    if marker and not text.lstrip().startswith(marker):
        if logger is not None:
            logger.warning(f"[HumanChatQuality] injected text missing marker prefix: {marker!r}")
        return False
    try:
        part = make_text_part(text, factory)
        if part is None:
            return False
        if not hasattr(req, "extra_user_content_parts") or req.extra_user_content_parts is None:
            req.extra_user_content_parts = []
        parts = req.extra_user_content_parts
        if not isinstance(parts, list):
            return False
        parts.append(part)
        return True
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] append temp text part failed: {e}")
        return False
