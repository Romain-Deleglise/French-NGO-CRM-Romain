#!/usr/bin/env python3
"""Import the médias held in CiviCRM as `organisations` — the bulk half of the sync.

Médias are the one thing worth copying wholesale out of CiviCRM: there are ~168
of them against the 165 already here, they are organisations rather than people
so they never reach the rencontre pickers, and a journalist fiche pulled on
demand needs its média to exist already. The journalists themselves are NOT
imported in bulk — see civicrm_lookup.py for why.

Reads the JSON that `cv api4 Contact.get` wrote (utils/deploy/civicrm-sync.sh),
never CiviCRM's database. Idempotent: médias are matched on a folded name
(accent- and case-insensitive, so CiviCRM's "LE FIGARO" finds this CRM's
"Le Figaro"), and a rerun adds nothing.

    python3 utils/import_civicrm_medias.py --file civi-medias.json            # dry run
    python3 utils/import_civicrm_medias.py --file civi-medias.json --commit
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from civicrm import (  # noqa: E402
    ORGANISATION_FIELDS, ContractError, assert_contract, norm_name,
    organisation_to_media, DEFAULT_STANCE, NOTE_SOURCE,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))


def log(msg):
    print(msg, flush=True)


def load_media_index(db):
    """Folded média name -> organisation id, for every Média already here."""
    index = {}
    for oid, name in db.execute(
        "SELECT id, name FROM organisations WHERE org_type = 'Média'"
    ):
        index.setdefault(norm_name(name), oid)
    return index


def upsert_media(db, name, now, index=None):
    """Return the id of the Média called `name`, creating it if it is new.

    The folded-name index is what keeps a rerun — and the on-demand journalist
    lookup — from creating "LE FIGARO" next to "Le Figaro". A média created here
    carries a note saying so: a moderator still has to set its type and
    orientation, which CiviCRM doesn't hold.
    """
    name = (name or "").strip()
    if not name:
        return None, False
    key = norm_name(name)
    if index is None:
        index = load_media_index(db)
    if key in index:
        return index[key], False
    org_id = db.execute(
        """
        INSERT INTO organisations (name, org_type, stance, notes, created_at)
        VALUES (?, 'Média', ?, ?, ?)
        """,
        (name, DEFAULT_STANCE,
         f"{NOTE_SOURCE} le {now[:10]}.\n"
         "Type de média et orientation à renseigner — CiviCRM ne les porte pas.",
         now),
    ).lastrowid
    index[key] = org_id
    return org_id, True


def link_person_media(db, person_id, media_name, now, index=None):
    """Attach a person to their média, creating the média if need be.

    Mirrors utils/orglink.link_group, which does the same for a politique and
    their groupe politique. Safe to call twice.
    """
    org_id, created = upsert_media(db, media_name, now, index)
    if org_id is None:
        return None, False
    db.execute(
        "INSERT OR IGNORE INTO person_organisations (person_id, organisation_id) "
        "VALUES (?, ?)",
        (person_id, org_id),
    )
    return org_id, created


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True,
                        help="JSON written by `cv api4 Contact.get` (médias)")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to meetings.db")
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is saved")
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        records = json.load(fh)
    try:
        assert_contract(records, ORGANISATION_FIELDS, "organisation")
    except ContractError as exc:
        log(f"ABANDON — contrat CiviCRM non respecté : {exc}")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys = ON")
    try:
        index = load_media_index(db)
        added = matched = skipped = 0
        for record in records:
            row = organisation_to_media(record, today)
            if row is None:
                skipped += 1
                continue
            key = norm_name(row["name"])
            if key in index:
                matched += 1
                continue
            if args.commit:
                _oid, _created = upsert_media(db, row["name"], now, index)
            else:
                index[key] = -1  # so duplicates inside the export count once
            added += 1
            log(f"  + {row['name']}")
        if args.commit:
            db.commit()
        verb = "ajoutés" if args.commit else "à ajouter (dry-run)"
        log(f"Terminé. Médias {verb} : {added} | déjà présents : {matched} | "
            f"ignorés (sans nom) : {skipped} | total lu : {len(records)}.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
