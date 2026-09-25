"""Learn how each média builds its addresses, from the ones we already know.

Seeding one CiviCRM group gives us a few hundred real addresses. Those are worth
more than themselves: a newsroom almost always follows one convention, so a
handful of known addresses tells us how *every* address at that média is built.

    tvey@lefigaro.fr        Tristan Vey
    ebastie@lefigaro.fr     Eugénie Bastié      →  lefigaro.fr = <initiale><nom>
    cdemalet@lefigaro.fr    Caroline De Malet

Knowing that, a mail arriving from `pdupont@lefigaro.fr` can be traced back to a
"P… Dupont" who writes for Le Figaro — a person CiviCRM may well know by name
even when it does not hold that address.

**This recognises addresses. It never invents one.** Building an address from a
name and mailing it would be guessing at someone's contact details and quite
possibly writing to a stranger. Every template here is used in one direction
only: rebuild the candidate from a *known* person and compare it to an address
that actually turned up in the mailbox. A match is still routed as
low-confidence, because a convention is a habit, not a rule.

Learning is deliberately strict, but not unanimous. A domain gets a convention
when at least two known addresses agree on it AND a clear majority of them do —
one address always matches several templates, and a wrong convention silently
mis-attributes mail. Unanimity was the first rule, and it collapsed at scale:
`francetv.fr` has 2 055 addresses in CiviCRM, all but a handful `prenom.nom`,
and those few historical exceptions vetoed the whole newsroom. A majority rule
keeps the protection (a média that genuinely mixes conventions still gets none)
without letting one outlier silence 2 000 addresses.
"""
import re
import unicodedata

# At least this many known addresses must agree before a domain gets a
# convention. Two is the floor at which a template stops being a coincidence.
MIN_EXAMPLES = 2

# And they must be this share of the domain's usable addresses. Below it the
# newsroom really does mix conventions and gets none: rebuilding a name under a
# minority template would mis-attribute more mail than it identifies. At 0.6 a
# domain split 50/50 between two conventions is refused, while one lone
# exception among hundreds no longer vetoes anything.
MIN_SHARE = 0.6

# Particles dropped when reading a surname: newsrooms fold "De Malet" into
# "demalet" and "Le Proktor" into "leproktor", but almost never keep the space.
PARTICLES = ("de", "du", "des", "le", "la", "les", "van", "von", "di", "da")

# Dropped before reading a name — see split_name.
CIVILITIES = ("m", "mr", "mme", "mlle", "dr", "me", "pr")


