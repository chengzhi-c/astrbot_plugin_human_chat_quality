from __future__ import annotations

from typing import Any, Literal

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

from .runtime_state import MAX_AVOID_OPENERS, MAX_OPEN_LEN, SessionState


# 所有注入 marker 的公共前缀
INJECTED_MARKER_PREFIX = "[Human Chat Quality"
STABLE_RULE_MARKER = f"{INJECTED_MARKER_PREFIX} Rules v4]"
# 历史版本注入过的规则标记（含无版本号形态）；
# 用于剥离 system_prompt/历史中残留的旧规则块（startswith 判定不会误伤当前 marker 自身）
LEGACY_STABLE_MARKERS: tuple[str, ...] = (
    f"{INJECTED_MARKER_PREFIX} Rules]",
    f"{INJECTED_MARKER_PREFIX} Rules v1]",
    f"{INJECTED_MARKER_PREFIX} Rules v2]",
    f"{INJECTED_MARKER_PREFIX} Rules v3]",
)
RUNTIME_HINT_MARKER = f"{INJECTED_MARKER_PREFIX} Runtime]"

# 三态返回契约（跨模块静态类型检查）
MarkerResult = Literal["modified", "absent", "str_blocked"]
MARKER_MODIFIED: MarkerResult = "modified"
MARKER_ABSENT: MarkerResult = "absent"
MARKER_STR_BLOCKED: MarkerResult = "str_blocked"


def build_stable_rules() -> str:
    """稳定规则：natural-talk v2.1.0（MIT）"作为 System Prompt"章节原文 + 插件附加条款。

    natural-talk 部分逐字引用其官方浓缩版（仅首行追加来源标注，MIT 要求保留版权说明）；
    "插件附加"为插件原有的安全条款（保留事实/任务优先/不写入回复），与 natural-talk 原则无冲突。
    """
    return (
        f"{STABLE_RULE_MARKER}\n"
        "遵循 natural-talk 原则（natural-talk v2.1.0，MIT）：\n"
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
        "插件附加（不改变上述原则）：\n"
        "- 保留事实、限制条件、安全提示和不确定性表述\n"
        "- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先\n"
        "- 不要把这些约束写进回复"
    )


def inject_stable_rules(system_prompt: str | None) -> str:
    """幂等拼入 system_prompt（marker 防重复）。非 str 视作空 prompt，不抛异常。"""
    if not isinstance(system_prompt, str):
        system_prompt = ""
    # 先剥离历史旧规则块再判幂等：避免升级后新旧并存（含多版本同存场景）
    system_prompt = _strip_legacy_stable_blocks(system_prompt)
    if STABLE_RULE_MARKER in system_prompt:
        return system_prompt
    rules = build_stable_rules()
    return f"{system_prompt.rstrip()}\n\n{rules}" if system_prompt.strip() else rules


def _strip_legacy_stable_blocks(system_prompt: str) -> str:
    """按段落剥离以 legacy 规则标记开头的块（注入格式恒为独立段落，不切割正文）。"""
    blocks = system_prompt.split("\n\n")
    kept = [block for block in blocks if not any(block.lstrip().startswith(marker) for marker in LEGACY_STABLE_MARKERS)]
    return "\n\n".join(kept)


def build_runtime_hint(state: SessionState, max_chars: int) -> str:
    # 超长自定义词不注入（record 入库侧已按 MAX_OPEN_LEN 过滤，此处兜底旧状态文件里残留的超长词）
    openers = [item for item in state.avoid_openers[:MAX_AVOID_OPENERS] if item and len(item) <= MAX_OPEN_LEN]
    if not openers:
        return ""

    hint = (
        f"{RUNTIME_HINT_MARKER}\n"
        "仅用于本轮回复的轻量状态：这些开头或说法最近已出现过，本轮换个自然说法，别再用，也别提到这条提示。\n"
        + "、".join(openers)
    )
    return clip_text(hint, max_chars)


