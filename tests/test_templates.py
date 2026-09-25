"""Tous les gabarits compilent, et les statiques qu'ils citent existent.

Jinja ne compile rien au démarrage : une faute de syntaxe dans un gabarit ne se
découvre qu'en ouvrant la page concernée — donc, pour une page peu visitée,
parfois des semaines plus tard. Ce test les compile tous d'un coup, avec
l'environnement réel de l'application (ses filtres maison : fr_date, fr_slot,
days_until…), pas avec un Jinja nu qui les ignorerait.
"""
import os
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utils"))


class TemplateTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        import app                                   # noqa: PLC0415
        self.app = app

    def test_every_template_compiles(self):
        gabarits = sorted((ROOT / "templates").glob("*.html"))
        self.assertGreater(len(gabarits), 30, "les gabarits n'ont pas été trouvés")
        erreurs = []
        for chemin in gabarits:
            try:
                self.app.app.jinja_env.get_template(chemin.name)
            except Exception as exc:                 # noqa: BLE001
                erreurs.append(f"{chemin.name} : {exc}")
        self.assertEqual(erreurs, [], "\n".join(erreurs))

    def test_the_static_files_the_templates_ask_for_exist(self):
        # `url_for('static', filename='…')` ne vérifie rien : un nom mal
        # orthographié donne un 404 silencieux, et une page sans style ou sans
        # son script se découvre à l'œil.
        cites = set()
        motif = re.compile(r"filename=['\"]([^'\"]+)['\"]")
        for chemin in (ROOT / "templates").glob("*.html"):
            cites.update(motif.findall(chemin.read_text()))
        manquants = [f for f in cites if not (ROOT / "static" / f).exists()]
        self.assertEqual(manquants, [], f"fichiers statiques absents : {manquants}")

    def test_every_route_the_templates_link_to_exists(self):
        # Un `url_for('nom_inexistant')` lève une erreur 500 à l'affichage.
        # Les compiler ne suffit pas : il faut confronter les noms aux routes.
        connus = {r.endpoint for r in self.app.app.url_map.iter_rules()}
        # Le `[,)]` final est indispensable : moderation.html construit un nom
        # de route par concaténation (`url_for('approve_pending_' ~ endpoint)`),
        # que ce test signalerait à tort comme une route inexistante.
        motif = re.compile(r"url_for\(\s*['\"]([a-zA-Z_][\w.]*)['\"]\s*[,)]")
        inconnus = set()
        for chemin in (ROOT / "templates").glob("*.html"):
            for nom in motif.findall(chemin.read_text()):
                if nom not in connus:
                    inconnus.add(f"{chemin.name} → {nom}")
        self.assertEqual(inconnus, set())


if __name__ == "__main__":
    unittest.main()
