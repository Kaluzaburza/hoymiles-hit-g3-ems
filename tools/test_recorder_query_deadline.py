"""Real SQLite cancellation and pooled-connection recovery, no live HA."""
from contextlib import nullcontext
import importlib
import time
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from test_recorder_duplication import http  # Real HA imports, isolated package.

bounded = importlib.import_module('custom_components.hoymiles_hit_modbus.bounded_history')


class RecorderDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.session = Session(self.engine)
        self.session.execute(text('PRAGMA query_only=ON'))

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def budget(self, seconds):
        guard = getattr(bounded, 'recorder_query_budget', None)
        return guard(self.session, time.monotonic()+seconds) if guard else nullcontext()

    def test_sql_work_stops_at_deadline_and_connection_remains_usable(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            with self.budget(.01):
                self.session.execute(text('WITH RECURSIVE n(x) AS '
                    '(VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) '
                    'SELECT sum(x) FROM n')).scalar()
        self.assertLess(time.monotonic()-start, 1)
        self.assertEqual(self.session.execute(text('SELECT 42')).scalar(), 42)
        self.assertEqual(self.session.execute(text('PRAGMA query_only')).scalar(), 1)

    def test_normal_results_and_errors_are_preserved(self):
        with self.budget(1):
            self.assertEqual(self.session.execute(text('SELECT 7')).scalar(), 7)
        with self.assertRaises(OperationalError):
            with self.budget(1):
                self.session.execute(text('SELECT missing FROM no_such_table'))
        self.assertEqual(self.session.execute(text('SELECT 8')).scalar(), 8)

    def test_expired_budget_does_not_start_sql(self):
        entered = False
        with self.assertRaises(TimeoutError):
            with self.budget(-1):
                entered = True
        self.assertFalse(entered)


if __name__ == '__main__': unittest.main()
