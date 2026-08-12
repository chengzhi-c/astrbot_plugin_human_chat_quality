import asyncio
import unittest

import sys
from pathlib import Path

_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from astrbot.api.event.filter import PermissionType, PermissionTypeFilter
from astrbot.api.star import Star
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot_plugin_human_chat_quality.main import HumanChatQualityPlugin, _version_from_lines


class TestVersionParse(unittest.TestCase):
    def test_version_from_lines(self):
        self.assertEqual(_version_from_lines(["name: x", "version: 1.2.3", ""]), "1.2.3")
        self.assertEqual(_version_from_lines(['version: "1.0.0"']), "1.0.0")
        self.assertEqual(_version_from_lines(["name: x"]), "0.0.0")
        self.assertEqual(_version_from_lines(["version:"]), "0.0.0")


class TestHostRegistration(unittest.TestCase):
    def test_plugin_is_star_subclass_and_handlers_registered(self):
        self.assertTrue(issubclass(HumanChatQualityPlugin, Star))
        handlers = star_handlers_registry.get_handlers_by_module_name(HumanChatQualityPlugin.__module__)
        names = {handler.handler_name for handler in handlers}
        self.assertTrue(
            {
                "on_llm_request",
                "on_llm_response",
                "humanq_status",
                "humanq_on",
                "humanq_off",
                "humanq_reset",
                "humanq_rules",
            }
            <= names
        )
        event_types = {handler.handler_name: handler.event_type for handler in handlers}
        self.assertEqual(event_types["on_llm_request"], EventType.OnLLMRequestEvent)
        self.assertEqual(event_types["on_llm_response"], EventType.OnLLMResponseEvent)
        request_handler = next(handler for handler in handlers if handler.handler_name == "on_llm_request")
        self.assertEqual(request_handler.extras_configs["priority"], -100)

    def test_commands_keep_admin_permission_filter(self):
        handlers = star_handlers_registry.get_handlers_by_module_name(HumanChatQualityPlugin.__module__)
        commands = {"humanq_status", "humanq_on", "humanq_off", "humanq_reset", "humanq_rules"}
        for handler in handlers:
            if handler.handler_name not in commands:
                continue
            permissions = [item for item in handler.event_filters if isinstance(item, PermissionTypeFilter)]
            self.assertEqual(len(permissions), 1, handler.handler_name)
            self.assertEqual(permissions[0].permission_type, PermissionType.ADMIN)

    def test_plugin_adapter_swallows_core_errors(self):
        async def fail(event, req):
            raise RuntimeError("boom")

        stub = type("StubCore", (), {"on_llm_request": staticmethod(fail)})()
        plugin = type("StubPlugin", (), {"core": stub})()
        asyncio.run(HumanChatQualityPlugin.on_llm_request(plugin, object(), object()))

        stub = type("StubCore", (), {"on_llm_response": staticmethod(fail)})()
        plugin = type("StubPlugin", (), {"core": stub})()
        asyncio.run(HumanChatQualityPlugin.on_llm_response(plugin, object(), object()))


if __name__ == "__main__":
    unittest.main()
