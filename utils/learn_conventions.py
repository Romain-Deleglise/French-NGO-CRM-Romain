#!/usr/bin/env python3
"""Learn every média's address convention from all of CiviCRM's journalists.

The conventions used to be derived from this CRM's own fiches — 587 journalists,
44 conventions. But CiviCRM holds ~12 900 of them with their addresses and their
média, and reading those teaches far more: hundreds of médias instead of dozens,
and each convention backed by many examples rather than two.

Crucially this creates **no fiche**. It reads names and addresses, keeps only
what it deduces from them — a domain, a média, a template, a count — and throws
the addresses away. The 12 900 journalists stay in CiviCRM where they belong;
what lands here is the shape of `<initiale><nom>@lefigaro.fr`, not the people.

Two things get better as a result:

- **Recognising an address nobody holds.** `mlefebvre@lefigaro.fr` traces back
  to an "M… Lefebvre" at Le Figaro even when no fiche and no CiviCRM address
  match, because the convention is known. With conventions for hundreds of
  médias instead of the few we happen to have fiches for, this reaches much
  further.
- **Spotting an address quoted in a mail body.** `maildomains` treats a domain
  as an organisation's once two known people share it. The learned table adds
  every média domain CiviCRM evidences, which is what lets a reply from another
  desk address be attributed (see AUTOMATISATION_MAILS_MEMBRES.md).

Reads the JSON that `cv api4 Contact.get` wrote, never CiviCRM's database.
Idempotent: the table is rebuilt from the export each run.

    python3 utils/learn_conventions.py --file civi-journalists.json
    python3 utils/learn_conventions.py --file civi-journalists.json --commit
    python3 utils/learn_conventions.py --show          # what is stored
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mailpatterns  # noqa: E402
from civicrm import clean_email, looks_like_an_address, norm_name  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# Freemail is never an organisation's domain, for the same reason as in
# maildomains: several journalists write from gmail, which says nothing about a
# *new* gmail address. Reused from there rather than restated.
from maildomains import FREEMAIL_DOMAINS  # noqa: E402


def log(msg):
    print(msg, flush=True)


def ensure_table(db):
    db.executescript(
        """
        -- One row per e-mail domain CiviCRM's journalists evidence. Holds no
        -- personal data: a domain, the média that owns it, how it builds local
        -- parts, and how many addresses backed that conclusion.
        CREATE TABLE IF NOT EXISTS mail_conventions (
            domain     TEXT PRIMARY KEY,
            media      TEXT,
            template   TEXT,
            examples   INTEGER NOT NULL DEFAULT 0,
            source     TEXT NOT NULL,
            learned_at TEXT NOT NULL
        );
        """
    )


def load_domains(db):
    """Every domain the learned table knows, for maildomains to widen with."""
    try:
        return {r[0] for r in db.execute("SELECT domain FROM mail_conventions")}
    except sqlite3.Error:
        return set()


def load_conventions(db):
    """{domain: {"media": …, "template": …}} for the resolver, or {}."""
    out = {}
    try:
        rows = db.execute(
            "SELECT domain, media, template FROM mail_conventions "
            "WHERE template IS NOT NULL AND media IS NOT NULL"
        )
    except sqlite3.Error:
        return out
    for domain, media, template in rows:
        out[domain] = {"media": media, "template": template}
    return out


def learn(records, min_examples=mailpatterns.MIN_EXAMPLES):
    """(rows, stats) from a CiviCRM journalist export.

    A row is (domain, média, template, examples). `template` may be None: a
    domain with enough addresses but no single convention is still worth
    recording, because knowing it belongs to a média helps the body scan even
    when no address can be rebuilt from a name.
    """
    pairs, per_domain, medias = [], {}, {}
    skipped = 0
    for record in records:
        name = (record.get("display_name") or "").strip()
        mail = clean_email(record.get("email_primary.email"))
        if not name or not mail or looks_like_an_address(name):
            skipped += 1
            continue
        domain = mail.rpartition("@")[2]
        if not domain or domain in FREEMAIL_DOMAINS:
            skipped += 1
            continue
        pairs.append((name, mail))
        per_domain[domain] = per_domain.get(domain, 0) + 1
        media = (record.get("employer_id.display_name") or "").strip()
        if media:
            medias.setdefault(domain, {})
            medias[domain][media] = medias[domain].get(media, 0) + 1

    templates = mailpatterns.learn(pairs, min_examples)

    rows = []
    for domain, count in sorted(per_domain.items()):
        if count < min_examples:
            continue
        owner = None
        if domain in medias:
            # A domain carries the occasional mistyped employer; the média most
            # of its addresses point at is the one that owns it.
            owner = max(medias[domain].items(), key=lambda kv: kv[1])[0]
        rows.append((domain, owner, templates.get(domain), count))
    return rows, {
        "records": len(records),
        "usable": len(pairs),
        "skipped": skipped,
        "domains": len(rows),
        "with_template": sum(1 for r in rows if r[2]),
    }


def store(db, rows, source, now):
    db.execute("DELETE FROM mail_conventions WHERE source = ?", (source,))
    db.executemany(
        "INSERT OR REPLACE INTO mail_conventions "
        "(domain, media, template, examples, source, learned_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(d, m, t, n, source, now) for d, m, t, n in rows],
    )


def cmd_show(db, args):
    ensure_table(db)
    rows = db.execute(
        "SELECT domain, COALESCE(media, '—'), COALESCE(template, '—'), examples "
        "FROM mail_conventions ORDER BY examples DESC, domain LIMIT ?",
        (args.limit,),
    ).fetchall()
    for domain, media, template, examples in rows:
        log(f"  {examples:>4}× {domain:<28} {template:<12} {media}")
    total, with_t = db.execute(
        "SELECT COUNT(*), SUM(template IS NOT NULL) FROM mail_conventions"
    ).fetchone()
    log(f"\n{total or 0} domaine(s) connu(s), dont {with_t or 0} avec une "
        f"convention d'adresses.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", help="JSON written by `cv api4 Contact.get`")
    parser.add_argument("--show", action="store_true",
                        help="print what is already stored")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is saved")
    parser.add_argument("--limit", type=int, default=40,
                        help="rows printed (default 40)")
    parser.add_argument("--min-examples", type=int,
                        default=mailpatterns.MIN_EXAMPLES,
                        help="addresses a domain needs before it counts")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    try:
        if args.show:
            return cmd_show(db, args)
        if not args.file:
            parser.error("--file ou --show est requis")

        with open(args.file, encoding="utf-8") as fh:
            records = json.load(fh)
        rows, stats = learn(records, args.min_examples)

        log(f"{stats['records']} contact(s) lu(s), {stats['usable']} utilisable(s), "
            f"{stats['skipped']} écarté(s) (sans nom, sans adresse, messagerie "
            f"grand public).")
        log(f"{stats['domains']} domaine(s) retenu(s), dont "
            f"{stats['with_template']} avec une convention identifiée.")

        by_template = {}
        for _d, _m, template, _n in rows:
            key = template or "(aucune)"
            by_template[key] = by_template.get(key, 0) + 1
        log("Répartition : " + ", ".join(
            f"{n}× {t}" for t, n in sorted(by_template.items(),
                                           key=lambda kv: -kv[1])))

        for domain, media, template, examples in sorted(
                rows, key=lambda r: -r[3])[:args.limit]:
            log(f"  {examples:>4}× {domain:<28} {template or '—':<12} "
                f"{media or '—'}")

        if args.commit:
            ensure_table(db)
            store(db, rows, "civicrm", datetime.now(timezone.utc)
                  .isoformat(timespec="seconds"))
            db.commit()
            log(f"\n{len(rows)} domaine(s) enregistré(s).")
        else:
            log("\n[dry-run] rien n'a été écrit.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
