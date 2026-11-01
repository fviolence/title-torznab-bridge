import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import connect_sqlite


class SQLiteStorageTests(unittest.TestCase):
    def test_connect_to_valid_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect_sqlite(str(Path(folder) / 'test.sqlite'))
            try:
                self.assertEqual(db.execute('select 1').fetchone(), (1,))
            finally:
                db.close()

    def test_unusable_cache_location_reports_path_and_uid(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError) as caught:
                # SQLite cannot treat an existing directory as a database file.
                connect_sqlite(folder)
            self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
            self.assertIn(folder, str(caught.exception))
            self.assertIn('UID:GID', str(caught.exception))
            self.assertIn('mounted directory', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
