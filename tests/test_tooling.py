import io
import unittest

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


if __name__ == "__main__":
    unittest.main()
