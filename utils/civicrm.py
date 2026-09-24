"""Mapping layer between CiviCRM and the CRM — the one file an upgrade can break.

PauseIA runs a second CRM, CiviCRM 6.15 Standalone, on the same server. It holds
~12 900 journalists with their e-mail address and their média, and it is the only
place those addresses exist. This module turns what `cv api4` prints into rows
this CRM understands.

Design, in three rules:

1. **We never touch CiviCRM's MySQL.** Its schema changes between major versions;
   its APIv4 does not. Everything here consumes the JSON that `cv api4 … ` writes
   (see utils/deploy/civicrm-sync.sh), so this code has no network, no
   credentials, and no dependency on CiviCRM's internals.
2. **One direction only, read-only.** CiviCRM → here. The mass-mailing base holds
   the donors and the Stripe/HelloAsso contributions; nothing in this CRM may
   write to it. The worst a bug here can do is import nothing.
3. **Contacts are pulled on demand, not in bulk.** A journalist gets a fiche the
   day someone actually exchanges mail with them (see civicrm_lookup.py), because
   this is a journal of interactions — copying 12 900 rows into it would make the
   Personnes page and the rencontre pickers unusable. Médias are the exception:
   168 organisations, no picker impact, imported in one go by
   import_civicrm_medias.py.

Everything CiviCRM-shaped lives here, so `assert_contract` is the single place
that fails loudly after a CiviCRM upgrade — instead of writing wrong data.
"""
import re
import unicodedata

# --------------------------------------------------------------------------- #
# The contract: what the cv api4 output must carry.
# --------------------------------------------------------------------------- #

# `select` clauses the sync script must ask for, and that we must find back in
# every record. Keys are CiviCRM APIv4 field names, including its implicit joins
# ("email_primary.email") and custom-group syntax ("<group>.<field>").
# Without these a fiche would be wrong rather than merely incomplete: no name,
# no média, no stance. Their absence stops the sync.
CONTACT_FIELDS_REQUIRED = (
    "id",
    "display_name",
    "contact_sub_type",
    "email_primary.email",
    "employer_id.display_name",
    "Analyse_strat_gique_Pause_IA.Alignement",
    "Analyse_strat_gique_Pause_IA.Niveau_d_influence",
    "Description_courte.Description_courte",
)

# Nice to have, and absent for a whole export when CiviCRM stores that custom
# group as a multi-record ("repeating") one — APIv4 does not expose those through
# the dotted syntax at all, it makes them a separate entity. Losing a Twitter
# handle must not stop 593 fiches from being created.
CONTACT_FIELDS_OPTIONAL = (
    "Compte_R_seaux_Sociaux.Twitter",
    "Compte_R_seaux_Sociaux.LinkedIn",
)

CONTACT_FIELDS = CONTACT_FIELDS_REQUIRED + CONTACT_FIELDS_OPTIONAL

# How many records to look at before deciding a field is missing. APIv4 normally
# returns the same keys for every row, but deciding from row 0 alone — as this
# did at first — turns one unusual record into a failed run.
CONTRACT_SAMPLE = 50

ORGANISATION_FIELDS = (
    "id",
    "display_name",
    "contact_sub_type",
)


class ContractError(RuntimeError):
    """CiviCRM returned something we don't recognise — stop, never guess."""


def assert_contract(records, fields, what="contact", optional=(), warn=print):
    """Fail loudly if the export lost a field we cannot do without.

    Called before a single row is written, so that a CiviCRM upgrade renaming a
    custom field stops the sync instead of quietly importing fiches with an
    empty média or a wrong stance.

    Two refinements this earned the hard way, on a 593-contact export:

    - It reads the keys of a *sample* of records, not of the first one. A single
      unusual row must not fail a whole run.
    - Fields listed in `optional` only produce a warning. A custom group stored
      as multi-record is simply not reachable through APIv4's dotted syntax, and
      a missing Twitter handle is no reason to refuse 593 journalists.

    Returns the optional fields that were absent, so the caller can say so.
    """
    if not isinstance(records, list):
        raise ContractError(f"expected a list of {what}s, got {type(records).__name__}")
    if not records:
        return ()  # An empty export is legitimate: nothing new to resolve.

    seen = set()
    for record in records[:CONTRACT_SAMPLE]:
        if isinstance(record, dict):
            seen.update(record)

    required = [f for f in fields if f not in seen and f not in optional]
    if required:
        raise ContractError(
            f"{what} export is missing {len(required)} required field(s): "
            + ", ".join(required)
            + ". CiviCRM was probably upgraded or a custom field renamed — check "
              "utils/deploy/civicrm-sync.sh against utils/civicrm.py before rerunning."
        )

    absent = tuple(f for f in optional if f not in seen)
    if absent and warn:
        warn(f"Champ(s) facultatif(s) absent(s) de l'export, ignoré(s) : "
             + ", ".join(absent)
             + ". Si c'est durable, retirez-les du `select` de civicrm-sync.sh — "
               "un groupe de champs personnalisés « multi-valeurs » n'est pas "
               "lisible par la syntaxe pointée d'APIv4.")
    return absent


# --------------------------------------------------------------------------- #
# Value mapping.
# --------------------------------------------------------------------------- #

