"""Cover the CiviCRM bridge: the contract, the value mapping, and the queue.

The mapping is the part a CiviCRM upgrade can silently break, so the contract
test here is the same guard the scripts rely on at runtime.
"""
import email
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import civicrm as cc  # noqa: E402
import civicrm_lookup as cl  # noqa: E402
from import_civicrm_medias import load_media_index, upsert_media  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")
TODAY = NOW[:10]

# A record shaped exactly as `cv api4 Contact.get` returns one, taken from the
# real export (Le Figaro's newsroom uses <initial><surname>@lefigaro.fr).
FIGARO = {
    "id": 792,
    "display_name": "Tristan Vey",
    "contact_sub_type": ["Journaliste"],
    "email_primary.email": "tvey@lefigaro.fr",
    "employer_id.display_name": "LE FIGARO",
    "Analyse_strat_gique_Pause_IA.Alignement": "2",
    "Analyse_strat_gique_Pause_IA.Niveau_d_influence": "3",
    "Description_courte.Description_courte": "Sciences et technologies.",
    "Compte_R_seaux_Sociaux.Twitter": "https://x.com/TristanVey",
    "Compte_R_seaux_Sociaux.LinkedIn": "",
}


class ContractTests(unittest.TestCase):
    def test_complete_export_passes(self):
        cc.assert_contract([FIGARO], cc.CONTACT_FIELDS)

    def test_empty_export_is_legitimate(self):
        cc.assert_contract([], cc.CONTACT_FIELDS)

    def test_a_missing_optional_field_only_warns(self):
        # Exactly what the 593-contact export did: CiviCRM stores the social
        # group as multi-record, so APIv4's dotted syntax never returns it.
        # Losing a Twitter handle must not refuse 593 journalists.
        export = dict(FIGARO)
        del export["Compte_R_seaux_Sociaux.Twitter"]
        del export["Compte_R_seaux_Sociaux.LinkedIn"]
        said = []
        absent = cc.assert_contract([export], cc.CONTACT_FIELDS, "contact",
                                    optional=cc.CONTACT_FIELDS_OPTIONAL,
                                    warn=said.append)
        self.assertEqual(set(absent), set(cc.CONTACT_FIELDS_OPTIONAL))
        self.assertTrue(said and "facultatif" in said[0])

    def test_a_required_field_still_stops_everything(self):
        export = dict(FIGARO)
        del export["employer_id.display_name"]
        with self.assertRaises(cc.ContractError) as ctx:
            cc.assert_contract([export], cc.CONTACT_FIELDS, "contact",
                               optional=cc.CONTACT_FIELDS_OPTIONAL, warn=None)
        self.assertIn("employer_id", str(ctx.exception))

    def test_one_odd_record_does_not_fail_the_whole_export(self):
        # Deciding from records[0] alone — as this did at first — turned a
        # single unusual row into a failed run of the entire export.
        odd = {"id": 1, "display_name": "Partiel"}
        with_odd_first = [odd] + [dict(FIGARO) for _ in range(5)]
        cc.assert_contract(with_odd_first, cc.CONTACT_FIELDS, "contact",
                           optional=cc.CONTACT_FIELDS_OPTIONAL, warn=None)

    def test_renamed_custom_field_is_caught(self):
        broken = dict(FIGARO)
        del broken["Analyse_strat_gique_Pause_IA.Alignement"]
        with self.assertRaises(cc.ContractError) as ctx:
            cc.assert_contract([broken], cc.CONTACT_FIELDS)
        self.assertIn("Alignement", str(ctx.exception))

    def test_non_list_is_caught(self):
        with self.assertRaises(cc.ContractError):
            cc.assert_contract({"id": 1}, cc.CONTACT_FIELDS)


