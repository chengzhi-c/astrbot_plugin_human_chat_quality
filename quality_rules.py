from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

from .runtime_state import MAX_AVOID_OPENERS, MAX_OPEN_LEN, SessionState


# 所有注入 marker 的公共前缀
INJECTED_MARKER_PREFIX = "[Human Chat Quality"
# 规则版本：升级 natural-talk 时 +1；旧版本随 LEGACY 推导保留，保证旧块可剥离
RULES_VERSION = 5
STABLE_RULE_MARKER = f"{INJECTED_MARKER_PREFIX} Rules v{RULES_VERSION}]"
# 历史版本注入过的规则标记（含无版本号形态，显式保留）；
# 用于剥离 system_prompt/历史中残留的旧规则块（startswith 判定不会误伤当前 marker 自身）
LEGACY_STABLE_MARKERS: tuple[str, ...] = (f"{INJECTED_MARKER_PREFIX} Rules]",) + tuple(
    f"{INJECTED_MARKER_PREFIX} Rules v{i}]" for i in range(1, RULES_VERSION)
)
RUNTIME_HINT_MARKER = f"{INJECTED_MARKER_PREFIX} Runtime]"
_RUNTIME_INSTRUCTION = "本轮避开这些重复项，换种自然说法，别提本提示："
_RUNTIME_PREFIX = f"{RUNTIME_HINT_MARKER}\n{_RUNTIME_INSTRUCTION}\n"
_LEGACY_RUNTIME_PREFIX = (
    f"{RUNTIME_HINT_MARKER}\n"
    "仅用于本轮回复的轻量状态：这些开头或说法最近已出现过，本轮换个自然说法，别再用，也别提到这条提示。\n"
)
_RUNTIME_ITEM_SEPARATOR = "、"
MIN_RUNTIME_HINT_CHARS = 80
MAX_RUNTIME_HINT_CHARS = (
    len(_RUNTIME_PREFIX) + MAX_AVOID_OPENERS * MAX_OPEN_LEN + (MAX_AVOID_OPENERS - 1) * len(_RUNTIME_ITEM_SEPARATOR)
)

# 已发布上游提交中的完整规则签名。正文留在测试夹具，运行时只保留 marker、行数和 hash。
# 注：v3 从未公开发布，无签名，保留 unknown=ambiguous 行为；v4 为 1.1.x 已发布块，必须可剥离。
_LEGACY_STABLE_SIGNATURES: dict[str, frozenset[tuple[int, str]]] = {
    f"{INJECTED_MARKER_PREFIX} Rules v1]": frozenset(
        {
            (7, "a418be2384020a69e089f10ccf92a595121cc912f7a4d6ac134c3870ce33af44"),
            (11, "cf703f9e2436a2e2f676c386f3e2673a6ac9c61e769268f411e96fcb16166aa2"),
        }
    ),
    f"{INJECTED_MARKER_PREFIX} Rules v2]": frozenset(
        {
            (39, "9f27e5df3f368f9cdc8ff0c2cd6bfc075365af0024dfe67c8ed3a21374d2fa82"),
            (7, "c33073fcaaca430cba3ab648f7a8df8bdf1c85b6c1d7c71025ce53896771e731"),
        }
    ),
    f"{INJECTED_MARKER_PREFIX} Rules v4]": frozenset(
        {
            (25, "c7787f6c38c128dab5b3781365516257af5a35f915766851b870828cd97e3f8f"),
        }
    ),
}
_STABLE_MARKERS = frozenset((*LEGACY_STABLE_MARKERS, STABLE_RULE_MARKER))
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_LEADING_SEPARATOR_RE = re.compile(r"^(?:(?:\r\n|\r|\n)){2}")
_TRAILING_SEPARATOR_RE = re.compile(r"(?:(?:\r\n|\r|\n)){2}$")


@dataclass(frozen=True)
class StableRewriteResult:
    text: str
    injected: bool
    removed: bool
    ambiguous: bool


@dataclass(frozen=True)
class ContextRewriteResult:
    stable_removed: bool = False
    runtime_satisfied: bool = False
    runtime_replaced: bool = False
    runtime_removed: bool = False
    runtime_ambiguous: bool = False


