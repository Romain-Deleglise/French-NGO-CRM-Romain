#!/usr/bin/env bash
# Bridge CiviCRM -> the CRM, from the host. Read-only on the CiviCRM side.
#
# The two apps run in separate Docker containers on the same server: CiviCRM in
# `civicrm-web`, this CRM in `website-meeting-app` with its SQLite inside the
# container. Neither can call the other, and we deliberately keep it that way —
# no API key, no `authx`, nothing exposed. So the host plays go-between: it asks
# CiviCRM with `cv` (which needs no credentials locally), hands the JSON over
# with `docker cp`, and lets this CRM's own scripts do the writing.
#
# Everything here is `cv api4 … .get` — reads only. Nothing in this file, and
# nothing it calls, can write to CiviCRM.
#
# Run order matters: médias first, so a journalist resolved in step 3 finds their
# média already there.
#
#   sudo /opt/scripts/civicrm-sync.sh            # dry run, writes nothing
#   sudo /opt/scripts/civicrm-sync.sh --commit
#
set -euo pipefail

# The press scope, by group id. Ids rather than titles is Romain's call as the
# CiviCRM maintainer, and it matches how mosaicotweaks already pins them:
#   12  Presse - Nationale
#   28  Presse - Régionale      (+ its ~40 département children)
#   69  Presse - Par thématique (+ its children)
# A child group inherits its parent's contacts in CiviCRM's `groups` filter, so
# the three parents cover the lot. If the press groups are ever renumbered, this
# line and mosaicotweaks.php are the two places to change.
PRESS_GROUP_IDS="${PRESS_GROUP_IDS:-12,28,69}"

CIVI_CONTAINER="${CIVI_CONTAINER:-civicrm-web}"
CRM_CONTAINER="${CRM_CONTAINER:-website-meeting-app}"
CIVI_CWD="${CIVI_CWD:-/var/www/html}"
REPO="${REPO:-/opt/volunteer-apps/apps/website-meeting}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

COMMIT=""
[ "${1:-}" = "--commit" ] && COMMIT="--commit"

say() { printf '\n== %s\n' "$*"; }

# cv prints PHP startup warnings on stderr; keep stdout clean for the JSON.
civi() { docker exec "$CIVI_CONTAINER" cv api4 "$1" "$2" --cwd="$CIVI_CWD" 2>/dev/null; }

say "0/5  Refreshing utils/ inside $CRM_CONTAINER"
docker exec "$CRM_CONTAINER" rm -rf /app/utils
docker cp "$REPO/utils" "$CRM_CONTAINER:/app/utils"

# --------------------------------------------------------------------------- #
say "1/5  Médias (organisations) from CiviCRM"
# Sub-types are machine names, never the interface labels: "M_dia", not "Média".
civi Contact.get \
  "{\"select\":[\"id\",\"display_name\",\"contact_sub_type\"],\"where\":[[\"contact_type\",\"=\",\"Organization\"],[\"contact_sub_type\",\"CONTAINS\",\"M_dia\"],[\"groups\",\"IN\",[$PRESS_GROUP_IDS]],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-medias.json"
# NB: scoped to the press groups *and* the M_dia sub-type. Those groups also hold
# ~2 300 organisations with no sub-type at all — almost certainly médias too, but
# that is a CiviCRM data-quality fix, not something to guess at here.
echo "   $(grep -c '"id"' "$WORK/civi-medias.json" || true) média(s) exporté(s)"

docker cp "$WORK/civi-medias.json" "$CRM_CONTAINER:/tmp/civi-medias.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/import_civicrm_medias.py \
  --file /tmp/civi-medias.json $COMMIT

# --------------------------------------------------------------------------- #
say "2/5  Addresses awaiting a lookup"
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py --list-pending \
  > "$WORK/pending.txt"
PENDING=$(wc -l < "$WORK/pending.txt" | tr -d ' ')
echo "   $PENDING adresse(s) en file"

if [ "$PENDING" -eq 0 ]; then
  say "Rien à résoudre. Terminé."
  exit 0
fi

# --------------------------------------------------------------------------- #
say "3/5  Asking CiviCRM about them"
# One query for the whole batch. `on_hold`/`do_not_email`/`is_opt_out` are NOT
# filtered here on purpose: we are identifying a person we already exchanged mail
# with, not deciding whether to mail them. A bounced address still names someone.
EMAILS=$(python3 - "$WORK/pending.txt" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.dumps([l.strip() for l in fh if l.strip()]))
PY
)
# Two steps on purpose. CiviCRM holds ~1.6 addresses per journalist (21 401 for
# 12 987 contacts), and the one a member wrote to is often NOT the primary — so
# filtering Contact.get on `email_primary.email` silently misses them. Ask the
# Email entity which contact each address belongs to, then fetch those contacts.
civi Email.get \
  "{\"select\":[\"email\",\"contact_id\"],\"where\":[[\"email\",\"IN\",$EMAILS]],\"limit\":0}" \
  > "$WORK/civi-emails.json"
echo "   $(grep -c '"email"' "$WORK/civi-emails.json" || true) adresse(s) reconnue(s)"

IDS=$(python3 - "$WORK/civi-emails.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    rows = json.load(fh)
print(json.dumps(sorted({r["contact_id"] for r in rows if r.get("contact_id")})))
PY
)
civi Contact.get \
  "{\"select\":[\"id\",\"display_name\",\"contact_sub_type\",\"email_primary.email\",\"employer_id.display_name\",\"Analyse_strat_gique_Pause_IA.Alignement\",\"Analyse_strat_gique_Pause_IA.Niveau_d_influence\",\"Description_courte.Description_courte\",\"Compte_R_seaux_Sociaux.Twitter\",\"Compte_R_seaux_Sociaux.LinkedIn\"],\"where\":[[\"id\",\"IN\",$IDS],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-contacts.json"
echo "   $(grep -c '"id"' "$WORK/civi-contacts.json" || true) contact(s) trouvé(s)"

# --------------------------------------------------------------------------- #
say "4/5  Creating the fiches"
docker cp "$WORK/civi-contacts.json" "$CRM_CONTAINER:/tmp/civi-contacts.json"
docker cp "$WORK/civi-emails.json" "$CRM_CONTAINER:/tmp/civi-emails.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py \
  --apply /tmp/civi-contacts.json --emails /tmp/civi-emails.json $COMMIT

# --------------------------------------------------------------------------- #
say "5/5  Re-linking the mails that were waiting"
if [ -n "$COMMIT" ]; then
  # A full sweep: the mails skipped earlier now match. `imported_mails` dedups,
  # so nothing is recorded twice.
  docker exec "$CRM_CONTAINER" python3 /app/utils/import_member_mails.py --backfill
else
  echo "   (dry-run : import_member_mails.py --backfill non lancé)"
fi

say "Terminé."
