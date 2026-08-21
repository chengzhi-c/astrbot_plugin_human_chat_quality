import os
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_DEFAULT_TMPDIR = Path(__file__).resolve().parents[2]


def ensure_plugin_package() -> None:
    """Load the source tree under its canonical package name."""
    name = "astrbot_plugin_human_chat_quality"
    root = Path(__file__).resolve().parents[1]
    expected = (root / "__init__.py").resolve()
    current = sys.modules.get(name)
    current_file = getattr(current, "__file__", None)
    if current_file and Path(current_file).resolve() == expected:
        return
    spec = importlib.util.spec_from_file_location(
        name,
        expected,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin package from {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


V2_RULES_E4AA983 = (
    "[Human Chat Quality Rules v2]\n"
    "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
    "1. 日常闲聊顺着对方的话自然回应，避免客服式收尾、空泛鼓励和无信息增量的总结。\n"
    "2. 保留事实、限制条件、安全提示和不确定性表述，不为口语化而删减。\n"
    "3. 用户明确要求技术步骤、对比、正式文稿或清单时，以任务完成为先，允许精确结构与术语。\n"
    "4. 不知道就直说，不用空泛免责声明掩盖不确定性。\n"
    "5. 不要把这些约束写进回复。"
)


V5_RULES_B46BD0D = (
    "[Human Chat Quality Rules v5]\n"
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

V6_RULES_2FF4406 = (
    "[Human Chat Quality Rules v6]\n"
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
    "- 不要把这些约束写进回复\n"
    "- 用户直接问及身份、能力边界或知识截止时间时，如实简短作答，不回避"
)


def temporary_directory(test_case: unittest.TestCase) -> str:
    root = os.environ.get("HCQ_TEST_TMPDIR") or _DEFAULT_TMPDIR
    temp_dir = tempfile.TemporaryDirectory(dir=root)
    test_case.addCleanup(temp_dir.cleanup)
    return temp_dir.name


class FakePart:
    def __init__(self, text):
        self.text = text


class FakeReq:
    def __init__(self, system_prompt="原人设：你是XX"):
        self.system_prompt = system_prompt
        self.contexts = []
        self.extra_user_content_parts = []
