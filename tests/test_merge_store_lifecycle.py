import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ps38app.fixture_merge import FixtureMergeStore


class FixtureMergeStoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FixtureMergeStore(Path(temporary.name) / "merge.sqlite3")

    def test_successful_transaction_persists_and_releases_connection(self):
        with self.store._connect() as connection:
            connection.execute("INSERT INTO merge_meta (key, value) VALUES ('saved', 'yes')")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.store._connect() as reopened:
            row = reopened.execute("SELECT value FROM merge_meta WHERE key='saved'").fetchone()
            self.assertEqual(row["value"], "yes")
            self.assertEqual(reopened.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(reopened.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_failed_transaction_rolls_back_and_releases_connection(self):
        with self.assertRaisesRegex(RuntimeError, "cancel transaction"):
            with self.store._connect() as connection:
                connection.execute("INSERT INTO merge_meta (key, value) VALUES ('discarded', 'no')")
                raise RuntimeError("cancel transaction")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.store._connect() as reopened:
            row = reopened.execute("SELECT value FROM merge_meta WHERE key='discarded'").fetchone()
            self.assertIsNone(row)

    def test_failed_connection_setup_still_releases_connection(self):
        connection = Mock(spec=sqlite3.Connection)
        connection.execute.side_effect = sqlite3.OperationalError("cannot set pragmas")
        with patch("ps38app.fixture_merge.sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "cannot set pragmas"):
                with self.store._connect():
                    self.fail("connection setup should fail before yielding")
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
