"""共享测试夹具（假宿主安装后经包加载器取业务模块）。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from _fakes import FakeEvent
from tests_pkg_loader import get_main, get_quality_rules, get_runtime_state, load_plugin_package

load_plugin_package()

main = get_main()
quality_rules = get_quality_rules()
runtime_state = get_runtime_state()

HumanChatQualityCore = main.HumanChatQualityCore
RuntimeStateStore = runtime_state.RuntimeStateStore
RUNTIME_HINT_MARKER = quality_rules.RUNTIME_HINT_MARKER
STABLE_RULE_MARKER = quality_rules.STABLE_RULE_MARKER


class FakeReq:
    """请求夹具。extra_user_content_parts 默认 None：区分「未触碰」与「空列表」。"""

    def __init__(self, contexts=None, system_prompt="", prompt="hi"):
        self.contexts = contexts or []
        self.system_prompt = system_prompt
        self.prompt = prompt
        self.extra_user_content_parts = None


class FakeResp:
    def __init__(self, completion_text=None, chain=None):
        self.completion_text = completion_text
        self.result_chain = chain


def chain(*texts):
    class _Item:
        def __init__(self, text):
            self.text = text

    class _Chain:
        def __init__(self, items):
            self.chain = items

    return _Chain([_Item(t) for t in texts])


def fake_part_factory(text: str):
    class _P:
        def __init__(self, text: str):
            self.text = text

    return _P(text)


def run(coro):
    return asyncio.run(coro)


def make_store(tmp: Path, window=8, custom=None, retention=14):
    return RuntimeStateStore(
        tmp / "state.json",
        retention_days=retention,
        recent_reply_window=window,
        custom_cliches=custom,
    )


def make_core(tmp: Path, config: dict, factory=fake_part_factory):
    store = RuntimeStateStore(
        tmp / "state.json",
        retention_days=14,
        recent_reply_window=8,
    )
    return HumanChatQualityCore(config, store, text_part_factory=factory)


def temp_dir():
    return tempfile.TemporaryDirectory()


__all__ = [
    "FakeEvent",
    "FakeReq",
    "FakeResp",
    "HumanChatQualityCore",
    "RUNTIME_HINT_MARKER",
    "RuntimeStateStore",
    "STABLE_RULE_MARKER",
    "chain",
    "fake_part_factory",
    "get_main",
    "get_quality_rules",
    "get_runtime_state",
    "load_plugin_package",
    "main",
    "make_core",
    "make_store",
    "quality_rules",
    "run",
    "runtime_state",
    "temp_dir",
]