def build_stable_rules() -> str:
    """稳定规则：natural-talk（MIT）"作为 System Prompt"章节原文 + 插件附加条款。

    natural-talk 部分逐字引用其官方浓缩版（仅首行追加来源标注，MIT 要求保留版权说明）：
    正文为 v2.1.0 lite 模板，另含上游 506407f 起新增的"不适用范围"行；
    "插件附加"为插件原有的安全条款（保留事实/任务优先/不写入回复），与 natural-talk 原则无冲突。
    """
    return (
        f"{STABLE_RULE_MARKER}\n"
        "遵循 natural-talk 原则（natural-talk v2.1.0+，MIT）：\n"
        "\n"
        "核心：\n"
        "- 直接回答，零开场零收尾，最多留一句有效过渡\n"
        "- 不知道就说不知道，不编造\n"
        "- 像朋友聊天，不像客服或老师\n"
        "\n"
        "禁止：\n"
        '- "作为AI" / "希望帮助你" / "好问题"（全文最多 1 次）\n'
        '- "让我来" / "首先其次最后" / "综上所述"（全文最多 1 次）\n'
        '- "值得注意" / "事实上" 等路标词（全文不超过 2 次）\n'
        "- 评判对方 / 替对方做心理判断\n"
        "- 破折号（全文不超过 2 次）\n"
        "\n"
        "要求：\n"
        "- 句子长短交替，不匀速\n"
        '- 能用"是/有"就不绕\n'
        "- 主动语态，真实主语\n"
        "- 具体表达，删除空泛词\n"
        "\n"
        "不适用范围：学术润色、正式公文、营销文案等需要相反风格的场景，本规则让位。\n"
        "\n"
        "插件附加（不改变上述原则）：\n"
        "- 保留事实、限制条件、安全提示和不确定性表述\n"
        "- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先\n"
        "- 不要把这些约束写进回复"
    )


def _signature(text: str) -> tuple[int, str]:
    normalized = _NEWLINE_RE.sub("\n", text)
    return len(normalized.splitlines()), hashlib.sha256(normalized.encode()).hexdigest()


_STABLE_SIGNATURES = {
    **_LEGACY_STABLE_SIGNATURES,
    STABLE_RULE_MARKER: frozenset({_signature(build_stable_rules())}),
}


def inject_stable_rules(system_prompt: str | None) -> str:
    """兼容旧调用：严格迁移已知块并确保当前规则至多一份。"""
    return rewrite_stable_rules(system_prompt, enabled=True).text


def rewrite_stable_rules(system_prompt: str | None, *, enabled: bool) -> StableRewriteResult:
    text = system_prompt if isinstance(system_prompt, str) else ""
    matches, ambiguous = _find_stable_blocks(text)
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
    if enabled and not current_kept and not ambiguous:
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

    return StableRewriteResult(text, injected, bool(removals), ambiguous)


def rewrite_context_injections(req: Any, runtime_text: str | None) -> ContextRewriteResult:
    result = ContextRewriteResult()
    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        for ctx in contexts:
            if not isinstance(ctx, dict) or ctx.get("role") != "user":
                continue
            content = ctx.get("content")
            if isinstance(content, str):
                rewritten, item_result = _rewrite_context_text(content, runtime_text, result.runtime_satisfied)
                if rewritten != content:
                    ctx["content"] = rewritten
                result = _merge_context_results(result, item_result)
            elif isinstance(content, list):
                rewritten, item_result = _rewrite_context_parts(content, runtime_text, result.runtime_satisfied)
                if rewritten != content:
                    ctx["content"] = rewritten
                result = _merge_context_results(result, item_result)

    parts = getattr(req, "extra_user_content_parts", None)
    if isinstance(parts, list):
        rewritten, item_result = _rewrite_context_parts(parts, runtime_text, result.runtime_satisfied)
        if rewritten != parts:
            req.extra_user_content_parts = rewritten
        result = _merge_context_results(result, item_result)
    return result


def _normalize_newlines(text: str) -> str:
    return _NEWLINE_RE.sub("\n", text)


def _find_stable_blocks(text: str) -> tuple[list[tuple[int, int, str]], bool]:
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    matches: list[tuple[int, int, str]] = []
    ambiguous = False
    for index, line in enumerate(lines):
        marker = line.rstrip("\r\n")
        if marker not in _STABLE_MARKERS:
            continue
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
    return matches, ambiguous


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
    if not normalized.splitlines() or normalized.splitlines()[0] != RUNTIME_HINT_MARKER:
        return "ordinary"
    if _is_complete_runtime(normalized) or _is_legacy_truncated_runtime(normalized):
        return "owned"
    return "ambiguous"


def _is_complete_runtime(text: str) -> bool:
    for prefix in (_RUNTIME_PREFIX, _LEGACY_RUNTIME_PREFIX):
        if text.startswith(prefix):
            items = text[len(prefix) :].split(_RUNTIME_ITEM_SEPARATOR)
            return 1 <= len(items) <= MAX_AVOID_OPENERS and all(
                0 < len(item) <= MAX_OPEN_LEN and "\n" not in item for item in items
            )
    return False


