import io
import unittest

from scripts import run_tests
from scripts.run_tests import run_suite


class TestStrictRunner(unittest.TestCase):
    def test_clean_suite_succeeds(self):
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        self.assertEqual(run_suite(suite, io.StringIO()), 0)

    def test_skipped_suite_fails(self):
        @unittest.skip("synthetic skip")
        def skipped():
            pass

        suite = unittest.TestSuite([unittest.FunctionTestCase(skipped)])
        self.assertEqual(run_suite(suite, io.StringIO()), 1)

    def test_named_suites_are_supported(self):
        self.assertGreater(run_tests.load_suite("core").countTestCases(), 0)
        self.assertGreater(run_tests.load_suite("host").countTestCases(), 0)


if __name__ == "__main__":
    unittest.main()
