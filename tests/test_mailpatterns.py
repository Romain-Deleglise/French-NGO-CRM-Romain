"""Cover the per-média address conventions, on the real Le Figaro examples.

The whole point is to recognise an address we have never seen from a name we
know. The risk is mis-attribution, so most of these tests are about the cases
where the module must refuse to answer.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import mailpatterns as mp  # noqa: E402

# Straight out of the CiviCRM export: Le Figaro uses <initial><surname>.
FIGARO = [
    ("Tristan Vey", "tvey@lefigaro.fr"),
    ("Eugenie Bastie", "ebastie@lefigaro.fr"),
    ("Caroline De Malet", "cdemalet@lefigaro.fr"),
    ("Keren Lentschner", "klentschner@lefigaro.fr"),
]
TRIBUNE = [("Sylvain Rolland", "srolland@latribune.fr")]


class SplitNameTests(unittest.TestCase):
    def test_splits_and_folds(self):
        self.assertEqual(mp.split_name("Eugénie Bastié"), ("eugenie", "bastie"))

    def test_a_compound_surname_is_joined(self):
        self.assertEqual(mp.split_name("Caroline De Malet"), ("caroline", "demalet"))

    def test_civility_is_not_read_as_a_first_name(self):
        # Without this, "M. Olivier Tesquet" teaches the domain that its
        # convention builds on the first name "m".
        self.assertEqual(mp.split_name("M. Olivier Tesquet"),
                         ("olivier", "tesquet"))
        self.assertEqual(mp.split_name("Mme Alexia Borg"), ("alexia", "borg"))

    def test_a_civility_only_name_gives_nothing(self):
        self.assertIsNone(mp.split_name("Mme"))

    def test_a_mononym_gives_nothing_to_build_on(self):
        for value in ("Jojol", "", None, "   "):
            self.assertIsNone(mp.split_name(value))


class LearnTests(unittest.TestCase):
    def test_le_figaro_convention_is_read_off_the_real_addresses(self):
        self.assertEqual(mp.learn(FIGARO), {"lefigaro.fr": "pnom"})

    def test_one_example_is_not_a_convention(self):
        # A single address matches several templates at once; calling that a
        # convention would mis-attribute every later mail from that domain.
        self.assertEqual(mp.learn(TRIBUNE), {})

    def test_a_domain_that_mixes_conventions_gets_none(self):
        mixed = [("Jean Dupont", "jean.dupont@mixte.fr"),
                 ("Marie Martin", "mmartin@mixte.fr"),
                 ("Paul Durand", "durand@mixte.fr")]
        self.assertNotIn("mixte.fr", mp.learn(mixed))

    def test_prenom_point_nom_is_learned_too(self):
        pairs = [("Jean Dupont", "jean.dupont@lemonde.fr"),
                 ("Marie Martin", "marie.martin@lemonde.fr")]
        self.assertEqual(mp.learn(pairs), {"lemonde.fr": "prenom.nom"})

    def test_an_address_matching_nothing_does_not_veto_the_convention(self):
        # A desk address sitting among real ones must not blind the learner.
        pairs = FIGARO + [("Tristan Vey", "scoop2024@lefigaro.fr")]
        self.assertEqual(mp.learn(pairs), {"lefigaro.fr": "pnom"})

    def test_several_domains_at_once(self):
        pairs = FIGARO + [("Jean Dupont", "jean.dupont@lemonde.fr"),
                          ("Marie Martin", "marie.martin@lemonde.fr")]
        self.assertEqual(mp.learn(pairs),
                         {"lefigaro.fr": "pnom", "lemonde.fr": "prenom.nom"})


class ResolveTests(unittest.TestCase):
    """Recognising an address we have never seen, from names CiviCRM knows."""

    CANDIDATES = [(1, "Pierre Dupont"), (2, "Marie Lefebvre"),
                  (3, "Caroline De Malet")]

    def test_an_unseen_address_finds_its_journalist(self):
        hit = mp.resolve("pdupont@lefigaro.fr", "pnom", self.CANDIDATES)
        self.assertEqual(hit, (1, "Pierre Dupont"))

    def test_a_particle_surname_resolves_both_ways(self):
        self.assertIsNotNone(
            mp.resolve("cdemalet@lefigaro.fr", "pnom", self.CANDIDATES))
        self.assertIsNotNone(
            mp.resolve("cmalet@lefigaro.fr", "pnom", self.CANDIDATES))

    def test_an_ambiguous_address_is_refused_rather_than_guessed(self):
        # Two journalists collapsing to the same local part: picking either
        # would be a coin toss recorded as a fact.
        twins = [(1, "Pierre Dupont"), (2, "Paul Dupont")]
        self.assertIsNone(mp.resolve("pdupont@lefigaro.fr", "pnom", twins))

    def test_no_candidate_matches(self):
        self.assertIsNone(
            mp.resolve("zzz@lefigaro.fr", "pnom", self.CANDIDATES))

    def test_a_plus_tag_is_ignored(self):
        self.assertIsNotNone(
            mp.resolve("pdupont+news@lefigaro.fr", "pnom", self.CANDIDATES))

    def test_a_malformed_address_resolves_to_nothing(self):
        for value in ("", None, "pas-une-adresse"):
            self.assertIsNone(mp.resolve(value, "pnom", self.CANDIDATES))


class BuildTests(unittest.TestCase):
    def test_each_template_renders_as_expected(self):
        for template, expected in (("prenom.nom", "jean.dupont"),
                                   ("pnom", "jdupont"),
                                   ("p.nom", "j.dupont"),
                                   ("nomprenom", "dupontjean"),
                                   ("nom", "dupont")):
            self.assertEqual(mp.build(template, "jean", "dupont"), expected)

    def test_an_unknown_template_builds_nothing(self):
        self.assertIsNone(mp.build("inexistant", "jean", "dupont"))


if __name__ == "__main__":
    unittest.main()


class DominanceTests(unittest.TestCase):
    """A majority learns the convention; a genuine mix still learns none.

    Unanimity was the first rule and it collapsed on real data: francetv.fr has
    2 055 addresses in CiviCRM, overwhelmingly `prenom.nom`, and a handful of
    historical exceptions vetoed the whole newsroom.
    """

    def _pairs(self, names, template):
        out = []
        for first, last in names:
            local = {"prenom.nom": f"{first}.{last}".lower(),
                     "pnom": f"{first[0]}{last}".lower()}[template]
            out.append((f"{first} {last}", f"{local}@francetv.fr"))
        return out

    NAMES = [("Emmanuel", "Pall"), ("Pierre", "Debaudouin"),
             ("Claire", "Bouchouchi"), ("Marc", "Lefebvre"),
             ("Anne", "Duval"), ("Paul", "Simon"), ("Luc", "Renard"),
             ("Sophie", "Marchand"), ("Yann", "Bertrand"), ("Zoe", "Camus")]

    def test_one_outlier_no_longer_vetoes_a_newsroom(self):
        pairs = self._pairs(self.NAMES, "prenom.nom")
        pairs.append(("Jean Historique", "jhistorique@francetv.fr"))
        self.assertEqual(mp.learn(pairs).get("francetv.fr"), "prenom.nom")

    def test_a_real_fifty_fifty_mix_is_still_refused(self):
        pairs = (self._pairs(self.NAMES[:5], "prenom.nom")
                 + self._pairs(self.NAMES[5:], "pnom"))
        self.assertIsNone(mp.learn(pairs).get("francetv.fr"))

    def test_the_share_is_reported(self):
        pairs = self._pairs(self.NAMES, "prenom.nom")
        pairs.append(("Jean Historique", "jhistorique@francetv.fr"))
        entry = mp.learn_shares(pairs)["francetv.fr"]
        self.assertEqual(entry["template"], "prenom.nom")
        self.assertEqual(entry["votes"], 11)
        self.assertAlmostEqual(entry["share"], 10 / 11)

    def test_unanimity_still_reads_as_a_full_share(self):
        entry = mp.learn_shares(self._pairs(self.NAMES, "pnom"))["francetv.fr"]
        self.assertEqual((entry["template"], entry["share"]), ("pnom", 1.0))

    def test_two_examples_remain_the_floor(self):
        entry = mp.learn_shares(self._pairs(self.NAMES[:1], "pnom"))
        self.assertEqual(entry, {})
