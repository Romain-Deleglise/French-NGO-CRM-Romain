"""Which e-mail domains belong to an organisation we follow — read from the data.

The mail importers used to carry three domains in the source:

    OFFICIAL_DOMAINS = ("senat.fr", "assemblee-nationale.fr", "europarl.europa.eu")

That was fine while this CRM only tracked élu·es. Since the press CRM was merged
in, journalists are `persons` too — and they are spread over hundreds of média
domains that no one is going to maintain by hand. So the list is now derived from
the addresses already in the database, and it widens on its own as fiches arrive
(from CiviCRM, from moderation, or typed in).

What the list is used for, and why widening it is safe:

- **Finding candidate addresses quoted in a mail body.** A reply usually quotes
  the original, which still carries the real address even when the reply's `From`
  is something else. Every candidate is then resolved against `persons.email`, so
  a domain that turns out to be irrelevant simply matches nothing.
- **Deciding whether an address is "someone we already know of".** This one does
  have teeth: an address on a known domain is not queued for a CiviCRM lookup,
  and is not learned as an alias. Hence the freemail rule below.

**Freemail is never a known domain.** Several journalists use gmail.com; that
tells us nothing about who a *new* gmail address belongs to. On a freemail domain
only the full address identifies a person, never the domain.

    python3 utils/maildomains.py --list        # the domains, one per line
    python3 utils/maildomains.py --google-rule # ready to paste into Workspace
"""
import argparse
import os
import re
import sqlite3
import sys

# Always known, even against an empty database: the three parliamentary domains
# the importers were written around. Keeping them as a floor means a fresh
# install behaves exactly as before the list became data-driven.
SEED_DOMAINS = ("senat.fr", "assemblee-nationale.fr", "europarl.europa.eu")

# Consumer mailbox providers. An address here identifies a person only in full,
# so these can never be treated as an organisation's domain — however many of our
# contacts happen to use them.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com",
    "hotmail.fr", "live.com", "live.fr", "msn.com", "yahoo.com", "yahoo.fr",
    "ymail.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr", "laposte.net",
    "bbox.fr", "numericable.fr", "aliceadsl.fr", "club-internet.fr",
    "protonmail.com", "proton.me", "pm.me", "tutanota.com", "gmx.fr", "gmx.com",
    "mailo.com", "zoho.com", "yandex.com", "hushmail.com", "fastmail.com",
})

# A domain has to be shared by at least this many known people before we call it
# an organisation's. One person on a domain is just as likely to be a personal
# address on their own name.
MIN_PERSONS_PER_DOMAIN = 2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# Module state, deliberately: both importers already work with module-level
# constants, and `refresh(db)` keeps the change to their call sites minimal.
# Untouched, this behaves exactly like the old hard-coded tuple.
_known = frozenset(SEED_DOMAINS)
_pattern = None


def domain_of(address):
    """The domain part of an address, lower-cased, or None."""
    address = (address or "").strip().lower()
    if "@" not in address:
        return None
    return address.rsplit("@", 1)[1] or None


def _compile(domains):
    if not domains:
        return re.compile(r"(?!)")   # matches nothing, never None
    alternatives = "|".join(re.escape(d) for d in sorted(domains, key=len,
                                                         reverse=True))
    return re.compile(r"[\w.\-+]+@(?:" + alternatives + r")\b", re.I)


_pattern = _compile(_known)


def collect(db, min_persons=MIN_PERSONS_PER_DOMAIN):
    """Domains worth knowing, read from `persons` and the learned aliases.

    Tolerant of `person_emails` not existing yet: it is created by the member
    import, which may not have run on a fresh database.
    """
    counts = {}

    def tally(address, person_id):
        domain = domain_of(address)
        if domain and domain not in FREEMAIL_DOMAINS:
            counts.setdefault(domain, set()).add(person_id)

    for pid, mail in db.execute(
        "SELECT id, email FROM persons WHERE email IS NOT NULL AND email != ''"
    ):
        tally(mail, pid)
    try:
        for mail, pid in db.execute("SELECT email, person_id FROM person_emails"):
            tally(mail, pid)
    except sqlite3.Error:
        pass

    found = {d for d, people in counts.items() if len(people) >= min_persons}
    return frozenset(found | set(SEED_DOMAINS))


def refresh(db, min_persons=MIN_PERSONS_PER_DOMAIN):
    """Load the domain list out of the database. Call once, at start-up."""
    global _known, _pattern
    _known = collect(db, min_persons)
    _pattern = _compile(_known)
    return _known


def refresh_seed_only():
    """Fall back to the three parliamentary domains — the pre-merge behaviour."""
    global _known, _pattern
    _known = frozenset(SEED_DOMAINS)
    _pattern = _compile(_known)
    return _known


def known_domains():
    return _known


def is_known(address):
    """True when the address sits on a domain of an organisation we follow."""
    domain = domain_of(address)
    return bool(domain) and domain in _known


def find_addresses(text):
    """Every address in `text` sitting on a known domain, lower-cased.

    The caller still has to resolve each one against `persons.email`: this only
    narrows a mail body down to plausible candidates.
    """
    return {a.lower() for a in _pattern.findall(text or "")}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="print the known domains, one per line")
    group.add_argument("--google-rule", action="store_true",
                       help="print them as one line, to paste into the Workspace "
                            "content-compliance rule")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--min-persons", type=int, default=MIN_PERSONS_PER_DOMAIN,
                        help="how many known people a domain needs to count")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    try:
        domains = sorted(collect(db, args.min_persons))
    finally:
        db.close()

    if args.list:
        for domain in domains:
            print(domain)
        print(f"\n{len(domains)} domaine(s).", file=sys.stderr)
    else:
        # The Workspace rule copies a message when one side matches any of these.
        print(" ".join(domains))
        print(f"\n{len(domains)} domaine(s) — à coller dans la règle "
              f"« Conformité du contenu » qui alimente la boîte d'audit.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