# CiviCRM sub-types come back as *machine names* ("M_dia", "Expert_IA"), never
# as the labels shown in its interface, and the field is multi-valued.
SUBTYPE_JOURNALIST = "Journaliste"
SUBTYPE_MEDIA = "M_dia"

# CiviCRM individual sub-type -> this CRM's CONTACT_TYPES (see app.py). Anything
# absent lands in "Autre" with its original sub-type kept in the notes, so no
# contact is ever dropped for being unclassifiable.
CONTACT_TYPE_BY_SUBTYPE = {
    "Journaliste": "Journaliste",
    "B_n_vole": "Autre",
    "Sympathisant": "Autre",
    "Influenceur": "Autre",   # no Influenceur type here yet — see the README
    "Expert_IA": "Autre",
}

# « Alignement » (option group 104) is stored as a *value*, not a label: the
# labels carry emoji ("🔵 Partiel") and are edited freely in the interface.
# This CRM has one degree CiviCRM lacks ("Plutôt opposé"); nothing maps onto it,
# which costs nothing since we only ever read.
STANCE_BY_ALIGNEMENT = {
    "1": "Neutre / indécis",   # ⚪ Neutre
    "2": "Plutôt favorable",   # 🔵 Partiel
    "3": "Favorable",          # 🎯 Total
    "4": "Opposé",             # 🔴 Opposé
    "5": "Inconnu",            # ❓ Indéterminé
}
DEFAULT_STANCE = "Inconnu"

# « Niveau d'influence » (option group 105). No column here holds it, so it goes
# into the notes rather than being lost. NB: the CiviCRM handbook is out of date
# on these labels — these are the values actually configured.
INFLUENCE_BY_VALUE = {
    "1": "Faible",
    "2": "Modéré",
    "3": "Élevé",
    "4": "Très élevé",
}


def _as_list(value):
    """contact_sub_type is multi-valued: a list, a lone string, or absent."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def has_subtype(record, subtype):
    return subtype in _as_list(record.get("contact_sub_type"))


def norm_name(name):
    """Fold a name for comparison: accents, case, punctuation and spacing.

    CiviCRM stores médias in capitals ("LE FIGARO") while this CRM uses normal
    case ("Le Figaro"), so a raw comparison would duplicate all 165 existing
    médias on the first run.
    """
    s = unicodedata.normalize("NFKD", (name or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def clean_email(value):
    return (value or "").strip().lower() or None


# --------------------------------------------------------------------------- #
# Record mapping.
# --------------------------------------------------------------------------- #

NOTE_SOURCE = "Importé de CiviCRM"


def contact_to_person(record, today):
    """Turn one CiviCRM contact into the columns of a `persons` row.

    Returns a dict, or None when the record carries no usable name. `added_by`
    and `validated_by` are left out on purpose: NULL is this CRM's marker for
    "brought in by a script", which is what keeps backfills off hand-typed fiches.
    """
    name = (record.get("display_name") or "").strip()
    if not name:
        return None

    subtypes = _as_list(record.get("contact_sub_type"))
    contact_type = "Autre"
    for st in subtypes:
        if st in CONTACT_TYPE_BY_SUBTYPE:
            contact_type = CONTACT_TYPE_BY_SUBTYPE[st]
            if contact_type != "Autre":
                break

    alignement = record.get("Analyse_strat_gique_Pause_IA.Alignement")
    stance = STANCE_BY_ALIGNEMENT.get(str(alignement), DEFAULT_STANCE)

    social = [
        link.strip()
        for link in (record.get("Compte_R_seaux_Sociaux.Twitter"),
                     record.get("Compte_R_seaux_Sociaux.LinkedIn"))
        if (link or "").strip()
    ]

    notes = [f"{NOTE_SOURCE} (contact #{record.get('id')}) le {today}."]
    desc = (record.get("Description_courte.Description_courte") or "").strip()
    if desc:
        notes.append(desc)
    influence = INFLUENCE_BY_VALUE.get(
        str(record.get("Analyse_strat_gique_Pause_IA.Niveau_d_influence")))
    if influence:
        notes.append(f"Niveau d'influence (CiviCRM) : {influence}.")
    # Keep the original sub-type visible whenever it didn't map to a real type,
    # so "Autre" never hides what CiviCRM actually knew.
    if contact_type == "Autre" and subtypes:
        notes.append("Sous-type CiviCRM : " + ", ".join(subtypes) + ".")

    return {
        "name": name,
        "contact_type": contact_type,
        "stance": stance,
        "email": clean_email(record.get("email_primary.email")),
        "social_links": "\n".join(social) or None,
        "notes": "\n".join(notes),
        "media_name": (record.get("employer_id.display_name") or "").strip() or None,
        "civicrm_id": record.get("id"),
    }


def organisation_to_media(record, today):
    """Turn one CiviCRM organisation into the columns of an `organisations` row.

    `media_type` and `orientation` are deliberately left unset: CiviCRM holds
    neither, and inventing them would overwrite what a moderator curated here.
    """
    name = (record.get("display_name") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "org_type": "Média",
        "stance": DEFAULT_STANCE,
        "notes": f"{NOTE_SOURCE} (organisation #{record.get('id')}) le {today}.\n"
                 "Type de média et orientation à renseigner — CiviCRM ne les porte pas.",
        "civicrm_id": record.get("id"),
    }
