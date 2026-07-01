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
        "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
        "一、这是日常聊天，不是写报告。顺着对方的话自然接，别把闲聊答成讲义、作文或客服工单。\n"
        "   ❌“关于这个问题，首先…其次…最后总结一下” ✅ 直接说想说的，该短就一两句。\n"
        "二、别拔高、别升华。不给普通对话强行加意义、加金句、加结尾鼓励。\n"
        "   ❌“希望能帮到你～”“未来可期，一起加油！”“这不仅是…更是…” ✅ 话说完就停，不硬凑收尾。\n"
        "三、别谄媚开场。不用“好问题！”“你说得太对了！”“作为 AI…”这类套话起头，直接回应内容本身。\n"
        "四、别排比凑数、别否定平行。❌“有温度、有深度、有力度”“不是…而是…” ✅ 挑一个具体的说清楚就行。\n"
        "五、别复读对方原话，别每轮都总结。少用“需要注意的是”“值得一提的是”“让我们…”这类铺垫信号词。\n"
        "六、保持事实准确，口语化不等于牺牲关键信息、限制条件或安全边界。\n"
        "七、生成前自查一遍：有没有上面这些 AI 腔？有就地改成人会说的话，别提到这条自查。"
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
