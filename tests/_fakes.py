"""AstrBot 宿主假模块与共享测试夹具（仅供 tests 使用）。

宿主策略：真实 astrbot（>=4.23,<5）可用时整个测试套件走真实 API；
不可用或版本不兼容时才注入假的 astrbot.* 子模块（sys.modules），
使业务模块在纯环境（CI/无宿主）下仍可导入与测试。
假模块行为与业务模块导入期需要的宿主 API 一一对应：filter 装饰器为恒等，
Star 仅存 context，logger 为空实现。注册靠宿主 Star.__init_subclass__ 自动完成，无需装饰器。
"""

import sys
import types


class _Logger:
    def info(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


class _Filter:
    def on_llm_request(self, *_args, **_kwargs):
        return lambda func: func

    def on_llm_response(self, *_args, **_kwargs):
        return lambda func: func

    def command_group(self, *_args, **_kwargs):
        def decorator(func):
            def command(*_c_args, **_c_kwargs):
                return lambda nested: nested

            func.command = command
            return func

        return decorator


class PermissionType:
    ADMIN = "admin"


def permission_type(_permission):
    return lambda func: func


class Star:
    def __init__(self, context):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(*_args, **_kwargs):
        return "."


def _make_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def is_supported_host(v: str) -> bool:
    """宿主版本判别，与 metadata 声明的 astrbot_version ">=4.23,<5" 精确对齐。"""
    try:
        major, minor = (int(x) for x in v.split(".")[:2])
    except (ValueError, AttributeError):
        return False
    return (major, minor) >= (4, 23) and major < 5


def _install() -> None:
    """真实宿主（astrbot >=4.23,<5）可用时优先使用真实 API，否则注入假模块。"""
    try:
        import importlib.metadata as _metadata

        if is_supported_host(_metadata.version("astrbot")):
            import astrbot  # noqa: F401

            return
    except Exception:
        pass
    # 键存在但值为 None = 导入被显式屏蔽（Python 惯用黑名单），仍需安装假模块
    if sys.modules.get("astrbot") is not None:
        return
    sys.modules["astrbot"] = _make_module("astrbot")
    sys.modules["astrbot.api"] = _make_module(
        "astrbot.api",
        AstrBotConfig=type("AstrBotConfig", (), {}),
        logger=_Logger(),
    )
    sys.modules["astrbot.api.event"] = _make_module(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=_Filter(),
    )
    sys.modules["astrbot.api.event.filter"] = _make_module(
        "astrbot.api.event.filter",
        PermissionType=PermissionType,
        permission_type=permission_type,
    )
    sys.modules["astrbot.api.provider"] = _make_module(
        "astrbot.api.provider",
        LLMResponse=type("LLMResponse", (), {}),
        ProviderRequest=type("ProviderRequest", (), {}),
    )
    sys.modules["astrbot.api.star"] = _make_module(
        "astrbot.api.star",
        Context=type("Context", (), {}),
        Star=Star,
        StarTools=StarTools,
    )


class FakeEvent:
    """共享测试事件：unified_msg_origin / get_group_id 模拟群聊事件。"""

    def __init__(self, origin="GroupMessage:123#abc", group_id="123#abc", message_obj=None):
        self.unified_msg_origin = origin
        self._group_id = group_id
        self.message_obj = message_obj

    def get_group_id(self):
        return self._group_id


_install()
