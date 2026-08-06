from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

try:
    from .runtime_state import MAX_AVOID_OPENERS, MAX_OPEN_LEN, SessionState
except ImportError:  # pragma: no cover
    from runtime_state import MAX_AVOID_OPENERS, MAX_OPEN_LEN, SessionState


# 所有注入 marker 的公共前缀
INJECTED_MARKER_PREFIX = "[Human Chat Quality"
STABLE_RULE_MARKER = f"{INJECTED_MARKER_PREFIX} Rules v2]"
RUNTIME_HINT_MARKER = f"{INJECTED_MARKER_PREFIX} Runtime]"


def build_stable_rules() -> str:
    """稳定规则（v0.6.0）：五类短约束，只定基调，不做禁句清单。"""
    return (
        f"{STABLE_RULE_MARKER}\n"
        "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
        "1. 日常闲聊顺着对方的话自然回应，避免客服式收尾、空泛鼓励和无信息增量的总结。\n"
        "2. 保留事实、限制条件、安全提示和不确定性表述，不为口语化而删减。\n"
        "3. 用户明确要求技术步骤、对比、正式文稿或清单时，以任务完成为先，允许精确结构与术语。\n"
        "4. 不知道就直说，不用空泛免责声明掩盖不确定性。\n"
        "5. 不要把这些约束写进回复。"
    )


def inject_stable_rules(system_prompt: str | None) -> str:
    """幂等拼入 system_prompt（marker 防重复）。v0.6.0 起稳定规则固定走此通道。"""
    prompt = system_prompt or ""
    if STABLE_RULE_MARKER in prompt:
        return prompt
    rules = build_stable_rules()
    return f"{prompt.rstrip()}\n\n{rules}" if prompt.strip() else rules


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


# TextPart 探测三态缓存：只在首次使用时探测一次，避免每次调用重复 import + 刷 warning。
# 决策依据：探测失败说明环境不兼容（进程内不会自愈），后续直接禁用 temp extra 注入；
# 热更新若重新加载本模块则重置缓存；否则需手动重装插件恢复探测（不承诺宿主行为）。
_TEXTPART_CLS: Any = None
_TEXTPART_PROBED = False


def _get_text_part_cls() -> Any:
    global _TEXTPART_CLS, _TEXTPART_PROBED
    if not _TEXTPART_PROBED:
        _TEXTPART_PROBED = True
        try:
            from astrbot.core.agent.message import TextPart

            _TEXTPART_CLS = TextPart
        except Exception as e:
            if logger is not None:
                logger.warning(f"[HumanChatQuality] TextPart unavailable, temp extra injection disabled: {e}")
    return _TEXTPART_CLS


def make_text_part(text: str, factory: Any | None = None) -> Any | None:
    """构造临时文本 part；构造失败返回 None（调用方自行降级）。

    TextPart 暂无 astrbot.api 公开导出，走 core.agent.message 内部路径。
    provider 对未知 part 类型会直接抛错，故失败时不得产出伪 part。
    4.23 起保存链路只看消息级 _no_save，part 级临时标记无意义，不再设置。
    """
    if factory is not None:
        return factory(text)
    text_part_cls = _get_text_part_cls()
    if text_part_cls is None:
        return None
    return text_part_cls(text=text)


def part_has_marker(part: Any, marker: str) -> bool:
    text_val = getattr(part, "text", None)
    if text_val is None and isinstance(part, dict):
        text_val = part.get("text")
    return isinstance(text_val, str) and marker in text_val


