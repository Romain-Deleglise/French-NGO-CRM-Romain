"""init_db() must survive four gunicorn workers reaching it at once.

The deploy of 44b4311 crash-looped exactly there: `init_db()` runs on import, so
every worker runs the migrations simultaneously, and one worker's
`RENAME COLUMN details` landed while another was running `UPDATE … details`.

These tests boot several *real processes* against one fresh database, the way
gunicorn does, rather than threads — a lock held by the kernel is only worth
testing across processes.
"""
import os
import subprocess
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Importing app.py runs init_db(); CRM_DB_PATH is what keeps that off the real
# database sitting next to it.
BOOT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, %r)
    import app          # importing is what runs init_db()
    print("ok")
    """
) % ROOT


def _flask_available():
    try:
        import flask  # noqa: F401
        import magic  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_flask_available(), "flask/python-magic not installed")
class ConcurrentBootTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.dir.name, "meetings.db")

    def tearDown(self):
        self.dir.cleanup()

    def _boot(self, n):
        env = dict(os.environ, CRM_DB_PATH=self.db_path, APP_PASSWORD="test",
                   SECRET_KEY="test")
        procs = [subprocess.Popen([sys.executable, "-c", BOOT], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(n)]
        return [(p.wait(timeout=120), *p.communicate()) for p in procs]

    def test_four_workers_booting_at_once_all_succeed(self):
        for code, out, err in self._boot(4):
            self.assertEqual(code, 0, msg=err)
            self.assertIn("ok", out)

    def test_the_schema_is_complete_afterwards(self):
        self._boot(4)
        db = sqlite3.connect(self.db_path)
        try:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            db.close()
        # A representative slice: the core, the merged-in press half, and the
        # moderation queue. If a migration had been interrupted, one would miss.
        for table in ("persons", "organisations", "person_organisations",
                      "meetings", "mails", "interventions", "contents",
                      "pending_persons", "pending_organisations", "moderators"):
            self.assertIn(table, tables)

    def test_a_second_boot_over_an_existing_database_changes_nothing(self):
        self._boot(1)
        db = sqlite3.connect(self.db_path)
        before = sorted(r[0] for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        db.close()
        for code, _out, err in self._boot(4):
            self.assertEqual(code, 0, msg=err)
        db = sqlite3.connect(self.db_path)
        after = sorted(r[0] for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        db.close()
        self.assertEqual(before, after)


@unittest.skipUnless(_flask_available(), "flask/python-magic not installed")
class MigrationLockTests(unittest.TestCase):
    """The lock has to actually exclude, not merely exist."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.dir.name, "meetings.db")
        sys.path.insert(0, ROOT)
        os.environ["CRM_DB_PATH"] = self.db_path
        os.environ.setdefault("APP_PASSWORD", "test")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_second_process_waits_for_the_first(self):
        holder = textwrap.dedent(
            """
            import sys, time
            sys.path.insert(0, %r)
            import app
            with app._migration_lock() as taken:
                assert taken, "lock not taken"
                print("held", flush=True)
                time.sleep(2)
            """
        ) % ROOT
        env = dict(os.environ, CRM_DB_PATH=self.db_path, APP_PASSWORD="test")
        first = subprocess.Popen([sys.executable, "-c", holder], env=env,
                                 stdout=subprocess.PIPE, text=True)
        self.assertEqual(first.stdout.readline().strip(), "held")

        started = time.monotonic()
        second = subprocess.run(
            [sys.executable, "-c", BOOT], env=env, capture_output=True, text=True,
            timeout=120)
        waited = time.monotonic() - started
        first.stdout.close()
        first.wait(timeout=30)

        self.assertEqual(second.returncode, 0, msg=second.stderr)
        # It had to sit behind the two-second hold rather than barge in.
        self.assertGreater(waited, 1.0)


if __name__ == "__main__":
    unittest.main()
