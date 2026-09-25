"""The whole bridge, end to end, on a synthetic CiviCRM export.

Every unit here was already covered, and the chain was still broken in
production for weeks: `--patterns` printed its summary onto the JSON that
civicrm-sync.sh parses, the caller's `|| echo {}` turned the parse failure into
"no convention applies", and nothing failed loudly. The units were right about
their own assumptions; nobody checked that they fitted together.

So this walks the real sequence, in the real order, through the same entry
points the shell script calls:

    learn_conventions --file      (1b/5)  learn the newsroom's convention
    civicrm_lookup --patterns     (4b/5)  which conventions the queue needs
    civicrm_lookup --apply-names  (4b/5)  resolve the queued address by name

and asserts on what the shell script actually consumes — stdout parsed as JSON,
then a fiche in `persons`. A future change that breaks the plumbing rather than
the logic fails here.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import civicrm_lookup as cl  # noqa: E402
import learn_conventions as lc  # noqa: E402
import maildomains as md  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# A newsroom as CiviCRM holds one: dozens of addresses on one domain, regional
# employer labels, a duplicate contact, and one historical exception that the
# old unanimity rule would have let veto the whole thing.
NEWSROOM = [("Emmanuel Pall", "emmanuel.pall"),
            ("Claire Bouchouchi", "claire.bouchouchi"),
            ("Marc Lefebvre", "marc.lefebvre"),
            ("Anne Duval", "anne.duval"),
            ("Paul Simon", "paul.simon"),
            ("Luc Renard", "luc.renard")]


def contact(cid, name, local, media, domain="francetv.fr"):
    return {
        "id": cid,
        "display_name": name,
        "contact_sub_type": ["Journaliste"],
        "email_primary.email": f"{local}@{domain}",
        "employer_id.display_name": media,
        "Analyse_strat_gique_Pause_IA.Alignement": "1",
        "Analyse_strat_gique_Pause_IA.Niveau_d_influence": "2",
        "Description_courte.Description_courte": "",
    }


def export():
    records, cid = [], 100
    for index, (name, local) in enumerate(NEWSROOM):
        region = "FRANCE 3 OCCITANIE" if index % 2 else "FRANCE 3 PARIS"
        records.append(contact(cid, name, local, region))
        cid += 1
    # A duplicate fiche for one of them — CiviCRM is full of these.
    records.append(contact(cid, "Emmanuel Pall", "e.pall", "FRANCE 3 NORD"))
    cid += 1
    # And the historical exception that unanimity used to choke on.
    records.append(contact(cid, "Jean Historique", "jhisto", "FRANCE 3 PARIS"))
    return records


class ChainTests(unittest.TestCase):
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
        cl.ensure_civicrm_tables(self.db)
        self.addCleanup(self.db.close)

    def _file(self, payload):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                            encoding="utf-8")
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _quiet(self, fn, *args):
        """Run a command, returning its stdout — stderr is the human summary."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            fn(*args)
        return out.getvalue()

    def test_the_whole_sequence_resolves_a_never_seen_address(self):
        # A member wrote to an address nobody holds and CiviCRM has no record of.
        # It is queued, exactly as import_member_mails would queue it.
        cl.enqueue(self.db, "anne.duval@francetv.fr", "", NOW)

        # 1b/5 — learn from the export.
        rows, stats = lc.learn(export())
        lc.ensure_table(self.db)
        lc.store(self.db, rows, "civicrm", NOW)
        self.db.commit()
        self.assertEqual(stats["with_template"], 1)
        learned = lc.load_conventions(self.db)["francetv.fr"]
        self.assertEqual(learned["template"], "prenom.nom")

        # 4b/5, first half — what the shell script parses out of stdout.
        stdout = self._quiet(cl.cmd_patterns, self.db,
                             types.SimpleNamespace(all=False))
        wanted = json.loads(stdout)
        self.assertIn("francetv.fr", wanted)

        # 4b/5, second half — resolve the queued address against those names.
        self._quiet(cl.cmd_apply_names, self.db, types.SimpleNamespace(
            apply_names=self._file(export()), commit=True,
            include_other=False))

        status, person_id = self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            ("anne.duval@francetv.fr",)).fetchone()
        self.assertEqual(status, "resolved")
        name, notes = self.db.execute(
            "SELECT name, notes FROM persons WHERE id = ?",
            (person_id,)).fetchone()
        self.assertEqual(name, "Anne Duval")
        self.assertIn("francetv.fr", notes)
        self.assertIn("à confirmer", notes)

    def test_the_domain_becomes_known_to_the_body_scan(self):
        # The other half of the point: once CiviCRM evidences the domain, an
        # address quoted in a mail body is worth extracting, even though this
        # CRM holds a single fiche there.
        self.assertNotIn("francetv.fr", md.collect(self.db))
        rows, _ = lc.learn(export())
        lc.ensure_table(self.db)
        lc.store(self.db, rows, "civicrm", NOW)
        md.refresh(self.db)
        self.addCleanup(md.refresh_seed_only)
        self.assertIn("marc.lefebvre@francetv.fr",
                      md.find_addresses("écrit par marc.lefebvre@francetv.fr hier"))

    def test_an_export_that_teaches_nothing_leaves_the_queue_untouched(self):
        cl.enqueue(self.db, "willa@godemandguide.co", "", NOW)
        lc.ensure_table(self.db)
        lc.store(self.db, lc.learn(export())[0], "civicrm", NOW)
        stdout = self._quiet(cl.cmd_patterns, self.db,
                             types.SimpleNamespace(all=False))
        # Valid JSON, and empty: nothing queued sits on a known convention.
        self.assertEqual(json.loads(stdout), {})
        status, = self.db.execute(
            "SELECT status FROM civicrm_pending WHERE email = ?",
            ("willa@godemandguide.co",)).fetchone()
        self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