class MappingTests(unittest.TestCase):
    def test_journalist_maps_whole_record(self):
        row = cc.contact_to_person(FIGARO, TODAY)
        self.assertEqual(row["name"], "Tristan Vey")
        self.assertEqual(row["contact_type"], "Journaliste")
        self.assertEqual(row["stance"], "Plutôt favorable")   # Alignement "2"
        self.assertEqual(row["email"], "tvey@lefigaro.fr")
        self.assertEqual(row["media_name"], "LE FIGARO")
        self.assertEqual(row["social_links"], "https://x.com/TristanVey")
        self.assertIn("Sciences et technologies.", row["notes"])
        self.assertIn("Élevé", row["notes"])                  # Niveau d'influence "3"

    def test_alignement_is_read_by_value_not_label(self):
        for value, stance in (("1", "Neutre / indécis"), ("3", "Favorable"),
                              ("4", "Opposé"), ("5", "Inconnu")):
            rec = dict(FIGARO, **{"Analyse_strat_gique_Pause_IA.Alignement": value})
            self.assertEqual(cc.contact_to_person(rec, TODAY)["stance"], stance)

    def test_missing_alignement_is_unknown(self):
        rec = dict(FIGARO, **{"Analyse_strat_gique_Pause_IA.Alignement": None})
        self.assertEqual(cc.contact_to_person(rec, TODAY)["stance"], "Inconnu")

    def test_unmapped_subtype_keeps_its_origin_in_the_notes(self):
        rec = dict(FIGARO, contact_sub_type=["Influenceur"])
        row = cc.contact_to_person(rec, TODAY)
        self.assertEqual(row["contact_type"], "Autre")
        self.assertIn("Influenceur", row["notes"])

    def test_multivalued_subtype_prefers_the_mapped_one(self):
        rec = dict(FIGARO, contact_sub_type=["B_n_vole", "Journaliste"])
        self.assertEqual(cc.contact_to_person(rec, TODAY)["contact_type"],
                         "Journaliste")

    def test_a_newsroom_desk_recorded_as_a_contact_is_dropped(self):
        # Fifteen of these turned up in the real 593-contact export of group 12.
        for desk in ("debats@lefigaro.fr", "standard@lepoint.fr",
                     "redaction@positivr.fr", "contributions@huffpost.fr",
                     "jean-marc.lalanne@inrocks.com"):
            self.assertTrue(cc.looks_like_an_address(desk), desk)
            self.assertIsNone(
                cc.contact_to_person(dict(FIGARO, display_name=desk), TODAY), desk)

    def test_a_real_name_is_not_mistaken_for_an_address(self):
        for name in ("Tristan Vey", "Caroline De Malet", "Jojol", "M. Tesquet"):
            self.assertFalse(cc.looks_like_an_address(name), name)

    def test_civility_is_stripped_from_the_fiche_name(self):
        # CiviCRM keeps "M. Olivier Tesquet" and "Mme Alexia Borg" as entered.
        for raw, expected in (("M. Olivier Tesquet", "Olivier Tesquet"),
                              ("Mme Alexia Borg", "Alexia Borg"),
                              ("Dr Jean Dupont", "Jean Dupont"),
                              ("Tristan Vey", "Tristan Vey")):
            row = cc.contact_to_person(dict(FIGARO, display_name=raw), TODAY)
            self.assertEqual(row["name"], expected)

    def test_nameless_record_is_dropped(self):
        self.assertIsNone(cc.contact_to_person(dict(FIGARO, display_name="  "),
                                               TODAY))

    def test_media_name_folding_matches_across_case_and_accents(self):
        self.assertEqual(cc.norm_name("LE FIGARO"), cc.norm_name("Le Figaro"))
        self.assertEqual(cc.norm_name("Médiapart"), cc.norm_name("MEDIAPART"))
        self.assertNotEqual(cc.norm_name("LE FIGARO"),
                            cc.norm_name("LE FIGARO ECONOMIE"))


