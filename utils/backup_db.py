#!/usr/bin/env python3
"""Take a consistent backup of meetings.db, whatever its journal mode.

`cp meetings.db backup.db` stopped being correct the day the database moved to
WAL journal mode (September 2026, to stop the app and the mail importers
deadlocking each other). In WAL, recent commits live in a separate
`meetings.db-wal` file until a checkpoint folds them back in, so a plain copy
silently loses them — and looks perfectly fine.

`VACUUM INTO` writes a single self-contained file with everything committed at
the moment it runs, from a normal connection, with the app still serving. It
also compacts, so the backup is usually smaller than the live file.

    python3 utils/backup_db.py                          # meetings.db.bak-YYYY-MM-DD
    python3 utils/backup_db.py --label avant-civicrm    # …bak-avant-civicrm-YYYY-MM-DD
    python3 utils/backup_db.py --out /tmp/snapshot.db

Inside the container, where the database lives:

    docker exec website-meeting-app python3 /app/utils/backup_db.py
"""
import argparse
import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="database to back up")
    parser.add_argument("--out", help="destination file (default: alongside the "
                                      "database, named .bak-<label>-<date>)")
    parser.add_argument("--label", help="what this snapshot is for, e.g. "
                                        "« avant-civicrm »")
    args = parser.parse_args()

    out = args.out
    if not out:
        suffix = f"-{args.label}" if args.label else ""
        out = f"{args.db}.bak{suffix}-{date.today().isoformat()}"
    if os.path.exists(out):
        print(f"ABANDON — {out} existe déjà. Donnez --out ou --label.")
        return 1

    db = sqlite3.connect(args.db)
    try:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        # VACUUM INTO takes a literal, not a placeholder, so the path is
        # quoted by hand — doubling any single quote, as SQL requires.
        db.execute("VACUUM INTO '" + out.replace("'", "''") + "'")
    finally:
        db.close()

    size = os.path.getsize(out)
    print(f"Sauvegarde écrite : {out}")
    print(f"  {size / 1_048_576:.1f} Mio | journal de la source : {mode}")
    if mode.lower() == "wal":
        print("  (en WAL, `cp meetings.db` seul perdrait les écritures récentes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