def _is_legacy_truncated_runtime(text: str) -> bool:
    if not 80 <= len(text) <= 182 or not text.endswith("..."):
        return False
    prefix = text[:-3]
    if _LEGACY_RUNTIME_PREFIX.startswith(prefix):
        return True
    if not prefix.startswith(_LEGACY_RUNTIME_PREFIX):
        return False
    payload = prefix[len(_LEGACY_RUNTIME_PREFIX) :]
    items = payload.split(_RUNTIME_ITEM_SEPARATOR)
    return 1 <= len(items) <= MAX_AVOID_OPENERS and all(
        (0 < len(item) <= MAX_OPEN_LEN if index < len(items) - 1 else len(item) <= MAX_OPEN_LEN) and "\n" not in item
        for index, item in enumerate(items)
    )


def _rewrite_context_text(
    text: str, runtime_text: str | None, already_satisfied: bool
) -> tuple[str, ContextRewriteResult]:
    if _is_known_stable_text(text):
        return "", ContextRewriteResult(stable_removed=True)
    kind = _runtime_kind(text)
    if kind == "ordinary":
        return text, ContextRewriteResult()
    if kind == "ambiguous":
        return text, ContextRewriteResult(runtime_ambiguous=True)
    if not runtime_text:
        return "", ContextRewriteResult(runtime_removed=True)
    if already_satisfied:
        return "", ContextRewriteResult(runtime_removed=True)
    if _normalize_newlines(text) == _normalize_newlines(runtime_text):
        return text, ContextRewriteResult(runtime_satisfied=True)
    return runtime_text, ContextRewriteResult(runtime_satisfied=True, runtime_replaced=True)


def _rewrite_context_parts(
    parts: list[Any], runtime_text: str | None, already_satisfied: bool
) -> tuple[list[Any], ContextRewriteResult]:
    rewritten: list[Any] = []
    result = ContextRewriteResult(runtime_satisfied=already_satisfied)
    for part in parts:
        text = _text_value(part)
        if text is None:
            rewritten.append(part)
            continue
        if _is_known_stable_text(text):
            result = _merge_context_results(result, ContextRewriteResult(stable_removed=True))
            continue
        kind = _runtime_kind(text)
        if kind == "ordinary":
            rewritten.append(part)
            continue
        if kind == "ambiguous":
            rewritten.append(part)
            result = _merge_context_results(result, ContextRewriteResult(runtime_ambiguous=True))
            continue
        if not runtime_text or result.runtime_satisfied:
            result = _merge_context_results(result, ContextRewriteResult(runtime_removed=True))
            continue
        if _normalize_newlines(text) == _normalize_newlines(runtime_text):
            rewritten.append(part)
            result = _merge_context_results(result, ContextRewriteResult(runtime_satisfied=True))
            continue
        rewritten.append({"type": "text", "text": runtime_text})
        result = _merge_context_results(result, ContextRewriteResult(runtime_satisfied=True, runtime_replaced=True))
    return rewritten, result


def _merge_context_results(left: ContextRewriteResult, right: ContextRewriteResult) -> ContextRewriteResult:
    return ContextRewriteResult(
        stable_removed=left.stable_removed or right.stable_removed,
        runtime_satisfied=left.runtime_satisfied or right.runtime_satisfied,
        runtime_replaced=left.runtime_replaced or right.runtime_replaced,
        runtime_removed=left.runtime_removed or right.runtime_removed,
        runtime_ambiguous=left.runtime_ambiguous or right.runtime_ambiguous,
    )


def build_runtime_hint(state: SessionState, max_chars: int) -> str:
    # 超长自定义词不注入（record 入库侧已按 MAX_OPEN_LEN 过滤，此处兜底旧状态文件里残留的超长词）
    openers = [item for item in state.avoid_openers[:MAX_AVOID_OPENERS] if item and len(item) <= MAX_OPEN_LEN]
    if not openers:
        return ""

    selected: list[str] = []
    for item in openers:
        candidate = _RUNTIME_PREFIX + _RUNTIME_ITEM_SEPARATOR.join([*selected, item])
        if len(candidate) > max_chars:
            break
        selected.append(item)
    return _RUNTIME_PREFIX + _RUNTIME_ITEM_SEPARATOR.join(selected) if selected else ""


def make_text_part(text: str, factory: Any | None = None) -> Any | None:
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
    req: Any,
    text: str,
    factory: Any | None = None,
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
