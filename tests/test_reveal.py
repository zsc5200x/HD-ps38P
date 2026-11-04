import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ps38app.reveal import open_in_file_manager, resolve_target


class RevealTargetTests(unittest.TestCase):
    def test_resolves_whitelisted_kinds(self):
        config = {
            "db_path": "/data/orders.sqlite3",
            "follow_db_path": "/data/follow.sqlite3",
            "merge_db_path": "/data/fixture_merge.sqlite3",
            "pool_db_path": "/data/accounts.sqlite3",
            "pool_dir": "/data/accounts",
        }
        self.assertEqual(resolve_target(config, "orders_db"), (Path("/data/orders.sqlite3"), False))
        self.assertEqual(resolve_target(config, "follow_db"), (Path("/data/follow.sqlite3"), False))
        self.assertEqual(resolve_target(config, "pool_dir"), (Path("/data/accounts"), True))

    def test_rejects_unknown_kinds_and_missing_paths(self):
        with self.assertRaises(ValueError):
            resolve_target({"db_path": "/data/orders.sqlite3"}, "../etc/passwd")
        with self.assertRaises(ValueError):
            resolve_target({"db_path": "/data/orders.sqlite3"}, "")
        with self.assertRaises(ValueError):
            resolve_target({"db_path": ""}, "orders_db")


class RevealCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.file = self.folder / "orders.sqlite3"
        self.file.write_text("", encoding="utf-8")

    def run_platform(self, platform, path, *, is_dir):
        with mock.patch("ps38app.reveal.subprocess.Popen") as popen:
            open_in_file_manager(path, is_dir=is_dir, platform=platform)
        return popen.call_args.args[0]

    def test_macos_reveals_file_in_finder(self):
        command = self.run_platform("darwin", self.file, is_dir=False)
        self.assertEqual(command, ["open", "-R", str(self.file)])

    def test_macos_opens_directory(self):
        command = self.run_platform("darwin", self.folder, is_dir=True)
        self.assertEqual(command, ["open", str(self.folder)])

    def test_windows_selects_file(self):
        command = self.run_platform("win32", self.file, is_dir=False)
        self.assertEqual(command, ["explorer", f"/select,{self.file}"])

    def test_linux_opens_parent_folder(self):
        command = self.run_platform("linux", self.file, is_dir=False)
        self.assertEqual(command, ["xdg-open", str(self.folder)])

    def test_missing_file_falls_back_to_parent_folder(self):
        missing = self.folder / "nested" / "missing.sqlite3"
        missing.parent.mkdir()
        command = self.run_platform("darwin", missing, is_dir=False)
        self.assertEqual(command, ["open", str(missing.parent)])

    def test_missing_folder_raises(self):
        with self.assertRaises(ValueError):
            open_in_file_manager(self.folder / "nope" / "x.db", is_dir=True, platform="darwin")


if __name__ == "__main__":
    unittest.main()
