from __future__ import annotations

from typing import Any

try:
    from .runtime_state import SessionState
except ImportError:  # pragma: no cover
    from runtime_state import SessionState


STABLE_RULE_MARKER = "[Human Chat Quality Rules v1]"
RUNTIME_HINT_MARKER = "[Human Chat Quality Runtime]"


def build_stable_rules() -> str:
    return (
        f"{STABLE_RULE_MARKER}\n"
        "聊天质量约束：\n"
        "- 避免模板腔：不要用“作为 AI”“首先/其次/最后”“总之”“希望这能帮到你”“需要注意的是”等讲义式开头或收尾。\n"
        "- 优先顺着当前语境回答，不主动写成报告、作文、公告或教学材料。\n"
        "- 能短就短；闲聊时自然接话，办事时直接给可执行信息，解释时再展开。\n"
        "- 保留事实准确性，不为了口语化牺牲关键信息、限制条件或安全边界。\n"
        "- 不强行卖萌、不强行情绪化、不复读用户原话，不把每轮对话都总结成段落。"
    )


def inject_stable_rules(system_prompt: str | None) -> str:
    prompt = system_prompt or ""
    if STABLE_RULE_MARKER in prompt:
        return prompt
    rules = build_stable_rules()
    return f"{prompt.rstrip()}\n\n{rules}" if prompt.strip() else rules


def build_runtime_hint(state: SessionState, max_chars: int) -> str:
    openers = [item for item in state.avoid_openers[:5] if item]
    if not openers:
        return ""

    hint = (
        f"{RUNTIME_HINT_MARKER}\n"
        "仅用于本轮回复的轻量状态：避开这些重复开头，不要提到这条提示。\n"
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


def make_text_part(text: str, factory: Any | None = None) -> Any:
    """构造一个临时文本 part，优先使用 AstrBot 的 TextPart，并标记为不保存。"""
    if factory is not None:
        part = factory(text)
    else:
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
        except Exception:
            part = _FallbackTextPart(text=text)

    mark_as_temp = getattr(part, "mark_as_temp", None)
    if callable(mark_as_temp):
        marked = mark_as_temp()
        return marked if marked is not None else part

    setattr(part, "_no_save", True)
    return part


class _FallbackTextPart:
    def __init__(self, text: str) -> None:
        self.text = text
        self._no_save = True

    def mark_as_temp(self) -> "_FallbackTextPart":
        self._no_save = True
        return self