def canon(text):
    """Accent-free, lower-case, letters only — the form newsrooms build on."""
    text = unicodedata.normalize("NFKD", (text or "").strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", text.lower())


def split_name(display_name):
    """(prénom, nom) from a display name, or None when it cannot be told apart.

    First token is the first name, the rest is the surname — the shape CiviCRM
    stores and the one `display_name` follows. A mononym gives nothing to build
    a template from, so it is skipped rather than guessed at.
    """
    tokens = [t for t in re.split(r"[\s\-_.]+", (display_name or "").strip()) if t]
    # "M. Olivier Tesquet": the civility is data entry, and reading "M" as the
    # first name would teach a wrong convention for the whole domain.
    while tokens and canon(tokens[0]) in CIVILITIES:
        tokens = tokens[1:]
    if len(tokens) < 2:
        return None
    prenom = canon(tokens[0])
    nom = "".join(canon(t) for t in tokens[1:])
    if not prenom or not nom:
        return None
    return prenom, nom


def _nom_variants(nom, display_name):
    """The surname as written, and without a leading particle.

    "Caroline De Malet" is `cdemalet@` at Le Figaro but could be `cmalet@`
    elsewhere, so both are candidates when we compare.
    """
    variants = {nom}
    tokens = [canon(t) for t in re.split(r"[\s\-_.]+", (display_name or "").strip())]
    tokens = [t for t in tokens if t][1:]
    while tokens and tokens[0] in PARTICLES:
        tokens = tokens[1:]
        joined = "".join(tokens)
        if joined:
            variants.add(joined)
    return variants


# Every template, as a function of (prénom, nom). Order matters only for
# readability; a domain keeps a template solely when it alone survives.
TEMPLATES = {
    "prenom.nom":  lambda p, n: f"{p}.{n}",
    "prenom_nom":  lambda p, n: f"{p}_{n}",
    "prenomnom":   lambda p, n: f"{p}{n}",
    "p.nom":       lambda p, n: f"{p[0]}.{n}",
    "pnom":        lambda p, n: f"{p[0]}{n}",
    "nom.prenom":  lambda p, n: f"{n}.{p}",
    "nomprenom":   lambda p, n: f"{n}{p}",
    "nom.p":       lambda p, n: f"{n}.{p[0]}",
    "nomp":        lambda p, n: f"{n}{p[0]}",
    "prenom.n":    lambda p, n: f"{p}.{n[0]}",
    "nom":         lambda p, n: n,
    "prenom":      lambda p, n: p,
}


def build(template, prenom, nom):
    """The local part this template gives for that person, or None."""
    fn = TEMPLATES.get(template)
    if not fn or not prenom or not nom:
        return None
    try:
        return fn(prenom, nom)
    except IndexError:
        return None


def templates_matching(local_part, display_name):
    """Every template under which `display_name` would produce `local_part`."""
    split = split_name(display_name)
    if not split:
        return set()
    prenom, _nom = split
    found = set()
    for nom in _nom_variants(_nom, display_name):
        for name, fn in TEMPLATES.items():
            try:
                if fn(prenom, nom) == local_part:
                    found.add(name)
            except IndexError:
                continue
    return found


def learn(pairs, min_examples=MIN_EXAMPLES, min_share=MIN_SHARE):
    """{domain: template} from (display_name, address) pairs we already trust.

    A domain keeps the template that explains the largest share of its addresses,
    provided `min_examples` back it and that share reaches `min_share`. Newsrooms
    that genuinely mix conventions — or that we know too little about — get none,
    and fall through to the generic matcher instead of being matched wrongly.

    `learn_shares` is the same computation with the evidence kept; this wrapper
    exists because most callers only want the verdict.
    """
    return {d: entry["template"] for d, entry in learn_shares(
        pairs, min_examples, min_share).items() if entry["template"]}


def learn_shares(pairs, min_examples=MIN_EXAMPLES, min_share=MIN_SHARE):
    """{domain: {"template", "share", "examples", "votes"}} — the evidence too.

    `template` is None when no convention reaches `min_share`; the domain is
    still reported, because knowing a domain exists is useful on its own (see
    maildomains). `share` is the fraction of the domain's usable addresses the
    winning template explains, `examples` how many addresses voted at all.
    """
    per_domain = {}
    for display_name, address in pairs:
        address = (address or "").strip().lower()
        if "@" not in address:
            continue
        local, _, domain = address.partition("@")
        local = local.split("+", 1)[0]
        matches = templates_matching(local, display_name)
        if not matches:
            # An address that fits no template at all (a nickname, a desk) says
            # nothing about the convention — it must not veto it either.
            continue
        entry = per_domain.setdefault(domain, {"sets": [], "count": 0})
        entry["sets"].append(matches)
        entry["count"] += 1

    learned = {}
    for domain, entry in per_domain.items():
        count = entry["count"]
        if count < min_examples:
            continue
        votes = {}
        for candidates in entry["sets"]:
            for template in candidates:
                votes[template] = votes.get(template, 0) + 1
        best = max(votes.values())
        winners = [t for t, v in votes.items() if v == best]
        # Several templates tie — e.g. every first name at the domain happens to
        # be unique, so "prenom" explains as much as "prenom.nom". Prefer the
        # most specific (the longest rendering), the one carrying both parts.
        winner = sorted(winners, key=lambda t: (
            -len(build(t, "prenom", "nom") or ""), t))[0]
        share = best / count
        keep = best >= min_examples and share >= min_share
        learned[domain] = {
            "template": winner if keep else None,
            "share": share if keep else 0.0,
            "examples": best if keep else 0,
            "votes": count,
        }
    return learned


def matches(local_part, display_name, template):
    """Would this person's address be `local_part` under the domain's convention?"""
    split = split_name(display_name)
    if not split:
        return False
    prenom, nom = split
    for variant in _nom_variants(nom, display_name):
        if build(template, prenom, variant) == local_part:
            return True
    return False


def resolve_all(address, template, candidates):
    """Every candidate whose address this would be, under that convention."""
    address = (address or "").strip().lower()
    if "@" not in address:
        return []
    local = address.partition("@")[0].split("+", 1)[0]
    return [c for c in candidates if matches(local, c[1], template)]


def resolve(address, template, candidates):
    """The one candidate whose address this would be, or None.

    `candidates` is (id, display_name). Ambiguity is refused outright: two
    journalists at the same média whose names collapse to the same local part
    cannot be told apart, and picking one would be a coin toss recorded as fact.
    Callers wanting to tell "nobody matched" from "several did" — the two are
    worth different follow-ups — should use resolve_all.
    """
    hits = resolve_all(address, template, candidates)
    return hits[0] if len(hits) == 1 else None
