#!/usr/bin/env python3
"""Resolve unknown mail addresses against CiviCRM — the on-demand half of the sync.

CiviCRM holds ~12 900 journalists. Copying them here would break the tool: the
rencontre and courriel forms render one <option> *and* one checkbox per person,
so a 17 000-row `persons` table turns the main data-entry screens into
multi-megabyte pages. And a journal of interactions has no use for 12 900 people
nobody has ever written to.

So a journalist earns a fiche the day a member actually exchanges mail with them:

  1. import_member_mails.py meets an address it cannot match and, instead of
     dropping it, records it in `civicrm_pending` (see enqueue below).
  2. This script prints those addresses (--list-pending); the host looks them up
     with `cv api4` — see utils/deploy/civicrm-sync.sh.
  3. This script reads the answer (--apply) and creates the fiche, with its
     média and its alignement.
  4. import_member_mails.py --backfill runs again; the address now matches, and
     the mail is recorded like any other. `imported_mails` keeps that sweep from
     duplicating anything.

An address CiviCRM doesn't know is marked 'absent' so it isn't looked up again
every night; --retry-absent puts them back in the queue after CiviCRM has been
enriched.

    python3 utils/civicrm_lookup.py --list-pending
    python3 utils/civicrm_lookup.py --apply civi-contacts.json --commit
    python3 utils/civicrm_lookup.py --stats
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from civicrm import (  # noqa: E402
    CONTACT_FIELDS, ContractError, assert_contract, clean_email,
    contact_to_person, norm_name,
)
from import_civicrm_medias import link_person_media, load_media_index  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# Addresses never worth asking CiviCRM about: shared desks and no-reply robots.
# They belong to a newsroom, not a person, so a fiche would be meaningless.
GENERIC_LOCALPARTS = {
    "contact", "redaction", "info", "infos", "presse", "press", "contactez-nous",
    "noreply", "no-reply", "ne-pas-repondre", "nepasrepondre", "mailer-daemon",
    "postmaster", "abonnement", "abonnements", "service-client", "newsletter",
    "courrier", "lecteurs", "moderation", "webmaster", "admin", "support",
}


def log(msg):
    print(msg, flush=True)


def ensure_civicrm_tables(db):
    """The lookup queue. Created by whoever gets there first (import or resolve)."""
    db.executescript(
        """
        -- One row per external address the mail import could not match. 'pending'
        -- is waiting for a CiviCRM lookup, 'resolved' got a fiche, 'absent' means
        -- CiviCRM doesn't know it either (kept, so we stop asking every night).
        CREATE TABLE IF NOT EXISTS civicrm_pending (
            email       TEXT PRIMARY KEY,
            display     TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            seen_count  INTEGER NOT NULL DEFAULT 1,
            status      TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT,
            person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_civicrm_pending_status
            ON civicrm_pending(status);
        """
    )


def is_generic(address):
    """True for a newsroom desk or a robot — worth skipping, not worth a fiche."""
    local = (address or "").split("@", 1)[0].lower()
    local = local.split("+", 1)[0]
    return local in GENERIC_LOCALPARTS


def enqueue(db, address, display, now):
    """Record an unmatched external address for the next CiviCRM lookup.

    Called by import_member_mails.py. Never raises: a queueing problem must not
    cost us the mail import itself.
    """
    address = clean_email(address)
    if not address or is_generic(address):
        return False
    display = (display or "").strip() or None
    try:
        # `rowcount` can't tell an insert from an ON CONFLICT update — both report
        # one row — and the caller counts genuinely new addresses, so ask first.
        seen = db.execute(
            "SELECT 1 FROM civicrm_pending WHERE email = ?", (address,)
        ).fetchone() is not None
        if seen:
            db.execute(
                """
                UPDATE civicrm_pending
                   SET last_seen  = ?,
                       seen_count = seen_count + 1,
                       display    = COALESCE(NULLIF(display, ''), ?)
                 WHERE email = ?
                """,
                (now, display, address),
            )
            return False
        db.execute(
            "INSERT INTO civicrm_pending (email, display, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (address, display, now, now),
        )
        return True
    except sqlite3.Error:
        return False


def load_person_index(db):
    """(folded name, contact_type) -> person id, to attach rather than duplicate.

    The 133 journalists seeded from the old press CRM have a name but no address.
    When CiviCRM hands us the same person, we fill that fiche in instead of
    creating a second one — the same "match by name within a type" rule the
    insert_* scripts follow.
    """
    index = {}
    for pid, name, ctype in db.execute(
        "SELECT id, name, contact_type FROM persons"
    ):
        index.setdefault((norm_name(name), ctype), pid)
    return index


def existing_emails(db):
    out = set()
    for (mail,) in db.execute(
        "SELECT email FROM persons WHERE email IS NOT NULL AND email != ''"
    ):
        out.add(mail.strip().lower())
    return out


def create_or_attach(db, row, now, person_index, media_index):
    """Give a CiviCRM contact a fiche here. Returns (person_id, action).

    action is 'attached' when an existing fiche gained the address, 'created'
    otherwise. An existing address is never overwritten: a hand-typed one wins.
    """
    key = (norm_name(row["name"]), row["contact_type"])
    person_id = person_index.get(key)
    if person_id is not None:
        # Fill the blanks only. The 133 fiches seeded from the old press CRM have
        # a name and nothing else, so they gain everything; a fiche a moderator
        # actually filled in keeps every value they typed. `stance` counts as
        # blank while it is still the default 'Inconnu' — nobody chose that.
        db.execute(
            """
            UPDATE persons
               SET email        = CASE WHEN COALESCE(TRIM(email), '') = ''
                                       THEN ? ELSE email END,
                   social_links = CASE WHEN COALESCE(TRIM(social_links), '') = ''
                                       THEN ? ELSE social_links END,
                   stance       = CASE WHEN stance = 'Inconnu' AND ? != 'Inconnu'
                                       THEN ? ELSE stance END,
                   notes        = CASE WHEN COALESCE(TRIM(notes), '') = ''
                                       THEN ? ELSE notes END
             WHERE id = ?
            """,
            (row["email"], row["social_links"], row["stance"], row["stance"],
             row["notes"], person_id),
        )
        action = "attached"
    else:
        person_id = db.execute(
            """
            INSERT INTO persons (name, contact_type, stance, email, social_links,
                notes, added_by, validated_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (row["name"], row["contact_type"], row["stance"], row["email"],
             row["social_links"], row["notes"], now),
        ).lastrowid
        person_index[key] = person_id
        action = "created"
    if row["media_name"]:
        link_person_media(db, person_id, row["media_name"], now, media_index)
    return person_id, action


def cmd_list_pending(db, args):
    rows = db.execute(
        "SELECT email FROM civicrm_pending WHERE status = 'pending' "
        "ORDER BY seen_count DESC, first_seen LIMIT ?",
        (args.limit,),
    ).fetchall()
    for (email,) in rows:
        print(email)
    return 0


def cmd_stats(db, _args):
    log("File d'attente CiviCRM :")
    for status, total in db.execute(
        "SELECT status, COUNT(*) FROM civicrm_pending GROUP BY status ORDER BY status"
    ):
        log(f"  {status:<10} {total}")
    return 0


def cmd_retry_absent(db, args):
    cur = db.execute(
        "UPDATE civicrm_pending SET status = 'pending', resolved_at = NULL "
        "WHERE status = 'absent'"
    )
    if args.commit:
        db.commit()
        log(f"{cur.rowcount} adresse(s) remise(s) en file.")
    else:
        log(f"[dry-run] {cur.rowcount} adresse(s) seraient remises en file.")
    return 0


def cmd_apply(db, args):
    with open(args.apply, encoding="utf-8") as fh:
        records = json.load(fh)
    try:
        assert_contract(records, CONTACT_FIELDS, "contact")
    except ContractError as exc:
        log(f"ABANDON — contrat CiviCRM non respecté : {exc}")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]

    by_email = {}
    for record in records:
        mail = clean_email(record.get("email_primary.email"))
        if mail:
            by_email.setdefault(mail, record)

    pending = [r[0] for r in db.execute(
        "SELECT email FROM civicrm_pending WHERE status = 'pending'")]
    person_index = load_person_index(db)
    media_index = load_media_index(db)
    known = existing_emails(db)

    created = attached = absent = already = 0
    for address in pending:
        record = by_email.get(address)
        if record is None:
            db.execute(
                "UPDATE civicrm_pending SET status = 'absent', resolved_at = ? "
                "WHERE email = ?", (now, address))
            absent += 1
            continue
        if address in known:
            # Someone typed the fiche in while the lookup was in flight.
            db.execute(
                "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ? "
                "WHERE email = ?", (now, address))
            already += 1
            continue
        row = contact_to_person(record, today)
        if row is None or not row["email"]:
            db.execute(
                "UPDATE civicrm_pending SET status = 'absent', resolved_at = ? "
                "WHERE email = ?", (now, address))
            absent += 1
            continue
        if args.commit:
            person_id, action = create_or_attach(
                db, row, now, person_index, media_index)
            db.execute(
                "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ?, "
                "person_id = ? WHERE email = ?", (now, person_id, address))
        else:
            action = ("attached"
                      if (norm_name(row["name"]), row["contact_type"]) in person_index
                      else "created")
        known.add(address)
        if action == "created":
            created += 1
        else:
            attached += 1
        media = f" — {row['media_name']}" if row["media_name"] else ""
        log(f"  {'+' if action == 'created' else '~'} {row['name']}{media} "
            f"<{row['email']}>")

    if args.commit:
        db.commit()
    prefix = "" if args.commit else "[dry-run] "
    log(f"{prefix}Terminé. Fiches créées : {created} | fiches complétées : "
        f"{attached} | déjà connues : {already} | inconnues de CiviCRM : {absent} "
        f"| en file au départ : {len(pending)}.")
    if created or attached:
        log("Relancez ensuite : import_member_mails.py --backfill "
            "(les mails en attente se rattacheront aux nouvelles fiches).")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-pending", action="store_true",
                       help="print the addresses awaiting a CiviCRM lookup")
    group.add_argument("--apply", metavar="FILE",
                       help="JSON written by `cv api4 Contact.get`")
    group.add_argument("--stats", action="store_true", help="queue counts by status")
    group.add_argument("--retry-absent", action="store_true",
                       help="put addresses CiviCRM didn't know back in the queue")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to meetings.db")
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is saved")
    parser.add_argument("--limit", type=int, default=500,
                        help="max addresses printed by --list-pending")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_civicrm_tables(db)
        if args.list_pending:
            return cmd_list_pending(db, args)
        if args.stats:
            return cmd_stats(db, args)
        if args.retry_absent:
            return cmd_retry_absent(db, args)
        return cmd_apply(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
