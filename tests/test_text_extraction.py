"""Response text extraction contracts (result_chain fallback path)."""

import unittest

from tests._support import ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.core import extract_response_text


class FakeLLMResp:
    def __init__(self, text):
        self.completion_text = text
        self.result_chain = None


class TestResponseTextExtraction(unittest.TestCase):
    """C11：回复文本提取的 result_chain 兜底路径。"""

    def test_completion_text_used_first(self):
        self.assertEqual(extract_response_text(FakeLLMResp("正文")), "正文")

    def test_chain_fallback_with_role_filter(self):
        class Part:
            def __init__(self, role, text):
                self.role = role
                self.text = text

        class Chain:
            def __init__(self, parts):
                self.chain = parts

        resp = FakeLLMResp("")
        resp.result_chain = Chain([Part("assistant", "模型输出"), Part("user", "用户原话")])
        self.assertEqual(extract_response_text(resp), "模型输出")

    def test_chain_content_field(self):
        class ContentPart:
            def __init__(self, content):
                self.content = content

        class Chain:
            def __init__(self, parts):
                self.chain = parts

        resp = FakeLLMResp("")
        resp.result_chain = Chain([ContentPart("正文内容")])
        self.assertEqual(extract_response_text(resp), "正文内容")

    def test_empty_all(self):
        self.assertEqual(extract_response_text(FakeLLMResp("")), "")


if __name__ == "__main__":
    unittest.main()
