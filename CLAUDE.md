# CLAUDE.md — carte du projet pour un agent IA

CRM interne de **PauseIA** (« Journal des rencontres et des médias ») : une app
**Flask + SQLite** (un seul fichier `app.py`, templates Jinja, pas de build front),
déployée en **Docker** derrière Caddy sur un serveur Hetzner. Ce fichier est le
point d'entrée pour une IA qui reprend le projet : il dit **où sont les choses**,
**ce qui est déjà automatisé**, et **ce qui reste à faire**.

> Docs de détail : `README.md` (app), `BACKWARD_COMPATIBILITY.md` (schéma & fusion
> des CRM), `utils/README.md` (tous les scripts), `AUTOMATISATION_MAILS_MEMBRES.md`
> et `AUTOMATISATION_MAILS_CAMPAGNE.md` (les deux pipelines mails). Lis-les avant
> de modifier la zone correspondante.

## 1. Modèle de données (SQLite, `meetings.db`)

Tout tourne autour de **`persons`** (contacts) et **`organisations`**, reliés par
`person_organisations`. Migrations idempotentes dans `init_db()` (`app.py`) —
jamais de fichier de migration séparé, on ajoute des `ALTER TABLE … ADD COLUMN`
gardés par un test de présence de colonne.

- **`persons`** : un contact. Colonne clé **`contact_type`** ∈ `CONTACT_TYPES`
  (`Journaliste`, `Politique`, `Religieux·se`, `Membre d'une ONG`,
  `Membre d'une entreprise`, `Autre`). Champs notables : `role` (fonctions,
  cumulables), `political_group` (nullable depuis la fusion), `stance` (position
  sur PauseIA), `email`, `in_office` (0 = mandat terminé, fiche conservée), etc.
- **`organisations`** : `ORG_TYPES` = `Média`, `Groupe politique`, `Culte`, `ONG`,
  `Entreprise`, `Autre`. Correspondance type de contact ↔ type d'orga dans
  `ORG_TYPE_BY_CONTACT_TYPE` (`app.py`).
