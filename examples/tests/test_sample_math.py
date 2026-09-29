"""示例测试：演示 unittest 风格的用例（可被 run_tests 工具发现并执行）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_math import add, average, divide, is_prime  # noqa: E402


class TestSampleMath(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(1, 2), 3)
        self.assertEqual(add(-1, 1), 0)

    def test_divide(self) -> None:
        self.assertEqual(divide(6, 3), 2)
        with self.assertRaises(ValueError):
            divide(1, 0)

    def test_average(self) -> None:
        self.assertEqual(average([1, 2, 3]), 2)
        with self.assertRaises(ValueError):
            average([])

    def test_is_prime(self) -> None:
        self.assertTrue(is_prime(2))
        self.assertTrue(is_prime(13))
        self.assertFalse(is_prime(1))
        self.assertFalse(is_prime(9))


if __name__ == "__main__":
    unittest.main()
