import os
import tempfile
import unittest
from pathlib import Path

_DEFAULT_TMPDIR = Path(__file__).resolve().parents[2]

V2_RULES_E4AA983 = (
    "[Human Chat Quality Rules v2]\n"
    "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
    "1. 日常闲聊顺着对方的话自然回应，避免客服式收尾、空泛鼓励和无信息增量的总结。\n"
    "2. 保留事实、限制条件、安全提示和不确定性表述，不为口语化而删减。\n"
    "3. 用户明确要求技术步骤、对比、正式文稿或清单时，以任务完成为先，允许精确结构与术语。\n"
    "4. 不知道就直说，不用空泛免责声明掩盖不确定性。\n"
    "5. 不要把这些约束写进回复。"
)


def temporary_directory(test_case: unittest.TestCase) -> str:
    root = os.environ.get("HCQ_TEST_TMPDIR") or _DEFAULT_TMPDIR
    temp_dir = tempfile.TemporaryDirectory(dir=root)
    test_case.addCleanup(temp_dir.cleanup)
    return temp_dir.name