class GenericAddressTests(unittest.TestCase):
    def test_newsroom_desks_are_not_worth_a_fiche(self):
        for addr in ("redaction@lemonde.fr", "contact@lefigaro.fr",
                     "no-reply@lemonde.fr", "presse@ngo.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_the_robots_the_first_real_run_actually_queued(self):
        # Every address the audit mailbox produced on 24/09: four robots, zero
        # journalists. They are the reason this filter exists.
        for addr in ("automated@airbnb.com", "notify@mail.notion.com",
                     "notify@mail.notion.so",
                     "bonjour@fresquedesrisquesdelia.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_the_robots_the_first_backfill_let_through(self):
        # Straight off the 2 079-message sweep: both were queued a dozen times
        # each before the filters grew to cover them.
        self.assertTrue(cl.is_generic("drive-shares-dm-noreply@google.com"))
        self.assertTrue(cl.is_generic("laredoute@news.laredoute.fr"))

    def test_a_robot_fragment_anywhere_in_the_local_part_counts(self):
        for addr in ("bounce-123@x.fr", "list-unsubscribe@y.org",
                     "auto-notification-42@z.com"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_a_sending_subdomain_is_bulk(self):
        for addr in ("x@news.leparisien.fr", "y@email.airbnb.com",
                     "z@mailing.example.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_a_journalist_on_a_plain_domain_still_passes(self):
        # The filters must not swallow the people this exists for.
        for addr in ("tvey@lefigaro.fr", "jboone@lesechos.fr",
                     "mtual@lemonde.fr", "a.grimonpont@leparisien.fr"):
            self.assertFalse(cl.is_generic(addr), addr)

    def test_a_two_label_domain_is_never_bulk_by_its_name(self):
        # "news.fr" would be a média, not a sending platform.
        self.assertFalse(cl.is_generic("redacteur@news.fr"))

    def test_our_own_domains_are_not_contacts(self):
        # The Fresque site is ours; a mail from it is internal, not a lead.
        self.assertTrue(cl.is_generic("quelquun@fresquedesrisquesdelia.org"))
        self.assertTrue(cl.is_generic("x@mail.fresquedesrisquesdelia.org"))

    def test_a_person_is(self):
        for addr in ("tvey@lefigaro.fr", "sylvain.rolland@latribune.fr",
                     "e.bastie@lefigaro.fr"):
            self.assertFalse(cl.is_generic(addr), addr)

    def test_a_malformed_address_is_skipped_rather_than_queued(self):
        for addr in ("", None, "pas-une-adresse"):
            self.assertTrue(cl.is_generic(addr))


class BulkMailTests(unittest.TestCase):
    """The structural filter: a machine writing to a list, whatever its address."""

    def _msg(self, headers):
        raw = "From: Someone <s@example.org>\r\nTo: m@pauseia.fr\r\n"
        raw += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        return email.message_from_string(raw + "\r\nbody\r\n")

    def test_list_unsubscribe_marks_a_newsletter(self):
        self.assertTrue(cl.is_bulk(self._msg(
            {"List-Unsubscribe": "<https://x.test/u>"})))

    def test_precedence_bulk_and_auto_submitted(self):
        self.assertTrue(cl.is_bulk(self._msg({"Precedence": "bulk"})))
        self.assertTrue(cl.is_bulk(self._msg(
            {"Auto-Submitted": "auto-generated"})))

    def test_auto_submitted_no_is_a_real_person(self):
        self.assertFalse(cl.is_bulk(self._msg({"Auto-Submitted": "no"})))

    def test_a_plain_message_is_not_bulk(self):
        self.assertFalse(cl.is_bulk(self._msg({"Subject": "Votre tribune"})))


class QueueAndApplyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact_type TEXT NOT NULL,
                stance TEXT NOT NULL, email TEXT, social_links TEXT, notes TEXT,
                added_by INTEGER, validated_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE organisations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, org_type TEXT NOT NULL, stance TEXT NOT NULL,
                notes TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE person_organisations (
                person_id INTEGER NOT NULL, organisation_id INTEGER NOT NULL,
                PRIMARY KEY (person_id, organisation_id)
            );
            """
        )
        # NB: person_emails is created by ensure_civicrm_tables below.
        cl.ensure_civicrm_tables(self.db)

    def tearDown(self):
        self.db.close()

    def _pending(self, email):
        return self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            (email,)).fetchone()

    def test_enqueue_is_idempotent_and_counts_sightings(self):
        self.assertTrue(cl.enqueue(self.db, "tvey@lefigaro.fr", "Tristan Vey", NOW))
        self.assertFalse(cl.enqueue(self.db, "TVEY@lefigaro.fr", "", NOW))
        count, = self.db.execute(
            "SELECT seen_count FROM civicrm_pending WHERE email = ?",
            ("tvey@lefigaro.fr",)).fetchone()
        self.assertEqual(count, 2)

    def test_generic_addresses_never_enter_the_queue(self):
        self.assertFalse(cl.enqueue(self.db, "redaction@lemonde.fr", "", NOW))
        self.assertIsNone(self._pending("redaction@lemonde.fr"))

    def test_apply_creates_the_fiche_and_links_the_media(self):
        cl.enqueue(self.db, "tvey@lefigaro.fr", "Tristan Vey", NOW)
        person_id, action = cl.create_or_attach(
            self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "created")
        name, ctype, mail = self.db.execute(
            "SELECT name, contact_type, email FROM persons WHERE id = ?",
            (person_id,)).fetchone()
        self.assertEqual((name, ctype, mail),
                         ("Tristan Vey", "Journaliste", "tvey@lefigaro.fr"))
        org, = self.db.execute(
            "SELECT o.name FROM organisations o "
            "JOIN person_organisations po ON po.organisation_id = o.id "
            "WHERE po.person_id = ?", (person_id,)).fetchone()
        self.assertEqual(org, "LE FIGARO")

    def test_existing_seeded_fiche_gains_the_address_instead_of_a_duplicate(self):
        # One of the 133 journalists seeded from the old press CRM: a name, no mail.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', ?)", (NOW,))
        person_id, action = cl.create_or_attach(
            self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "attached")
        total, = self.db.execute(
            "SELECT COUNT(*) FROM persons WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(total, 1)
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE id = ?", (person_id,)).fetchone()
        self.assertEqual(mail, "tvey@lefigaro.fr")

    def test_attaching_fills_the_blanks_of_a_seeded_fiche(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', ?)", (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        stance, social, notes = self.db.execute(
            "SELECT stance, social_links, notes FROM persons "
            "WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(stance, "Plutôt favorable")
        self.assertEqual(social, "https://x.com/TristanVey")
        self.assertIn("CiviCRM", notes)

    def test_attaching_never_overwrites_what_a_moderator_typed(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, social_links, notes, "
            "created_at) VALUES ('Tristan Vey', 'Journaliste', 'Opposé', "
            "'https://perso.fr', 'Rencontré en mars.', ?)", (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        stance, social, notes, mail = self.db.execute(
            "SELECT stance, social_links, notes, email FROM persons "
            "WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(stance, "Opposé")            # their judgement wins
        self.assertEqual(social, "https://perso.fr")
        self.assertEqual(notes, "Rencontré en mars.")
        self.assertEqual(mail, "tvey@lefigaro.fr")    # but the blank is filled

    def test_a_hand_typed_address_is_never_overwritten(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Favorable', 'perso@vey.fr', ?)",
            (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(mail, "perso@vey.fr")

    def test_the_same_media_is_not_created_twice_across_case(self):
        self.db.execute(
            "INSERT INTO organisations (name, org_type, stance, created_at) "
            "VALUES ('Le Figaro', 'Média', 'Inconnu', ?)", (NOW,))
        index = load_media_index(self.db)
        org_id, created = upsert_media(self.db, "LE FIGARO", NOW, index)
        self.assertFalse(created)
        total, = self.db.execute(
            "SELECT COUNT(*) FROM organisations WHERE org_type = 'Média'").fetchone()
        self.assertEqual(total, 1)
        self.assertIsNotNone(org_id)

    def test_a_second_address_is_kept_as_an_alias(self):
        # CiviCRM holds ~1.6 addresses per journalist. A member wrote to the
        # desk address; the fiche already carries the newsroom one. The second
        # has to stay matchable, or the next mail from it is queued again.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', 'tvey@lefigaro.fr', ?)",
            (NOW,))
        row = cc.contact_to_person(FIGARO, TODAY)
        row["email"] = "tristan.vey@lefigaro.fr"          # the matched address
        person_id, action = cl.create_or_attach(
            self.db, row, NOW, cl.load_person_index(self.db),
            load_media_index(self.db))
        self.assertEqual(action, "attached")
        alias = self.db.execute(
            "SELECT person_id, source FROM person_emails WHERE email = ?",
            ("tristan.vey@lefigaro.fr",)).fetchone()
        self.assertEqual(alias, (person_id, "civicrm"))
        # …and the fiche's own address is untouched.
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE id = ?", (person_id,)).fetchone()
        self.assertEqual(mail, "tvey@lefigaro.fr")

    def test_no_alias_row_when_the_address_is_the_fiche_s_own(self):
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        total, = self.db.execute("SELECT COUNT(*) FROM person_emails").fetchone()
        self.assertEqual(total, 0)

    def test_one_person_at_two_medias_stays_one_fiche(self):
        # Pierre Dandumont writes for MacGeneration and iGeneration; the real
        # export carries him twice. Anthony Morel likewise, for BFM and RMC.
        first = dict(FIGARO, display_name="Pierre Dandumont",
                     **{"employer_id.display_name": "MACGENERATION",
                        "email_primary.email": "pd@macg.fr"})
        second = dict(first, **{"employer_id.display_name": "IGENERATION",
                                "email_primary.email": "pd@igen.fr"})
        index, media_index = cl.load_person_index(self.db), load_media_index(self.db)
        pid1, a1 = cl.create_or_attach(
            self.db, cc.contact_to_person(first, TODAY), NOW, index, media_index)
        pid2, a2 = cl.create_or_attach(
            self.db, cc.contact_to_person(second, TODAY), NOW, index, media_index)
        self.assertEqual((a1, a2), ("created", "attached"))
        self.assertEqual(pid1, pid2)
        medias = [r[0] for r in self.db.execute(
            "SELECT o.name FROM organisations o "
            "JOIN person_organisations po ON po.organisation_id = o.id "
            "WHERE po.person_id = ? ORDER BY o.name", (pid1,))]
        self.assertEqual(medias, ["IGENERATION", "MACGENERATION"])

    def test_a_homonym_in_another_type_stays_a_separate_fiche(self):
        # "Laurent Alexandre" is both an LFI député and a chroniqueur.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Laurent Alexandre', 'Politique', 'Inconnu', ?)", (NOW,))
        rec = dict(FIGARO, display_name="Laurent Alexandre",
                   **{"email_primary.email": "lalexandre@lexpress.fr"})
        _pid, action = cl.create_or_attach(
            self.db, cc.contact_to_person(rec, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "created")
        total, = self.db.execute(
            "SELECT COUNT(*) FROM persons WHERE name = 'Laurent Alexandre'"
        ).fetchone()
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
