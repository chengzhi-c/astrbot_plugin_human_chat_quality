"""main.py 双导入 try 分支（生产包加载路径）冒烟测试。

背景：真实 AstrBot 宿主以包方式加载插件，走 main.py 的 try 分支
（`from .quality_rules import ...`）；而仓库内全部常规测试都是顶层导入
（走 except 分支）。本测试以临时包结构复制源文件后包导入，堵住
"只改了 except 没改 try"的导入名不同步回归盲区。

隔离策略：子进程独立解释器运行，只暴露临时包目录与 tests 目录（假宿主）。
必须显式清空 PYTHONPATH/PYTHONHOME：若环境变量指向仓库根，except fallback
会从仓库根顶层导入成功，掩盖 try 分支破坏（红灯实测确认的掩盖路径之一）。

运行：python -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
# 新增模块文件时同步此清单（双导入约定的一部分，由本测试红灯兜底）
MODULES = ("main", "quality_rules", "runtime_state")


class PackageImportSmokeTest(unittest.TestCase):
    def test_package_import(self):
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "hcq_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            for name in MODULES:
                shutil.copy2(SRC / f"{name}.py", pkg / f"{name}.py")
            script = (
                "import sys; "
                f"sys.path.insert(0, {str(TESTS_DIR)!r}); "
                "import _fakes; "
                f"sys.path.insert(0, {str(td)!r}); "
                "import importlib; "
                "m = importlib.import_module('hcq_pkg.main'); "
                "assert m.STABLE_RULE_MARKER == '[Human Chat Quality Rules v2]', m.STABLE_RULE_MARKER; "
                "assert callable(m.HumanChatQualityCore) and callable(m.HumanChatQualityPlugin); "
                # 关键：确认走的是包内相对导入（try 分支）而非 except 顶层导入。
                # except fallback 会创建顶层 quality_rules 模块名，try 分支不会。
                "assert 'quality_rules' not in sys.modules, 'top-level fallback used (try branch broken)'; "
                "print('PKG_OK')"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            env["PYTHONHOME"] = ""
            r = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=str(td),
                env=env,
                timeout=30,
            )
            self.assertEqual(r.returncode, 0, f"package import failed:\n{r.stderr}")
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("PKG_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
