"""Cover the import trace and the freshness it feeds the interface.

The point is not bookkeeping for its own sake: before this, an expired IMAP
password left every page looking exactly as usual while nothing arrived any
more, and a member who did not find their exchange had no way to tell a normal
delay from a broken pipeline. So the two things pinned here are that a failed
run IS recorded (the easy case to get wrong — a crash that writes nothing looks
like no run at all) and that the banner states the right one of three states.
"""
import os
import sqlite3
import tempfile
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import importruns  # noqa: E402


def iso(dt):
    return dt.isoformat(timespec="seconds")


class TrackTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def _rows(self):
        return self.db.execute(
            "SELECT script, status, imported, detail FROM import_runs "
            "ORDER BY id").fetchall()

    def test_a_successful_run_is_closed(self):
        with importruns.track(self.db, "member_mails") as run:
            run.imported = 12
            run.inspected = 40
            run.detail = "12 courriel(s) intégré(s)."
        self.assertEqual(self._rows(),
                         [("member_mails", "ok", 12, "12 courriel(s) intégré(s).")])
        self.assertIsNotNone(self.db.execute(
            "SELECT finished_at FROM import_runs").fetchone()[0])

    def test_a_crashing_run_is_recorded_and_the_error_still_raised(self):
        with self.assertRaises(RuntimeError):
            with importruns.track(self.db, "member_mails"):
                raise RuntimeError("IMAP login failed")
        script, status, _imported, detail = self._rows()[0]
        self.assertEqual((script, status), ("member_mails", "error"))
        self.assertIn("RuntimeError", detail or "")

    def test_a_dry_run_records_nothing(self):
        with importruns.track(self.db, "member_mails", enabled=False) as run:
            run.imported = 5
        with self.assertRaises(sqlite3.Error):
            self.db.execute("SELECT 1 FROM import_runs")

    def test_last_run_tolerates_a_database_without_the_table(self):
        self.assertIsNone(importruns.last_run(sqlite3.connect(":memory:")))

    def test_last_run_picks_the_most_recent_for_that_script(self):
        importruns.ensure_table(self.db)
        for script, when in (("campaign_mails", "2026-09-25T06:00:00+00:00"),
                             ("member_mails", "2026-09-25T05:00:00+00:00"),
                             ("member_mails", "2026-09-25T07:00:00+00:00")):
            self.db.execute(
                "INSERT INTO import_runs (script, started_at, status) "
                "VALUES (?, ?, 'ok')", (script, when))
        self.assertEqual(importruns.last_run(self.db, "member_mails")["started_at"],
                         "2026-09-25T07:00:00+00:00")


class FreshnessTests(unittest.TestCase):
    """`import_status` is what the banner says. Three states, no others."""

    def setUp(self):
        # A temp file rather than ":memory:": `app` is imported once per test
        # run and freezes DB_PATH, so a module that pins it to memory would
        # leave every later test with a database each connection creates anew.
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        import app                                   # noqa: PLC0415
        self.app = app
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        importruns.ensure_table(self.db)
        self.addCleanup(self.db.close)

    def _run(self, minutes_ago, status="ok", imported=3):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.db.execute(
            "INSERT INTO import_runs (script, started_at, finished_at, status, "
            "imported) VALUES ('member_mails', ?, ?, ?, ?)",
            (iso(when), iso(when), status, imported))

    def test_nothing_ever_run_says_nothing(self):
        # A fresh install has nobody to reassure yet; a banner would be noise.
        self.assertIsNone(self.app.import_status(self.db))

    def test_a_recent_run_is_ok(self):
        self._run(4)
        state = self.app.import_status(self.db)
        self.assertEqual(state["state"], "ok")
        self.assertEqual(state["when"], "il y a 4 minutes")

    def test_an_old_run_is_stale(self):
        # Three 10-minute slots missed in a row: something is stopping it.
        self._run(45)
        self.assertEqual(self.app.import_status(self.db)["state"], "stale")

    def test_a_failed_run_is_an_error_however_recent(self):
        self._run(2, status="error")
        self.assertEqual(self.app.import_status(self.db)["state"], "error")

    def test_only_the_latest_run_counts(self):
        self._run(90, status="error")
        self._run(3)
        self.assertEqual(self.app.import_status(self.db)["state"], "ok")

    def test_a_naive_timestamp_is_read_as_utc(self):
        # Rows written before the importers stored a timezone.
        when = datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.execute(
            "INSERT INTO import_runs (script, started_at, status) "
            "VALUES ('member_mails', ?, 'ok')", (iso(when),))
        self.assertEqual(self.app.import_status(self.db)["state"], "ok")

    def test_the_phrasing_walks_from_minutes_to_days(self):
        cases = {1: "à l'instant", 20: "il y a 20 minutes",
                 60 * 3: "il y a 3 heures", 60 * 24 * 2: "il y a 2 jours"}
        for minutes, expected in cases.items():
            self.assertEqual(
                self.app._humanise_age(timedelta(minutes=minutes)), expected)


if __name__ == "__main__":
    unittest.main()
