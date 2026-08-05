from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore

try:
    from .runtime_state import SessionState
except ImportError:  # pragma: no cover
    from runtime_state import SessionState


# 所有注入 marker 的公共前缀（voice_profile 用它把注入文本排除出风格样本）
INJECTED_MARKER_PREFIX = "[Human Chat Quality"
STABLE_RULE_MARKER = f"{INJECTED_MARKER_PREFIX} Rules v2]"
RUNTIME_HINT_MARKER = f"{INJECTED_MARKER_PREFIX} Runtime]"


def build_stable_rules() -> str:
    return (
        f"{STABLE_RULE_MARKER}\n"
        "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
        "这是日常聊天，不是写报告。顺着对方的话自然接，别把闲聊答成讲义、作文或客服工单。\n"
        "\n"
        "【铁律】以下任何一条出现都必须改掉：\n"
        "1. 客服式收尾：\"希望这能帮到你\"\"如果还有问题随时问我\"。\n"
        "2. \"不是…而是…\"\"不仅…更是…\"排比句式。\n"
        "3. \"首先…其次…最后\"\"第一…第二…第三\"式清单骨架。\n"
        "4. 升华/鼓励式结尾：\"未来可期\"\"一起加油\"\"让我们共同努力\"。\n"
        "\n"
        "【词汇】这些词出现即换人话：\n"
        "- 值得注意的是/值得一提的是/需要注意的是 → 直接说内容\n"
        "- 总的来说/综上所述/总而言之 → 直接说结论\n"
        "- 众所周知/不可否认/毋庸置疑 → 直接陈述\n"
        "- 此外/与此同时/由此可见 → 删掉或换“另外/所以”\n"
        "- 深入探讨/深入分析/深度解析 → 聊聊/看看/说说\n"
        "- 赋能/闭环/抓手/沉淀/对齐/颗粒度 → 换大白话\n"
        "- 作为 AI/作为人工智能 → 别强调身份，直接说\n"
        "\n"
        "【结构】\n"
        "- 别排比凑数：三个以上相似短语并列（\"有温度、有深度、有力度\"）→ 挑一个说透\n"
        "- 别破折号连发：一条回复里出现两个以上\"——\"→ 换逗号句号或删掉\n"
        "- 别自问自答：\"你可能会问：为什么？\" → 直接讲\n"
        "- 别过度条列：日常闲聊尽量不列 1.2.3.\n"
        "\n"
        "【沟通】\n"
        "- 别客服腔开场：\"好问题！\"\"你说得太对了！\"→ 直接回应内容\n"
        "- 别复读对方原话、别每轮总结、别铺垫信号词（\"需要注意的是\"\"有趣的是\"\"事实上\"）\n"
        "- 别免责声明腔：\"根据我的知识截止日期…\"除非真的不确定，那就直接说不确定\n"
        "\n"
        "【像个人】\n"
        "- 句长要有变化，全是长句像念稿\n"
        "- 有观点，对事实做反应，别中立地列举利弊\n"
        "- 承认复杂：\"这事我也说不准\"比\"从多个维度看\"像人话\n"
        "- 不知道就直说：\"没查过，不敢乱说\"\"这个我不确定\"比硬答或编造像人话；别说\"根据我的知识截止日期\"这类免责声明腔\n"
        "- 口语化不等于丢信息：关键信息、限制条件、安全边界必须保留\n"
        "\n"
        "【自查】生成前心里过一遍，别把这条写进回复：\n"
        "有没有客服腔收尾？有没有\"不是…而是\"/\"首先…其次\"/\"不仅…更是\"？有没有两个以上破折号？有没有排比三连或金句？句长是不是全一样？有没有该说不知道却硬答的？有就改掉。"
    )


def inject_stable_rules(system_prompt: str | None) -> str:
    """幂等拼入 system_prompt（marker 防重复）。新路径优先走 append_temp_text_part。"""
    prompt = system_prompt or ""
    if STABLE_RULE_MARKER in prompt:
        return prompt
    rules = build_stable_rules()
    return f"{prompt.rstrip()}\n\n{rules}" if prompt.strip() else rules


def build_runtime_hint(state: SessionState, max_chars: int) -> str:
    # 超长自定义词（>20 字）不注入，避免被截断成半截短语
    openers = [item for item in state.avoid_openers[:5] if item and len(item) <= 20]
    if not openers:
        return ""

    hint = (
        f"{RUNTIME_HINT_MARKER}\n"
        "仅用于本轮回复的轻量状态：这些开头或说法最近已出现过，本轮换个自然说法，别再用，也别提到这条提示。\n"
        + "、".join(openers)
    )
    return _clip(hint, max_chars)


def _clip(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3].rstrip() + "..."