def scan_request_markers(req: Any, markers: tuple[str, ...]) -> set[str]:
    """单次遍历 system_prompt / extra_user_content_parts / 历史 user contexts，返回命中的 marker 集合。

    生产热路径每轮只需一次 O(历史) 扫描（4.23+ 注入块入历史后历史持续增长）。
    只检查 user 消息：注入块只会出现在 user 消息里，模型复述/用户手打
    marker 到 assistant/system 消息不应误停注入。
    """
    found: set[str] = set()
    if not markers:
        return found
    try:
        sp = getattr(req, "system_prompt", None) or ""
        if isinstance(sp, str):
            for marker in markers:
                if marker in sp:
                    found.add(marker)
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] scan_request_markers system_prompt check failed: {e}")
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            for part in parts:
                for marker in markers:
                    if part_has_marker(part, marker):
                        found.add(marker)
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] scan_request_markers parts check failed: {e}")
    try:
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            for ctx in contexts:
                if not isinstance(ctx, dict) or ctx.get("role") != "user":
                    continue
                content = ctx.get("content")
                if isinstance(content, str):
                    for marker in markers:
                        if marker in content:
                            found.add(marker)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            for marker in markers:
                                if marker in part["text"]:
                                    found.add(marker)
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] scan_request_markers contexts check failed: {e}")
    return found


def _marker_in_req(req: Any, marker: str) -> bool:
    """仅检查 system_prompt 与本请求 temp parts（不扫历史 contexts）。

    历史级幂等由调用方（on_llm_request 的单次扫描）负责；
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


def replace_marker_in_contexts(req: Any, marker: str, new_text: str) -> bool:
    """在 contexts 的 user 消息里把旧注入块统一为最新块（动态提示每轮更新用）。

    4.23.3 实测：on_llm_request hook 早于 runner.reset() 执行，reset 才把
    contexts 深拷贝进 run_context.messages 并随本轮保存落库——故 hook 内对
    contexts 的原位改写会随本次保存写回历史（替换后的块入库，历史不累积）。
    - list 形态（4.23+ 实际形态）：首个命中替换为 new_text，其余命中丢弃，
      历史收敛为至多一个最新块（异常多块状态可自愈）；
    - 找不到（首轮）：返回 False，调用方应走 append 注入。
    """
    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return False
    replaced = False
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if not isinstance(content, list):
            continue
        kept: list[Any] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and marker in part["text"]:
                if not replaced:
                    kept.append({"type": "text", "text": new_text})
                    replaced = True
                # 其余命中丢弃：历史收敛为至多一个最新块（防多块累积）
                continue
            kept.append(part)
        try:
            ctx["content"] = kept
        except Exception as e:
            # 异常形态（如只读 content）：保留原内容、跳过该 ctx，不影响其它 ctx
            if logger is not None:
                logger.debug(f"[HumanChatQuality] contexts content rewrite skipped: {e}")
    return replaced


def remove_marker_in_contexts(req: Any, marker: str) -> bool:
    """从 contexts 的 user 消息里安全删除含 marker 的 part（动态块清空/规则迁移用）。

    - list 形态（4.23+ 实际形态）：删除含 marker 的 dict part，其余 part（用户原话、
      图片等）原位保留；删除过则返回 True（改写随本次保存写回历史，同 replace）；
    - str 形态含 marker：不做不安全的字符串切割（可能误删用户内容），返回 False；
    - 找不到 marker：返回 False。
    """
    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return False
    removed = False
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if not isinstance(content, list):
            continue
        kept = [
            part
            for part in content
            if not (isinstance(part, dict) and isinstance(part.get("text"), str) and marker in part["text"])
        ]
        if len(kept) != len(content):
            try:
                ctx["content"] = kept
                removed = True
            except Exception as e:
                # 异常形态（如只读 content）：保留原内容、跳过该 ctx，不影响其它 ctx
                if logger is not None:
                    logger.debug(f"[HumanChatQuality] contexts content rewrite skipped: {e}")
    return removed


def append_temp_text_part(
    req: Any,
    text: str,
    factory: Any | None = None,
    *,
    marker: str | None = None,
) -> bool:
    """写入 temp extra；缺失 list 时创建。marker 已存在（system_prompt 或本请求已有 part）则跳过。

    契约：注入文本必须以 marker 开头（幂等的前提），违反时拒绝注入并告警。
    历史 contexts 含 marker 时本函数仍会写入——历史级幂等由调用方的单次扫描负责。
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
