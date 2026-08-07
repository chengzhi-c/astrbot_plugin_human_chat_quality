"""生产包相对导入冒烟：业务模块必须能以包方式加载（无顶层 fallback）。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
MODULES = ("main", "quality_rules", "runtime_state")


class PackageImportSmokeTest(unittest.TestCase):
    def test_package_import_relative_only(self):
        """子进程内以正式包名加载，确认相对导入可用且不出现顶层 quality_rules。"""
        with tempfile.TemporaryDirectory() as td:
            script = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(TESTS_DIR)!r})
import _fakes
from tests_pkg_loader import load_plugin_package, PLUGIN_PKG
import sys as _sys
pkg = load_plugin_package()
m = _sys.modules[PLUGIN_PKG + '.main']
assert m.STABLE_RULE_MARKER == '[Human Chat Quality Rules v2]', m.STABLE_RULE_MARKER
assert callable(m.HumanChatQualityCore) and callable(m.HumanChatQualityPlugin)
assert 'quality_rules' not in _sys.modules, 'top-level quality_rules leaked'
assert (PLUGIN_PKG + '.quality_rules') in _sys.modules
print('PKG_OK')
"""
            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            env["PYTHONHOME"] = ""
            r = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=str(td),
                env=env,
                timeout=60,
            )
            self.assertEqual(r.returncode, 0, f"package import failed:\nstdout={r.stdout}\nstderr={r.stderr}")
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("PKG_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
