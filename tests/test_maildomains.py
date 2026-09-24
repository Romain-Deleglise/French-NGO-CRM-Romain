"""Cover the derived domain list: what counts as an organisation's domain.

The list used to be three constants in the source. Now it is read from the
fiches, so the tests here pin the two rules that keep that safe: freemail is
never an organisation, and one lone address on a domain proves nothing.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import maildomains as md  # noqa: E402

NOW = "2026-09-24T00:00:00+00:00"


class DomainOfTests(unittest.TestCase):
    def test_extracts_and_lowercases(self):
        self.assertEqual(md.domain_of("TVey@LeFigaro.FR"), "lefigaro.fr")

    def test_rejects_a_non_address(self):
        for value in ("", None, "pas-une-adresse", "@", "x@"):
            self.assertIsNone(md.domain_of(value), value)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact_type TEXT NOT NULL,
                stance TEXT NOT NULL, email TEXT, created_at TEXT NOT NULL
            );
            """
        )

    def tearDown(self):
        self.db.close()
        md.refresh_seed_only()

    def add(self, name, mail, ctype="Journaliste"):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES (?, ?, 'Inconnu', ?, ?)", (name, ctype, mail, NOW))

    def test_the_three_parliamentary_domains_are_always_there(self):
        self.assertEqual(md.collect(self.db), frozenset(md.SEED_DOMAINS))

    def test_a_media_domain_appears_once_two_journalists_share_it(self):
        self.add("Tristan Vey", "tvey@lefigaro.fr")
        self.assertNotIn("lefigaro.fr", md.collect(self.db))
        self.add("Eugénie Bastié", "ebastie@lefigaro.fr")
        self.assertIn("lefigaro.fr", md.collect(self.db))

    def test_freemail_never_becomes_an_organisation(self):
        # Journalists do use personal addresses; that must not turn gmail.com
        # into "a domain we follow" — every new gmail address would then be
        # treated as already known, and never looked up.
        for i in range(5):
            self.add(f"Pigiste {i}", f"pigiste{i}@gmail.com")
        self.assertNotIn("gmail.com", md.collect(self.db))

    def test_free_fr_and_orange_fr_are_freemail_too(self):
        for i in range(3):
            self.add(f"A {i}", f"a{i}@free.fr")
            self.add(f"B {i}", f"b{i}@orange.fr")
        found = md.collect(self.db)
        self.assertNotIn("free.fr", found)
        self.assertNotIn("orange.fr", found)

    def test_the_same_person_twice_is_still_one_person(self):
        # Two addresses on one domain, one fiche: not evidence of an organisation.
        self.add("Solo", "solo@petitmedia.fr")
        self.db.executescript(
            "CREATE TABLE person_emails (email TEXT PRIMARY KEY, "
            "person_id INTEGER, source TEXT, created_at TEXT);")
        self.db.execute(
            "INSERT INTO person_emails VALUES ('s.autre@petitmedia.fr', 1, 'x', ?)",
            (NOW,))
        self.assertNotIn("petitmedia.fr", md.collect(self.db))

    def test_learned_aliases_count_towards_a_domain(self):
        self.add("Un", "un@mediapart.fr")
        self.db.executescript(
            "CREATE TABLE person_emails (email TEXT PRIMARY KEY, "
            "person_id INTEGER, source TEXT, created_at TEXT);")
        self.db.execute(
            "INSERT INTO person_emails VALUES ('deux@mediapart.fr', 2, 'fil', ?)",
            (NOW,))
        self.assertIn("mediapart.fr", md.collect(self.db))

    def test_a_missing_person_emails_table_is_tolerated(self):
        self.add("Un", "un@x.fr")
        self.add("Deux", "deux@x.fr")
        self.assertIn("x.fr", md.collect(self.db))   # no person_emails here


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact_type TEXT NOT NULL,
                stance TEXT NOT NULL, email TEXT, created_at TEXT NOT NULL
            );
            """
        )
        for name, mail in (("Tristan Vey", "tvey@lefigaro.fr"),
                           ("Eugénie Bastié", "ebastie@lefigaro.fr")):
            self.db.execute(
                "INSERT INTO persons (name, contact_type, stance, email, created_at)"
                " VALUES (?, 'Journaliste', 'Inconnu', ?, ?)", (name, mail, NOW))

    def tearDown(self):
        self.db.close()
        md.refresh_seed_only()

    def test_untouched_the_module_behaves_like_the_old_constant(self):
        md.refresh_seed_only()
        self.assertTrue(md.is_known("x@senat.fr"))
        self.assertFalse(md.is_known("tvey@lefigaro.fr"))

    def test_refresh_widens_to_the_media_domains(self):
        md.refresh(self.db)
        self.assertTrue(md.is_known("tvey@lefigaro.fr"))
        self.assertTrue(md.is_known("x@senat.fr"))      # the floor still holds
        self.assertFalse(md.is_known("a@gmail.com"))

    def test_find_addresses_picks_candidates_out_of_a_quoted_body(self):
        md.refresh(self.db)
        body = ("Le 12 mars, Tristan Vey <TVEY@lefigaro.fr> a écrit :\n"
                "> bonjour, merci de me contacter sur perso@gmail.com\n"
                "Cc: greffe@senat.fr")
        found = md.find_addresses(body)
        self.assertIn("tvey@lefigaro.fr", found)        # lower-cased
        self.assertIn("greffe@senat.fr", found)
        self.assertNotIn("perso@gmail.com", found)

    def test_find_addresses_on_an_empty_set_matches_nothing(self):
        self.assertEqual(md.find_addresses(""), set())

    def test_a_domain_that_is_a_suffix_of_another_is_not_confused(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at)"
            " VALUES ('A', 'Journaliste', 'Inconnu', 'a@notlefigaro.fr', ?)", (NOW,))
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at)"
            " VALUES ('B', 'Journaliste', 'Inconnu', 'b@notlefigaro.fr', ?)", (NOW,))
        md.refresh(self.db)
        self.assertTrue(md.is_known("c@notlefigaro.fr"))
        self.assertTrue(md.is_known("c@lefigaro.fr"))
        self.assertFalse(md.is_known("c@figaro.fr"))


if __name__ == "__main__":
    unittest.main()
