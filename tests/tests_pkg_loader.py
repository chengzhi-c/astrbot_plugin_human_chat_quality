"""测试包加载：把插件根注册为正式包名，生产代码只保留相对导入。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

PLUGIN_PKG = "astrbot_plugin_human_chat_quality"
ROOT = Path(__file__).resolve().parent.parent
_SUBMODULES = ("runtime_state", "quality_rules", "main")


def load_plugin_package() -> ModuleType:
    """确保 astrbot_plugin_human_chat_quality.{runtime_state,quality_rules,main} 可导入。"""
    import _fakes  # noqa: F401

    existing = sys.modules.get(PLUGIN_PKG)
    if existing is not None and all(f"{PLUGIN_PKG}.{n}" in sys.modules for n in _SUBMODULES):
        return existing

    pkg = types.ModuleType(PLUGIN_PKG)
    pkg.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    pkg.__file__ = str(ROOT / "__init__.py")
    pkg.__package__ = PLUGIN_PKG
    sys.modules[PLUGIN_PKG] = pkg

    for name in _SUBMODULES:
        full = f"{PLUGIN_PKG}.{name}"
        if full in sys.modules:
            setattr(pkg, name, sys.modules[full])
            continue
        path = str(ROOT / f"{name}.py")
        loader = importlib.machinery.SourceFileLoader(full, path)
        spec = importlib.util.spec_from_loader(full, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = PLUGIN_PKG
        sys.modules[full] = module
        loader.exec_module(module)
        setattr(pkg, name, module)

    return pkg


def get_main() -> ModuleType:
    load_plugin_package()
    return sys.modules[f"{PLUGIN_PKG}.main"]


def get_quality_rules() -> ModuleType:
    load_plugin_package()
    return sys.modules[f"{PLUGIN_PKG}.quality_rules"]


def get_runtime_state() -> ModuleType:
    load_plugin_package()
    return sys.modules[f"{PLUGIN_PKG}.runtime_state"]
