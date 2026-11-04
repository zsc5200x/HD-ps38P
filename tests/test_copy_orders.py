"""主页「跟单视角」8 位单号的分配、唯一性与追溯。"""

import tempfile
import unittest
from pathlib import Path

from ps38app import copy_orders


class CopyOrderNumberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "copy_orders.sqlite3"

    def test_numbers_are_eight_digits(self):
        numbers = copy_orders.assign_numbers(self.path, ["a", "b", "c"])
        self.assertEqual(len(numbers), 3)
        for value in numbers.values():
            self.assertEqual(len(value), 8, value)
            self.assertTrue(value.isdigit(), value)
            self.assertGreaterEqual(int(value), copy_orders.MIN_NO)

    def test_same_group_keeps_the_same_number(self):
        first = copy_orders.assign_numbers(self.path, ["group-a"])
        second = copy_orders.assign_numbers(self.path, ["group-a"])
        self.assertEqual(first, second)

    def test_numbers_are_unique_across_groups(self):
        keys = [f"group-{index}" for index in range(200)]
        numbers = copy_orders.assign_numbers(self.path, keys)
        self.assertEqual(len(set(numbers.values())), len(keys))

    def test_number_traces_back_to_the_group(self):
        numbers = copy_orders.assign_numbers(self.path, ["trace-me"])
        self.assertEqual(
            copy_orders.lookup(self.path, numbers["trace-me"]), "trace-me"
        )
        self.assertIsNone(copy_orders.lookup(self.path, "00000000"))

    def test_empty_input_creates_no_database(self):
        self.assertEqual(copy_orders.assign_numbers(self.path, []), {})
        self.assertFalse(self.path.exists())

    def test_unwritable_path_returns_empty_numbers(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        numbers = copy_orders.assign_numbers(blocker / "copy.sqlite3", ["a"])
        self.assertEqual(numbers, {})


if __name__ == "__main__":
    unittest.main()
