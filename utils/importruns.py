r"""Record what each import run did, so the interface can say it out loud.

Until now an import left no trace but its log lines, on the server, readable by
one person. Two consequences, both bad for a member:

- **Nothing said how fresh the data was.** Someone who had just written to a
  journalist and did not see the exchange could not tell "not imported yet" from
  "not recognised" from "broken since Tuesday".
- **A failure was silent.** If the IMAP password expires, the pages look exactly
  as they always do — only nothing new ever arrives again. A CRM that quietly
  stops recording is worse than one that says it has stopped.

So every run opens a row here and closes it: when it started, when it ended,
whether it worked, how many mails it brought in. `/echanges` reads the last row
and shows "Dernière synchronisation il y a 4 minutes", or a warning when the
last run failed or is too old.

This is operational metadata, not personal data: a script name, timestamps, a
count and a one-line summary. No address, no name, no subject.

Used as a context manager, so a crash is recorded rather than lost:

    with importruns.track(db, "member_mails") as run:
        ...
        run.imported = 12
"""
import os
import sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# How long the interface may call the data fresh. Read by app.py: the member
# import runs every 10 minutes, so anything older than half an hour means three
# runs in a row did not happen.
STALE_AFTER_SECONDS = 30 * 60


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            script      TEXT NOT NULL,        -- 'member_mails', 'campaign_mails'…
            started_at  TEXT NOT NULL,
            finished_at TEXT,                 -- NULL while running, or if killed
            status      TEXT NOT NULL,        -- 'running' | 'ok' | 'error'
            imported    INTEGER NOT NULL DEFAULT 0,
            inspected   INTEGER NOT NULL DEFAULT 0,
            detail      TEXT                  -- one line, shown as-is
        );
        CREATE INDEX IF NOT EXISTS idx_import_runs_script
            ON import_runs (script, started_at DESC);
        """
    )


class Run:
    """One run in progress. Set the counters as you go; `track` closes it."""

    def __init__(self, db, run_id):
        self.db = db
        self.id = run_id
        self.imported = 0
        self.inspected = 0
        self.detail = None

    def finish(self, status):
        if self.id is None:
            return
        self.db.execute(
            "UPDATE import_runs SET finished_at = ?, status = ?, imported = ?, "
            "inspected = ?, detail = ? WHERE id = ?",
            (_now(), status, self.imported, self.inspected, self.detail, self.id),
        )
        self.db.commit()


class track:
    """Context manager recording one run — including the one that crashes.

    Never lets its own bookkeeping break an import: a database that cannot take
    the row (a dry run on a read-only copy, an old schema) yields a Run with no
    id, which then does nothing.
    """

    def __init__(self, db, script, enabled=True):
        self.db = db
        self.script = script
        self.enabled = enabled
        self.run = None

    def __enter__(self):
        run_id = None
        if self.enabled:
            try:
                ensure_table(self.db)
                cur = self.db.execute(
                    "INSERT INTO import_runs (script, started_at, status) "
                    "VALUES (?, ?, 'running')", (self.script, _now()))
                run_id = cur.lastrowid
                self.db.commit()
            except sqlite3.Error:
                run_id = None
        self.run = Run(self.db, run_id)
        return self.run

    def __exit__(self, exc_type, _exc, _tb):
        try:
            self.run.finish("ok" if exc_type is None else "error")
            if exc_type is not None and self.run.detail is None:
                self.db.execute(
                    "UPDATE import_runs SET detail = ? WHERE id = ?",
                    (f"{exc_type.__name__}", self.run.id))
                self.db.commit()
        except sqlite3.Error:
            pass
        return False          # never swallow the original exception


def record_external(db, script, status, detail=None, imported=0):
    """Enregistrer une exécution qui s'est déroulée **hors** de ce processus.

    `civicrm-sync.sh` tourne sur l'hôte et orchestre des scripts à travers
    `docker exec` : aucun processus Python ne couvre son exécution de bout en
    bout. Il appelle donc ceci en dernier, par un `docker exec` de plus, pour
    que sa réussite ou son échec apparaisse dans `import_runs` comme le reste.
    Sans quoi la seule synchro qu'on ne surveille pas serait la plus fragile —
    elle dépend d'un second logiciel.
    """
    ensure_table(db)
    now = _now()
    db.execute(
        "INSERT INTO import_runs (script, started_at, finished_at, status, "
        "imported, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (script, now, now, status, imported, detail))
    db.commit()


def last_run(db, script=None):
    """The most recent run, as a dict, or None. Tolerant of a missing table."""
    sql = ("SELECT script, started_at, finished_at, status, imported, inspected, "
           "detail FROM import_runs ")
    params = ()
    if script:
        sql += "WHERE script = ? "
        params = (script,)
    sql += "ORDER BY started_at DESC, id DESC LIMIT 1"
    try:
        row = db.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    keys = ("script", "started_at", "finished_at", "status", "imported",
            "inspected", "detail")
    return dict(zip(keys, row))


def main():
    """Point d'entrée en ligne de commande, pour les orchestrateurs hôtes.

        python3 utils/importruns.py --record civicrm_sync --status ok \
            --detail "168 médias, 3 fiches créées"
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--record", metavar="SCRIPT", required=True,
                        help="nom du script dont on enregistre l'exécution")
    parser.add_argument("--status", default="ok", choices=("ok", "error"))
    parser.add_argument("--detail", default=None)
    parser.add_argument("--imported", type=int, default=0)
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    try:
        record_external(db, args.record, args.status, args.detail, args.imported)
    finally:
        db.close()
    print(f"Exécution enregistrée : {args.record} ({args.status}).")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
