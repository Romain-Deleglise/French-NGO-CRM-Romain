#!/usr/bin/env bash
# Sauvegarder la base, depuis l'hôte, et la sortir du conteneur.
#
# C'est le seul filet de l'outil : la base vit dans le conteneur
# (/app/meetings.db) et rien d'autre ne l'écrit ailleurs. Une sauvegarde qui
# resterait dans ce même conteneur ne protégerait de rien — ni d'un
# `docker-compose down -v`, ni d'un volume perdu, ni d'une image reconstruite.
# Elle est donc copiée sur l'hôte, puis, si RSYNC_DEST est renseigné, poussée
# hors de la machine. Sans copie distante, un incident serveur emporte tout.
#
#   sudo /opt/scripts/backup-db.sh
#   sudo RSYNC_DEST=sauvegardes@ailleurs:/srv/pauseia /opt/scripts/backup-db.sh
#
set -euo pipefail

CRM_CONTAINER="${CRM_CONTAINER:-website-meeting-app}"
DEST="${BACKUP_DIR:-/opt/volunteer-apps/backups/website-meeting}"
# Un mois d'historique sur l'hôte : assez pour rattraper une corruption
# découverte tardivement, assez peu pour ne pas remplir le disque.
KEEP="${BACKUP_KEEP:-30}"
# Facultatif : `user@hote:/chemin` ou un chemin local sur un autre disque.
RSYNC_DEST="${RSYNC_DEST:-}"

mkdir -p "$DEST"

say() { printf '\n== %s\n' "$*"; }

say "1/3  Sauvegarde dans le conteneur ($CRM_CONTAINER)"
# --keep 2 côté conteneur : juste de quoi ne pas laisser le disque du conteneur
# se remplir. L'historique, lui, vit sur l'hôte.
docker exec "$CRM_CONTAINER" python3 /app/utils/backup_db.py \
  --dir /tmp/sauvegardes --keep 2

FICHIER=$(docker exec "$CRM_CONTAINER" sh -c \
  'ls -1t /tmp/sauvegardes/*.bak* | head -1')
if [ -z "$FICHIER" ]; then
  echo "ABANDON : aucune sauvegarde produite." >&2
  exit 1
fi

say "2/3  Copie sur l'hôte"
docker cp "$CRM_CONTAINER:$FICHIER" "$DEST/"
LOCAL="$DEST/$(basename "$FICHIER")"
echo "   $LOCAL ($(du -h "$LOCAL" | cut -f1))"

# Rotation côté hôte : les KEEP plus récentes, les autres s'en vont.
# shellcheck disable=SC2012
ls -1t "$DEST"/*.bak* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r vieux; do
  echo "   supprimée : $(basename "$vieux")"
  rm -f "$vieux"
done

say "3/3  Copie hors de la machine"
if [ -n "$RSYNC_DEST" ]; then
  rsync -a "$LOCAL" "$RSYNC_DEST/"
  echo "   envoyée vers $RSYNC_DEST"
else
  # Dit à chaque passage, volontairement : une sauvegarde qui ne quitte pas le
  # serveur ne protège pas de la perte du serveur, et c'est le genre de détail
  # qu'on croit avoir réglé.
  echo "   AUCUNE copie distante (RSYNC_DEST non renseigné)."
  echo "   Une sauvegarde restée sur ce serveur ne protège pas de sa perte."
fi

say "Terminé."