- **`meetings`** / `mails` (+ `mail_persons`, `mail_members`, `mail_bodies`,
  `mail_thread`), **`members`** (membres @pauseia.fr, auto-créés par l'import),
  **`interventions`** / `contents` (médiatiques), **`pending_*`** (file de
  modération), `moderators` (les « Utilisateurices »).
- **Provenance** : `added_by` / `validated_by` NULL = importé par script ;
  renseigné = saisi/validé à la main (les backfills ne touchent jamais ces
  dernières).

## 2. Interface

Nav (voir `templates/base.html`) : TODO · Fait · Répartition · Calendrier ·
**Suivi des échanges** · Rencontres · Personnes · Courriels · Organisations ·
Interventions · Contenus · Modération · Utilisateurices.

- **Suivi des échanges** (`/echanges`) : vue unifiée type boîte mail (citoyens +
  membres + élu·es), fils regroupés, sous-onglets *Échanges · Membres · Tous les
  courriels*. Détail d'un fil = `conversation.html` (cartes expéditeur→destinataires).
- Les listes filtrent par `contact_type` ; badge « Non élu·e actuellement » quand
  `in_office = 0`.

## 3. Ce qui est DÉJÀ automatisé

### a) Peuplement des contacts (scripts `utils/`, idempotents, `--commit`/`--dry-run`)
- **Élu·es** : `extract_deputes` + `insert_deputes`, `insert_senateurices`,
  `extract_gouvernement` + `insert_gouvernement`, `extract_eurodeputes` +
  `insert_eurodeputes`. Orchestrés par **`sync_officials.py`** (les 4 chambres) +
  réconciliation `in_office`.
- **Emails des élu·es** : `sync_emails_from_elus.py` (depuis `elus.json`).
- **Médias & journalistes** : `insert_medias_journalistes.py` (données embarquées).
- **Religieux·ses** : `insert_cultes.py` + `extract_eveques`/`insert_eveques`
  (+ `extract_eveques_orthodoxes`).

### b) Intégration des mails (récurrent, timers systemd)
- **Citoyens → élu·es** : `import_campaign_mails.py` (BCC campagne). Voir
  `AUTOMATISATION_MAILS_CAMPAGNE.md`. RGPD : identité citoyen jamais stockée.
- **Membres ↔ élu·es** : `import_member_mails.py` (règle Gmail invisible →
  boîte d'audit IMAP). Matching robuste (adresse/alias/corps/fil/motif de nom),
  membres auto-créés. Voir `AUTOMATISATION_MAILS_MEMBRES.md`.

### c) Planification (`utils/deploy/*.timer`, UTC)
- `sync-officials` (lun. 05:30, les 4 chambres + `in_office`) — remplace l'ancien
  `sync-eurodeputes` (désactivé).
- `import-campaign-mails` (06:00, sync emails + import citoyens).
- `import-member-mails` (06:10).

## 4. Ce qui n'est PAS (encore) automatisé — pistes

Le peuplement/intégration ci-dessus est **partiel**. Chantiers ouverts :

- **Organisations / groupes politiques** : les `persons` élu·es sont
  auto-synchronisées, mais les **organisations** (groupes politiques, médias,
  cultes) et les liens `person_organisations` ne sont **pas** rafraîchis par un
  timer — peuplés une fois par les `insert_*`. → automatiser leur mise à jour.
- **Types de contact sans source auto** : **ONG**, **Entreprises**, et la plupart
  des **cultes** hors diocèses n'ont pas d'extract/sync (pas de dataset public
  exploitable, cf. commentaires de `insert_cultes.py`). → identifier des sources.
- **Élus locaux** (maires, conseillers régionaux/départementaux, EPCI) : ~500 000,
  **absents**. Le RNE (data.gouv) existe mais **sans emails** → matching impossible
  par adresse. Piste retenue (non implémentée) : *capture assistée par modération*
  — les mails de membres vers une adresse externe inconnue vont dans une file
  « élu local à confirmer », un humain valide, l'adresse est apprise. La capture
  Gmail attrape **déjà** ces mails (règle sur `@pauseia.fr`), ils sont juste
  ignorés faute de fiche.
- **Interventions / Contenus** : entités présentes, mais aucune ingestion
  automatique (ex. veille médias). → à concevoir.
- **Concurrence `init_db`** : les migrations tournent dans **chaque worker
  gunicorn** au démarrage ; une migration non atomique (ex. `RENAME COLUMN`) peut
  planter en course entre workers. Toute migration lourde doit être idempotente
  **et** sûre en concurrence (verrou / `IF NOT EXISTS`), ou lancée une seule fois.

## 5. Conventions

- **Scripts `utils/`** : sans dépendance tierce (stdlib), **idempotents**,
  `--dry-run` par défaut ou explicite puis `--commit`, DB = `ROOT/meetings.db`
  (= `/app/meetings.db` en conteneur). Matching par **nom au sein d'un même type**
  (un homonyme dans un autre type reste une fiche séparée).
- **Ne jamais supprimer** un contact qui quitte ses fonctions : `in_office = 0`.
- **Secrets** uniquement en variables d'env (`/opt/volunteer-apps/secrets/…`),
  jamais dans le dépôt. IMAP : `IMAP_*` (citoyens), `MEMBER_IMAP_*` (membres).
- **Tests** : `python3 -m unittest discover -s tests` (couvre le classifieur mails).

## 6. Déploiement (Docker)

La base **vit dans le conteneur** (`/app/meetings.db`) — ne pas la manipuler
depuis l'hôte. Le `Dockerfile` ne copie que `app.py` + `templates/` + `static/` ;
`utils/` et `actual_dataset/` sont injectés à l'exécution par `docker cp`.

```bash
cd /opt/volunteer-apps/apps/website-meeting
sudo docker exec website-meeting-app sh -c 'cp /app/meetings.db /app/meetings.db.bak-$(date +%F)'
git fetch <remote> && git checkout -f <remote>/main    # la prod suit main
sudo docker-compose build && sudo docker-compose up -d  # init_db applique les migrations
sudo docker logs --tail 15 website-meeting-app          # vérifier: pas de traceback
# peuplement éventuel :
sudo docker exec website-meeting-app rm -rf /app/utils
sudo docker cp utils website-meeting-app:/app/utils
sudo docker cp actual_dataset website-meeting-app:/app/actual_dataset   # si le script lit un JSON
sudo docker exec website-meeting-app python3 /app/utils/<script>.py [--commit]
```

## 7. Où regarder en premier

| Besoin | Fichier |
|---|---|
| Routes, schéma, migrations, constantes (`CONTACT_TYPES`…) | `app.py` |
| Un script de peuplement / sync | `utils/` + `utils/README.md` |
| Comment le schéma a évolué (fusion CRM) | `BACKWARD_COMPATIBILITY.md` |
| Les pipelines mails | `AUTOMATISATION_MAILS_*.md` |
| Timers / unités systemd | `utils/deploy/` |
