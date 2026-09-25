"""Cover the conventions learned from all of CiviCRM's journalists.

The point of `learn_conventions.py` is to recognise an address at a newsroom we
hold almost no fiche for — `cbouchouchi@nouvelobs.com` — because CiviCRM holds
hundreds of addresses there. So the tests pin: what a convention is deduced
from, what is refused, that no personal data is kept, and that the learned table
actually reaches the two consumers (`maildomains`, the resolver).
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import learn_conventions as lc  # noqa: E402
import maildomains as md  # noqa: E402

NOW = "2026-09-25T00:00:00+00:00"


def record(name, mail, media=None):
    return {"display_name": name, "email_primary.email": mail,
            "employer_id.display_name": media}


class LearnTests(unittest.TestCase):
    def test_deduces_a_template_and_its_owner(self):
        rows, stats = lc.learn([
            record("Claire Bouchouchi", "cbouchouchi@nouvelobs.com", "Le Nouvel Obs"),
            record("Marc Lefebvre", "mlefebvre@nouvelobs.com", "Le Nouvel Obs"),
            record("Anne Duval", "aduval@nouvelobs.com", "Le Nouvel Obs"),
        ])
        self.assertEqual(rows, [("nouvelobs.com", "Le Nouvel Obs", "pnom", 3)])
        self.assertEqual(stats["usable"], 3)
        self.assertEqual(stats["with_template"], 1)

    def test_a_dotted_newsroom_is_another_template(self):
        rows, _ = lc.learn([
            record("Emmanuel Pall", "emmanuel.pall@francetv.fr", "France Télévisions"),
            record("Pierre Debaudouin", "pierre.debaudouin@francetv.fr",
                   "France Télévisions"),
        ])
        self.assertEqual(rows[0][0], "francetv.fr")
        self.assertEqual(rows[0][2], "prenom.nom")

    def test_freemail_is_never_a_domain(self):
        rows, stats = lc.learn([
            record("Marie Durand", "marie.durand@orange.fr", "Le Nouvel Obs"),
            record("Paul Simon", "paul.simon@gmail.com", "Le Nouvel Obs"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["skipped"], 2)

    def test_a_desk_address_as_a_name_is_skipped(self):
        # CiviCRM carries newsroom desks whose display_name *is* an address.
        rows, stats = lc.learn([
            record("redaction@nouvelobs.com", "redaction@nouvelobs.com",
                   "Le Nouvel Obs"),
            record("service.info@nouvelobs.com", "service.info@nouvelobs.com",
                   "Le Nouvel Obs"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["skipped"], 2)

    def test_a_lone_address_proves_nothing(self):
        rows, _ = lc.learn([record("Anne Duval", "aduval@lemonde.fr", "Le Monde")])
        self.assertEqual(rows, [])

    def test_a_domain_without_one_convention_is_still_recorded(self):
        # Knowing the domain belongs to a média helps the body scan even when no
        # address can be rebuilt from a name.
        rows, stats = lc.learn([
            record("Anne Duval", "aduval@mixte.fr", "Mixte"),
            record("Marc Lefebvre", "marc.lefebvre@mixte.fr", "Mixte"),
            record("Paul Simon", "ps@mixte.fr", "Mixte"),
        ])
        self.assertEqual(rows, [("mixte.fr", "Mixte", None, 3)])
        self.assertEqual(stats["with_template"], 0)

    def test_the_owner_is_the_media_most_addresses_point_at(self):
        rows, _ = lc.learn([
            record("Anne Duval", "aduval@nouvelobs.com", "Le Nouvel Obs"),
            record("Marc Lefebvre", "mlefebvre@nouvelobs.com", "Le Nouvel Obs"),
            record("Paul Simon", "psimon@nouvelobs.com", "Obs (ancien nom)"),
        ])
        self.assertEqual(rows[0][1], "Le Nouvel Obs")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        lc.ensure_table(self.db)

    def tearDown(self):
        self.db.close()

    def test_keeps_no_address_and_no_name(self):
        rows, _ = lc.learn([
            record("Claire Bouchouchi", "cbouchouchi@nouvelobs.com", "Le Nouvel Obs"),
            record("Marc Lefebvre", "mlefebvre@nouvelobs.com", "Le Nouvel Obs"),
        ])
        lc.store(self.db, rows, "civicrm", NOW)
        dump = str(self.db.execute(
            "SELECT * FROM mail_conventions").fetchall())
        self.assertNotIn("cbouchouchi", dump)
        self.assertNotIn("Bouchouchi", dump)
        self.assertIn("nouvelobs.com", dump)

    def test_rerunning_replaces_rather_than_piles_up(self):
        rows, _ = lc.learn([
            record("Anne Duval", "aduval@nouvelobs.com", "Le Nouvel Obs"),
            record("Marc Lefebvre", "mlefebvre@nouvelobs.com", "Le Nouvel Obs"),
        ])
        lc.store(self.db, rows, "civicrm", NOW)
        lc.store(self.db, rows, "civicrm", NOW)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM mail_conventions").fetchone()[0], 1)

    def test_load_conventions_feeds_the_resolver(self):
        lc.store(self.db, [("nouvelobs.com", "Le Nouvel Obs", "pnom", 40),
                           ("mixte.fr", "Mixte", None, 9)], "civicrm", NOW)
        conventions = lc.load_conventions(self.db)
        # Only a domain with both a média and a template can resolve a name.
        self.assertEqual(conventions, {
            "nouvelobs.com": {"media": "Le Nouvel Obs", "template": "pnom"}})

    def test_load_is_silent_when_nothing_was_learned(self):
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        self.assertEqual(lc.load_conventions(empty), {})
        self.assertEqual(lc.load_domains(empty), set())


class KnownDomainTests(unittest.TestCase):
    """A learned domain counts as known, whatever our own fiche count."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, email TEXT
            );
            """
        )
        self.addCleanup(self.db.close)

    def test_a_single_fiche_is_enough_once_civicrm_evidences_the_domain(self):
        self.db.execute(
            "INSERT INTO persons (name, email) VALUES ('Claire B', ?)",
            ("cbouchouchi@nouvelobs.com",))
        # Without the learned table the two-person rule rejects it.
        self.assertNotIn("nouvelobs.com", md.collect(self.db))
        lc.ensure_table(self.db)
        lc.store(self.db, [("nouvelobs.com", "Le Nouvel Obs", "pnom", 40)],
                 "civicrm", NOW)
        self.assertIn("nouvelobs.com", md.collect(self.db))

    def test_freemail_stays_out_even_if_a_row_slipped_in(self):
        lc.ensure_table(self.db)
        lc.store(self.db, [("orange.fr", "Orange", "prenom.nom", 40)],
                 "civicrm", NOW)
        self.assertNotIn("orange.fr", md.collect(self.db))


class ResolverTests(unittest.TestCase):
    """The learned table must reach `civicrm_lookup`, not just sit there."""

    def test_the_resolver_sees_a_learned_convention(self):
        import civicrm_lookup  # imported here: it pulls in the whole bridge
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            """
            CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
            CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT,
                                        org_type TEXT);
            CREATE TABLE person_organisations (person_id INT, organisation_id INT);
            """
        )
        lc.ensure_table(db)
        lc.store(db, [("nouvelobs.com", "Le Nouvel Obs", "pnom", 40)],
                 "civicrm", NOW)
        self.assertEqual(civicrm_lookup.domain_conventions(db), {
            "nouvelobs.com": {"media": "Le Nouvel Obs", "template": "pnom"}})


if __name__ == "__main__":
    unittest.main()
