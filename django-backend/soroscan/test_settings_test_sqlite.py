"""SQLite PRAGMA tuning applied by settings_test.py (issue #1575)."""

from django.db import connection
from django.test import SimpleTestCase


class SqliteTestPragmaTests(SimpleTestCase):
    databases = {"default"}

    def _pragma(self, name):
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA {name}")
            return cursor.fetchone()[0]

    def test_synchronous_is_off(self):
        # 0 == OFF
        self.assertEqual(self._pragma("synchronous"), 0)

    def test_journal_mode_is_memory(self):
        self.assertEqual(self._pragma("journal_mode").lower(), "memory")
