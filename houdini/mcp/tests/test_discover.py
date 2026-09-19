import os
import sys
import unittest

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, HERE)

from discover import discover  # noqa: E402


class DiscoverTests(unittest.TestCase):
    def test_returns_platform(self):
        info = discover()
        self.assertEqual(info["platform"], sys.platform)
        self.assertIn("installs", info)
        self.assertIsInstance(info["installs"], list)


if __name__ == "__main__":
    unittest.main()
