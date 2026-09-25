"""Le pipeline citoyen, aligné sur celui des membres — sauf sur un point.

Deux écarts avaient été laissés derrière : l'index des adresses ignorait
`person_emails` (un courriel adressé à l'adresse de cabinet d'un·e élu·e n'était
rattaché à personne, alors que le pipeline des membres sait le faire), et une
adresse inconnue était abandonnée en silence au lieu d'entrer dans la file
d'identification.

L'écart qui RESTE, et qui est voulu : ici l'expéditeur est le citoyen, et son
identité n'est jamais stockée. La mise en file ne porte donc que sur les
destinataires. La symétrie du pipeline des membres serait ici une fuite.
"""
import email
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import civicrm_lookup as cl  # noqa: E402
import import_campaign_mails as ic  # noqa: E402
import maildomains as md  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def un_mail(de, a, objet="Sécurité de l'IA", mid="<c1@exemple.fr>"):
    return email.message_from_string(
        f"From: {de}\r\nTo: {a}\r\nSubject: {objet}\r\n"
        f"Message-ID: {mid}\r\nDate: Thu, 24 Sep 2026 10:00:00 +0200\r\n"
        f"\r\nBonjour,\r\n")


class CampaignPipelineTests(unittest.TestCase):
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
        cl.ensure_civicrm_tables(self.db)
        ic.ensure_state_table(self.db)
        self.depute = self.db.execute(
            "INSERT INTO persons (name, email) "
            "VALUES ('Sandrine Rousseau', 'sandrine.rousseau@assemblee-nationale.fr')"
        ).lastrowid
        self.db.commit()
        md.refresh_scope(self.db)
        self.addCleanup(md.refresh_seed_only)
        self.addCleanup(self.db.close)

    def _queued(self):
        return {r[0] for r in self.db.execute("SELECT email FROM civicrm_pending")}

    def test_a_secondary_address_now_matches(self):
        # L'élue reçoit sur son adresse de cabinet ; sa fiche porte l'adresse
        # parlementaire. Le pipeline des membres savait déjà faire ce lien.
        self.db.execute(
            "INSERT INTO person_emails (email, person_id, source, created_at) "
            "VALUES ('contact@cabinet-rousseau.fr', ?, 'fil', ?)",
            (self.depute, NOW))
        self.db.commit()
        index = ic.load_email_index(self.db)
        self.assertIn("contact@cabinet-rousseau.fr", index)
        self.assertEqual(index["contact@cabinet-rousseau.fr"][0][0], self.depute)

    def test_the_index_still_holds_the_main_addresses(self):
        index = ic.load_email_index(self.db)
        self.assertIn("sandrine.rousseau@assemblee-nationale.fr", index)

    def test_an_index_survives_a_database_without_the_alias_table(self):
        neuve = sqlite3.connect(":memory:")
        self.addCleanup(neuve.close)
        neuve.executescript(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, email TEXT);")
        self.assertEqual(ic.load_email_index(neuve), {})

    def test_an_unknown_recipient_on_a_public_domain_is_queued(self):
        # Un·e élu·e local·e sans fiche : exactement ce que la file existe pour
        # récupérer.
        ic.queue_unknown_recipients(
            self.db, un_mail("citoyen@gmail.com", "maire@mairie-nantes.fr"))
        self.assertIn("maire@mairie-nantes.fr", self._queued())

    def test_the_citizen_is_never_queued(self):
        # LE point du test : l'expéditeur est le citoyen. Son adresse ne doit
        # apparaître nulle part, même si elle relevait d'un domaine suivi.
        ic.queue_unknown_recipients(
            self.db,
            un_mail("militant@mairie-nantes.fr", "inconnu@mairie-lyon.fr"))
        file_attente = self._queued()
        self.assertIn("inconnu@mairie-lyon.fr", file_attente)
        self.assertNotIn("militant@mairie-nantes.fr", file_attente)

    def test_an_out_of_scope_recipient_is_not_queued(self):
        ic.queue_unknown_recipients(
            self.db, un_mail("citoyen@gmail.com", "contact@plombier-92.fr"))
        self.assertEqual(self._queued(), set())

    def test_handle_message_reports_the_queued_case(self):
        etat = ic.handle_message(
            self.db, un_mail("citoyen@gmail.com", "maire@mairie-nantes.fr"),
            None, "test", ic.load_email_index(self.db),
            dry_run=False, auto_publish=False, verbose=False)
        self.assertEqual(etat, "queued")

    def test_a_dry_run_queues_nothing(self):
        ic.handle_message(
            self.db, un_mail("citoyen@gmail.com", "maire@mairie-nantes.fr"),
            None, "test", ic.load_email_index(self.db),
            dry_run=True, auto_publish=False, verbose=False)
        self.assertEqual(self._queued(), set())


if __name__ == "__main__":
    unittest.main()