# TextPart 探测三态缓存：只在首次使用时探测一次，避免每次调用重复 import + 刷 warning。
# 决策依据：探测失败说明环境不兼容（进程内不会自愈），后续直接走 system 回退；
# AstrBot 热更新会重新加载模块、重置缓存。
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
                logger.warning(
                    f"[HumanChatQuality] TextPart unavailable, temp extra injection disabled: {e}"
                )
    return _TEXTPART_CLS


def make_text_part(text: str, factory: Any | None = None) -> Any | None:
    """构造一个临时文本 part，标记为不保存。

    TextPart 暂无 astrbot.api 公开导出，走 core.agent.message 内部路径；
    构造失败时返回 None（调用方应回退 system_prompt 注入，而不是带上
    无法被 provider 消费的 part——provider 对未知 part 类型会直接抛错）。
    兼容两代 AstrBot：4.16 有 mark_as_temp（返回 self 并置 _no_save）；
    4.23 起移除，改为直接 setattr（旧版本生效，新版本由 contexts 扫描兜底）。
    """
    if factory is not None:
        part = factory(text)
        if part is None:
            return None
    else:
        text_part_cls = _get_text_part_cls()
        if text_part_cls is None:
            return None
        part = text_part_cls(text=text)

    mark_as_temp = getattr(part, "mark_as_temp", None)
    if callable(mark_as_temp):
        marked = mark_as_temp()
        return marked if marked is not None else part

    try:
        # 4.23+ 无 mark_as_temp：setattr 仅写入实例 __dict__（pydantic 允许，dump 不带出），
        # 该标记对 4.23+ 无效，由 request_has_marker 的 contexts 扫描兜底防累积
        setattr(part, "_no_save", True)
    except Exception:
        pass
    return part


def part_has_marker(part: Any, marker: str) -> bool:
    text_val = getattr(part, "text", None)
    if text_val is None and isinstance(part, dict):
        text_val = part.get("text")
    return isinstance(text_val, str) and marker in text_val


def request_has_marker(req: Any, marker: str) -> bool:
    try:
        sp = getattr(req, "system_prompt", None) or ""
        if marker in sp:
            return True
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] request_has_marker system_prompt check failed: {e}")
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            return any(part_has_marker(part, marker) for part in parts)
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] request_has_marker parts check failed: {e}")
    try:
        # 历史 contexts 兜底：AstrBot >=4.23 移除 part 级 _no_save 标记，
        # 注入文本会随 user 消息进入历史；历史中已有 marker 时不再重复注入，
        # 避免规则块逐轮累积（4.16 上注入永不入历史，此处恒不命中，行为不变）。
        # 只检查 user 消息：注入块只会出现在 user 消息里，模型复述/用户手打
        # marker 到 assistant/system 消息不应误停注入。
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            for ctx in contexts:
                if not isinstance(ctx, dict) or ctx.get("role") != "user":
                    continue
                content = ctx.get("content")
                if isinstance(content, str):
                    if marker in content:
                        return True
                elif isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and isinstance(part.get("text"), str)
                            and marker in part["text"]
                        ):
                            return True
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] request_has_marker contexts check failed: {e}")
    return False


def replace_marker_in_contexts(req: Any, marker: str, new_text: str) -> bool:
    """在 contexts 的 user 消息里原位替换旧注入块（4.23+ 动态提示更新用）。

    contexts 是历史加载的独立副本，改写只影响本轮请求，不会写回持久化：
    每轮替换 = 模型始终看到最新动态提示，且历史不累积。
    - list 形态（4.23+ 实际形态）：替换含 marker 的 part 文本，返回 True；
    - str 形态含 marker（实际链路中不存在，仅防御）：不替换返回 True，
      视为"已存在"让调用方跳过追加，避免累积；
    - 找不到（首轮/4.16）：返回 False，调用方应走 append 注入。
    """
    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return False
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            if marker in content:
                return True
        elif isinstance(content, list):
            for i, part in enumerate(content):
                if (
                    isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and marker in part["text"]
                ):
                    content[i] = {"type": "text", "text": new_text}
                    return True
    return False


def append_temp_text_part(
    req: Any,
    text: str,
    factory: Any | None = None,
    *,
    marker: str | None = None,
) -> bool:
    """写入 temp extra；缺失 list 时创建。marker 已存在（system_prompt 或已有 part 文本）则跳过。

    约定：调用方注入的 text 必须以 marker 开头，幂等才成立（真实调用均满足）。
    """
    if not text.strip():
        return False
    if marker and request_has_marker(req, marker):
        return False
    try:
        if not hasattr(req, "extra_user_content_parts") or req.extra_user_content_parts is None:
            req.extra_user_content_parts = []
        parts = req.extra_user_content_parts
        if not isinstance(parts, list):
            return False
        part = make_text_part(text, factory)
        if part is None:
            return False
        parts.append(part)
        return True
    except Exception as e:
        if logger is not None:
            logger.error(f"[HumanChatQuality] append temp text part failed: {e}")
        return False