def clip_text(text: str, max_chars: int) -> str:
    """截断到 max_chars 字符，超长以省略号收尾。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3].rstrip() + "..."


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


def part_has_marker(part: Any, marker: str) -> bool:
    """对象 part 或 dict part 是否含 marker（历史 list 与 temp parts 共用）。"""
    text_val = getattr(part, "text", None)
    if text_val is None and isinstance(part, dict):
        text_val = part.get("text")
    return isinstance(text_val, str) and marker in text_val


def _marker_in_req(req: Any, marker: str) -> bool:
    """仅检查 system_prompt 与本请求 temp parts（不扫历史 contexts）。

    历史级幂等由调用方 apply_context_marker 负责；
    本守卫只防同一请求内重复追加。
    """
    try:
        sp = getattr(req, "system_prompt", None) or ""
        if isinstance(sp, str) and marker in sp:
            return True
    except Exception as e:
        if logger is not None:
            logger.debug(f"[HumanChatQuality] _marker_in_req system_prompt check failed: {e}")
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            return any(part_has_marker(part, marker) for part in parts)
    except Exception as e:
        if logger is not None:
            logger.debug(f"[HumanChatQuality] _marker_in_req parts check failed: {e}")
    return False


def apply_context_marker(req: Any, marker: str, new_text: str | None) -> MarkerResult:
    """单趟处理 contexts 中 user 消息的 marker（热路径唯一历史遍历入口）。

    new_text 非空：list 形态首个命中替换为 new_text，其余命中丢弃（多块自愈）；
    new_text 为空：list 形态删除含 marker 的 part，保留用户原话/图片等；
    str 形态含 marker：不做不安全切割，返回 str_blocked（调用方不得再 append）。

    返回：modified | absent | str_blocked

    4.23.3：on_llm_request 早于 runner.reset()，此处原位改写会随本轮保存写回历史。
    """
    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return "absent"

    did_operation = False
    want_replace = bool(new_text)
    replaced_once = False
    saw_str_marker = False

    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            if marker in content:
                saw_str_marker = True
            continue
        if not isinstance(content, list):
            continue

        if want_replace and not replaced_once:
            kept: list[Any] = []
            for part in content:
                if part_has_marker(part, marker):
                    if not replaced_once:
                        kept.append({"type": "text", "text": new_text})
                        replaced_once = True
                        did_operation = True
                    continue
                kept.append(part)
            if replaced_once:
                try:
                    ctx["content"] = kept
                except Exception as e:
                    if logger is not None:
                        logger.debug(f"[HumanChatQuality] contexts content rewrite skipped: {e}")
        else:
            kept = [part for part in content if not part_has_marker(part, marker)]
            if len(kept) != len(content):
                try:
                    ctx["content"] = kept
                    did_operation = True
                except Exception as e:
                    if logger is not None:
                        logger.debug(f"[HumanChatQuality] contexts content rewrite skipped: {e}")

    if did_operation:
        return MARKER_MODIFIED
    if saw_str_marker:
        return MARKER_STR_BLOCKED
    return MARKER_ABSENT


def append_temp_text_part(
    req: Any,
    text: str,
    factory: Any | None = None,
    *,
    marker: str | None = None,
) -> bool:
    """写入 temp extra；缺失 list 时创建。marker 已存在（system_prompt 或本请求已有 part）则跳过。

    契约：注入文本必须以 marker 开头（幂等的前提），违反时拒绝注入并告警。
    历史 contexts 含 marker 时本函数仍会写入——历史级幂等由调用方 apply_context_marker 负责。
    """
    if not text.strip():
        return False
    if marker and not text.lstrip().startswith(marker):
        if logger is not None:
            logger.warning(f"[HumanChatQuality] injected text missing marker prefix: {marker!r}")
        return False
    if marker and _marker_in_req(req, marker):
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
