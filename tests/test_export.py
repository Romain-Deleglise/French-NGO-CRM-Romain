"""L'export des échanges : ce qu'il contient, et ce qu'il laisse dehors.

Un export sert à relire une série d'entretiens ou à la transmettre. Deux choses
comptent donc, et sont vérifiées ici : qu'un aller-retour se relise dans
l'ordre, fil par fil, et que le filtre par type de contact dise la vérité — un
export « élu·es » qui glisserait un échange avec la presse serait pire qu'un
export vide, puisque personne ne le remarquerait.
"""
import csv
import io
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import export_exchanges as ex  # noqa: E402

NOW = datetime.now(timezone.utc)
ISO = NOW.isoformat(timespec="seconds")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE persons (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, contact_type TEXT);
            CREATE TABLE organisations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, org_type TEXT);
            CREATE TABLE person_organisations (person_id INT, organisation_id INT);
            CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, email TEXT);
            CREATE TABLE mails (id INTEGER PRIMARY KEY AUTOINCREMENT,
                mail_date TEXT, direction TEXT, subject TEXT, summary TEXT);
            CREATE TABLE mail_persons (mail_id INT, person_id INT);
            CREATE TABLE mail_members (mail_id INT, member_id INT);
            CREATE TABLE mail_bodies (mail_id INT PRIMARY KEY, body TEXT);
            CREATE TABLE mail_thread (mail_id INT PRIMARY KEY, thread_key TEXT);
            """
        )
        self.membre = self.db.execute(
            "INSERT INTO members (name, email) "
            "VALUES ('Maxime Fournes', 'maxime@pauseia.fr')").lastrowid
        self.elue = self._personne("Sandrine Rousseau", "Politique",
                                   "Assemblée nationale")
        self.journaliste = self._personne("Morgane Tual", "Journaliste",
                                          "Le Monde")
        self.addCleanup(self.db.close)

    def _personne(self, nom, type_contact, organisation):
        pid = self.db.execute(
            "INSERT INTO persons (name, contact_type) VALUES (?, ?)",
            (nom, type_contact)).lastrowid
        oid = self.db.execute(
            "INSERT INTO organisations (name, org_type) VALUES (?, 'x')",
            (organisation,)).lastrowid
        self.db.execute("INSERT INTO person_organisations VALUES (?, ?)",
                        (pid, oid))
        return pid

    def _message(self, personne, jours, corps, fil="f1", sens="sent",
                 objet="Entretien", avec_membre=True):
        date = (NOW - timedelta(days=jours)).date().isoformat()
        mid = self.db.execute(
            "INSERT INTO mails (mail_date, direction, subject, summary) "
            "VALUES (?, ?, ?, ?)", (date, sens, objet, corps)).lastrowid
        self.db.execute("INSERT INTO mail_persons VALUES (?, ?)",
                        (mid, personne))
        if avec_membre:
            self.db.execute("INSERT INTO mail_members VALUES (?, ?)",
                            (mid, self.membre))
        self.db.execute("INSERT INTO mail_thread VALUES (?, ?)", (mid, fil))
        self.db.execute("INSERT INTO mail_bodies VALUES (?, ?)", (mid, corps))
        return mid

    def _lignes(self, **kwargs):
        return list(ex.en_lignes(ex.lire(self.db, **kwargs)))

    def test_a_thread_comes_back_in_order(self):
        self._message(self.elue, 10, "Premier", sens="sent")
        self._message(self.elue, 9, "Deuxième", sens="received")
        self._message(self.elue, 8, "Troisième", sens="sent")
        corps = [l["corps"] for l in self._lignes()]
        self.assertEqual(corps, ["Premier", "Deuxième", "Troisième"])

    def test_threads_do_not_interleave(self):
        # Deux fils qui se chevauchent dans le temps doivent rester groupés,
        # sinon la relecture suivie est perdue.
        self._message(self.elue, 10, "A1", fil="a")
        self._message(self.elue, 5, "B1", fil="b")
        self._message(self.elue, 4, "A2", fil="a")
        self._message(self.elue, 3, "B2", fil="b")
        self.assertEqual([l["corps"] for l in self._lignes()],
                         ["A1", "A2", "B1", "B2"])

    def test_the_contact_type_filter_tells_the_truth(self):
        self._message(self.elue, 5, "Avec une élue", fil="a")
        self._message(self.journaliste, 4, "Avec une journaliste", fil="b")
        elus = [l["corps"] for l in self._lignes(contact_type="Politique")]
        self.assertEqual(elus, ["Avec une élue"])
        presse = [l["corps"] for l in self._lignes(contact_type="Journaliste")]
        self.assertEqual(presse, ["Avec une journaliste"])

    def test_citizen_campaign_mails_are_left_out_by_default(self):
        # Ils n'ont pas de membre et leur « corps » est une ligne générée :
        # les exporter gonflerait le fichier sans rien apprendre.
        self._message(self.elue, 5, "Vrai échange", fil="a")
        self._message(self.elue, 4, "Mail d'un citoyen", fil="b",
                      avec_membre=False)
        self.assertEqual([l["corps"] for l in self._lignes()], ["Vrai échange"])
        tous = self._lignes(membres_seuls=False)
        self.assertEqual(len(tous), 2)

    def test_the_dates_bound_the_export(self):
        self._message(self.elue, 30, "Vieux", fil="a")
        self._message(self.elue, 2, "Récent", fil="b")
        limite = (NOW - timedelta(days=10)).date().isoformat()
        self.assertEqual([l["corps"] for l in self._lignes(depuis=limite)],
                         ["Récent"])

    def test_the_row_carries_who_and_where(self):
        self._message(self.elue, 5, "Bonjour")
        ligne = self._lignes()[0]
        self.assertEqual(ligne["membre"], "Maxime Fournes")
        self.assertEqual(ligne["interlocuteur"], "Sandrine Rousseau")
        self.assertEqual(ligne["type"], "Politique")
        self.assertEqual(ligne["organisation"], "Assemblée nationale")
        self.assertEqual(ligne["sens"], "Envoyé")

    def test_a_multi_line_body_survives_the_csv(self):
        # Le cas qui casse un export naïf : un corps de message contient des
        # retours à la ligne et des points-virgules, le séparateur du fichier.
        self._message(self.elue, 5, "Bonjour,\n\nTrois points ; un ; deux.\n\nMerci")
        tampon = io.StringIO()
        ex.ecrire_csv(self._lignes(), tampon)
        tampon.seek(0)
        relu = list(csv.DictReader(tampon, delimiter=";"))
        self.assertEqual(len(relu), 1)
        self.assertIn("Trois points ; un ; deux.", relu[0]["corps"])

    def test_the_text_output_names_who_wrote(self):
        self._message(self.elue, 5, "Bonjour", sens="sent")
        self._message(self.elue, 4, "Bonjour à vous", sens="received")
        tampon = io.StringIO()
        total, fils = ex.ecrire_txt(self._lignes(), tampon)
        texte = tampon.getvalue()
        self.assertEqual((total, fils), (2, 1))
        self.assertIn("Envoyé par Maxime Fournes", texte)
        self.assertIn("Reçu par Maxime Fournes", texte)
        self.assertIn("Sandrine Rousseau", texte)

    def test_an_empty_selection_is_not_an_error(self):
        self.assertEqual(self._lignes(contact_type="Religieux·se"), [])


if __name__ == "__main__":
    unittest.main()
