"""Le dépôt de la page de veille, sans compte sur le CRM et sans accès serveur.

La page est produite hors du CRM, par un script qui lit les comptes rendus de
l'Assemblée, du Sénat et du Parlement européen. Elle change souvent. Sans ce
dépôt, chaque mise à jour passerait par quelqu'un qui a les accès serveur, et
ce quelqu'un deviendrait le goulot d'étranglement.

Ce qui est verrouillé ici : la page n'est jamais servie sans authentification,
le dépôt exige un jeton distinct du mot de passe du CRM, et un fichier abîmé
ne remplace jamais celui qui marche.
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

JSON_VALIDE = json.dumps({
    "genere_le": "2026-10-15",
    "personnes": [{"nom": "Aurélien Saintoul", "chambre": "Assemblée nationale"}],
}).encode("utf-8")


class VeilleTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        self.client = app.app.test_client()
        # Un dossier neuf par test : on remplace des fichiers, pas question
        # qu'un test hérite de ce qu'un autre a déposé.
        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        self._dir = app.VEILLE_DIR
        app.VEILLE_DIR = __import__("pathlib").Path(dossier.name) / "veille"
        self.addCleanup(setattr, app, "VEILLE_DIR", self._dir)
        self._token = app.VEILLE_TOKEN
        app.VEILLE_TOKEN = "jeton-de-test"
        self.addCleanup(setattr, app, "VEILLE_TOKEN", self._token)

    def connecte(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def depose(self, nom, contenu, token="jeton-de-test"):
        return self.client.post(
            "/veille/televerser",
            data={"fichier": (io.BytesIO(contenu), nom)},
            headers={"X-Veille-Token": token},
            content_type="multipart/form-data",
        )

    # --- le dépôt ---------------------------------------------------------
    def test_un_depot_avec_le_jeton_est_accepte(self):
        self.assertEqual(self.depose("synthese_elus.json", JSON_VALIDE).status_code, 200)
        self.assertTrue((self.app.VEILLE_DIR / "synthese_elus.json").exists())

    def test_sans_jeton_rien_nest_ecrit(self):
        self.assertEqual(self.depose("synthese_elus.json", JSON_VALIDE, "").status_code, 403)
        self.assertFalse((self.app.VEILLE_DIR / "synthese_elus.json").exists())

    def test_un_mauvais_jeton_non_plus(self):
        self.assertEqual(
            self.depose("synthese_elus.json", JSON_VALIDE, "presque").status_code, 403)

    def test_sans_jeton_configure_le_serveur_le_dit(self):
        self.app.VEILLE_TOKEN = ""
        r = self.depose("synthese_elus.json", JSON_VALIDE)
        self.assertEqual(r.status_code, 503)
        self.assertIn(b"VEILLE_UPLOAD_TOKEN", r.data)

    def test_un_autre_nom_de_fichier_est_refuse(self):
        self.assertEqual(self.depose("app.py", b"print()").status_code, 400)

    def test_un_chemin_dans_le_nom_ne_sort_pas_du_dossier(self):
        self.assertEqual(self.depose("../../app.py", b"<html>").status_code, 400)

    # --- un fichier abîmé ne remplace jamais le bon ------------------------
    def test_un_json_tronque_est_refuse(self):
        self.depose("synthese_elus.json", JSON_VALIDE)
        r = self.depose("synthese_elus.json", b'{"personnes": [')
        self.assertEqual(r.status_code, 400)
        garde = (self.app.VEILLE_DIR / "synthese_elus.json").read_bytes()
        self.assertEqual(garde, JSON_VALIDE)

    def test_un_json_sans_personnes_est_refuse(self):
        r = self.depose("synthese_elus.json",
                        json.dumps({"genere_le": "2026-10-15", "personnes": []}).encode())
        self.assertEqual(r.status_code, 400)

    def test_un_fichier_vide_est_refuse(self):
        self.assertEqual(self.depose("synthese_elus.json", b"").status_code, 400)

    def test_la_version_precedente_est_gardee(self):
        self.depose("synthese_elus.json", JSON_VALIDE)
        autre = json.dumps({"genere_le": "2026-10-16",
                            "personnes": [{"nom": "X"}]}).encode()
        self.depose("synthese_elus.json", autre)
        self.assertEqual(
            (self.app.VEILLE_DIR / "synthese_elus.json.bak").read_bytes(), JSON_VALIDE)

    def test_aucun_fichier_provisoire_ne_reste(self):
        self.depose("synthese_elus.json", JSON_VALIDE)
        self.assertEqual(list(self.app.VEILLE_DIR.glob("*.tmp")), [])

    # --- la lecture --------------------------------------------------------
    def test_la_page_nest_jamais_servie_sans_authentification(self):
        self.depose("index.html", b"<html>veille</html>")
        r = self.client.get("/veille/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_les_donnees_non_plus(self):
        self.depose("synthese_elus.json", JSON_VALIDE)
        r = self.client.get("/veille/synthese_elus.json")
        self.assertEqual(r.status_code, 302)

    def test_connecte_la_page_saffiche(self):
        self.depose("index.html", b"<html>veille</html>")
        self.connecte()
        r = self.client.get("/veille/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"veille", r.data)

    def test_la_page_porte_une_politique_de_securite(self):
        # Elle vient d'un dépôt tiers : elle ne doit pas pouvoir envoyer
        # ailleurs ce qu'elle lit sur l'origine du CRM.
        self.depose("index.html", b"<html>veille</html>")
        self.connecte()
        csp = self.client.get("/veille/").headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("form-action 'none'", csp)

    def test_sans_barre_oblique_on_redirige(self):
        # Sinon le fetch relatif de la page chercherait /synthese_elus.json.
        self.connecte()
        r = self.client.get("/veille")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/veille/"))

    def test_rien_dautre_nest_servi_depuis_ce_dossier(self):
        self.connecte()
        (self.app.VEILLE_DIR).mkdir(parents=True, exist_ok=True)
        (self.app.VEILLE_DIR / "secret.txt").write_text("x")
        self.assertEqual(self.client.get("/veille/secret.txt").status_code, 404)

    def test_sans_page_deposee_on_lexplique(self):
        self.connecte()
        r = self.client.get("/veille/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("televerser", r.data.decode())


if __name__ == "__main__":
    unittest.main()
