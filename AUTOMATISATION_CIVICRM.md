# Automatisation : CiviCRM → CRM

PauseIA fait tourner un second CRM, **CiviCRM 6.15 Standalone**, sur le même
serveur, pour les envois de masse (newsletters, communiqués). Il contient
**~12 900 journalistes avec leur adresse e-mail et leur média** — des données que
ce CRM-ci n'a pas, et qui bloquaient le rapprochement automatique des courriels
avec la presse.

Ce document décrit le pont entre les deux. Il complète
`AUTOMATISATION_MAILS_MEMBRES.md` et `AUTOMATISATION_MAILS_CAMPAGNE.md`.

---

## Le principe : à la demande, pas en masse

On **ne recopie pas** les 12 900 journalistes. Deux raisons :

1. **L'interface casserait.** `new_meeting.html` et `new_mail.html` rendent un
   `<option>` *et* une case à cocher par personne. À 1 200 personnes ça passe ; à
   17 000, les écrans de saisie pèsent plusieurs mégaoctets et deviennent
   inutilisables.
2. **Ce CRM est un journal d'interactions**, pas un fichier d'envoi. Une fiche n'a
   de sens que pour quelqu'un avec qui on a réellement échangé.

Donc : **un journaliste obtient une fiche le jour où un membre lui écrit.** Les
adresses restent disponibles à 100 % côté CiviCRM sans être dupliquées ici — ce
qui est aussi beaucoup plus propre côté RGPD, les deux bases n'ayant pas la même
finalité.

**Exception : les médias.** 168 organisations, contre 165 déjà présentes ici.
Elles n'apparaissent dans aucun sélecteur de personnes, et une fiche journaliste
résolue a besoin de son média. Elles sont donc importées en une fois.

---

## Les trois règles qui protègent CiviCRM

| Règle | Pourquoi |
|---|---|
| **APIv4 uniquement, jamais MySQL** | Le schéma de CiviCRM change entre versions majeures ; son APIv4 est un contrat maintenu. Une lecture SQL directe casse silencieusement un jour. |
| **Le CLI `cv` en local, pas l'API REST** | Même serveur : aucune clé d'API à stocker, pas besoin d'activer `authx`, rien d'exposé sur Internet. |
| **Le périmètre presse par id de groupe** | `PRESS_GROUP_IDS` en tête de `civicrm-sync.sh` (12, 28, 69). Un groupe enfant hérite du filtre de son parent, donc les trois parents couvrent les ~40 groupes départementaux. |
| **Un seul sens, lecture seule** | CiviCRM porte les donateurs et les contributions Stripe / HelloAsso. Aucun script d'ici ne peut y écrire. Le pire qu'un bug puisse produire est « rien n'a été importé ». |

Les scripts de ce dépôt **ne parlent jamais à CiviCRM**. Ils lisent le JSON que
`cv` a écrit. Pas de réseau, pas d'identifiants, aucune dépendance aux entrailles
de CiviCRM.

---

## Le cycle complet

Les deux applications sont dans des conteneurs Docker séparés (`civicrm-web` et
`website-meeting-app`), et la base de ce CRM vit **dans** son conteneur. Aucun des
deux ne peut appeler l'autre — c'est voulu. L'hôte fait l'intermédiaire.

```
1. import_member_mails.py    rencontre une adresse qu'il ne sait pas rattacher
                             → civicrm_pending  (au lieu de l'ignorer)

2. civicrm_lookup.py --list-pending      → la liste des adresses

3. l'hôte : cv api4 Email.get             → quelle adresse appartient à qui
           puis cv api4 Contact.get       → les fiches (lecture seule)

4. civicrm_lookup.py --apply             → crée la fiche, son média, son alignement

5. import_member_mails.py --backfill     → le mail se rattache enfin
                                           (imported_mails évite tout doublon)
```

Tout est orchestré par **`utils/deploy/civicrm-sync.sh`**, qui tourne sur l'hôte :

```bash
sudo /opt/scripts/civicrm-sync.sh            # dry run, n'écrit rien
sudo /opt/scripts/civicrm-sync.sh --commit
```

### Planification

`civicrm-sync.service` + `civicrm-sync.timer`, à **06:30 UTC** — après
l'import des mails de membres de 06:10, qui est précisément ce qui remplit la
file que cette synchronisation vide. L'ordre compte.

```bash
sudo cp utils/deploy/civicrm-sync.sh /opt/scripts/ && sudo chmod +x /opt/scripts/civicrm-sync.sh
sudo cp utils/deploy/civicrm-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now civicrm-sync.timer
systemctl list-timers civicrm-sync.timer
journalctl -u civicrm-sync -n 40        # après le premier passage
```

Lancez d'abord `sudo /opt/scripts/civicrm-sync.sh` **sans `--commit`** : rien
n'est écrit et la sortie dit exactement ce qui serait créé.

### Pourquoi deux requêtes pour les adresses

CiviCRM porte **~1,6 adresse par journaliste** (21 401 adresses pour 12 987
contacts), et celle à laquelle un membre a écrit n'est souvent **pas la
primaire**. Filtrer `Contact.get` sur `email_primary.email` les manque en
silence — l'adresse est alors classée « inconnue de CiviCRM » alors qu'elle y
est. D'où `Email.get` d'abord, pour savoir à quel contact appartient chaque
adresse, puis `Contact.get` sur ces identifiants.

Quand la fiche existe déjà avec une autre adresse, celle qui a été rencontrée
est enregistrée dans **`person_emails`** — la table d'alias que l'import de
mails consulte déjà. Sans ça, le prochain mail venant de cette adresse
repartirait en file indéfiniment.

---

## Correspondance des données

| CiviCRM | Ici | Note |
|---|---|---|
| Sous-type `Journaliste` | `contact_type = 'Journaliste'` | |
| Sous-type organisation `M_dia` | `organisations.org_type = 'Média'` | |
| `employer_id` | `person_organisations` | le journaliste et son média |
| `Analyse_strat_gique_Pause_IA.Alignement` | `stance` | lu par **valeur** (`1`–`5`), jamais par libellé : ceux-ci contiennent des emoji et sont modifiables dans l'interface |
| `Analyse_strat_gique_Pause_IA.Niveau_d_influence` | `notes` | aucune colonne ici ne le porte |
| `Description_courte.Description_courte` | `notes` | |
| `Compte_R_seaux_Sociaux.Twitter` / `.LinkedIn` | `social_links` | |

**Alignement → stance** : `1` Neutre → *Neutre / indécis* · `2` Partiel → *Plutôt
favorable* · `3` Total → *Favorable* · `4` Opposé → *Opposé* · `5` Indéterminé et
absent → *Inconnu*. Ce CRM a un degré de plus (*Plutôt opposé*) que CiviCRM n'a
pas ; sans conséquence puisqu'on ne fait que lire.

### Pièges vérifiés sur les données réelles

- **Les sous-types remontent en noms techniques** : `M_dia`, `Expert_IA`,
  `Recherche_ducation`, `B_n_vole` — jamais les libellés de l'interface. Et le
  champ est **multivalué** : un contact peut porter
  `["B_n_vole","Expert_IA","Influenceur"]`.
- **Les médias sont en capitales** dans CiviCRM (`LE FIGARO`) contre une casse
  normale ici (`Le Figaro`). Le rapprochement se fait sur un nom replié (sans
  accents, sans casse, sans ponctuation), sinon le premier passage crée 165
  doublons.
- **`LE FIGARO` et `LE FIGARO ECONOMIE` restent deux organisations distinctes.**
  On garde ce que dit CiviCRM ; deviner une hiérarchie de rédactions par le nom
  finit toujours mal. Un modérateur peut fusionner à la main.
- **Un homonyme dans un autre type reste une fiche séparée** (« Laurent
  Alexandre » est à la fois député LFI et chroniqueur), comme dans tous les
  scripts `insert_*`.

### Ce qui n'est jamais écrasé

Une fiche existante n'est **jamais remplacée**, seulement complétée :

- une adresse, des réseaux sociaux ou des notes déjà renseignés sont conservés ;
- `stance` n'est repris que s'il vaut encore `Inconnu` — personne n'a choisi cette
  valeur-là ;
- `added_by` / `validated_by` restent `NULL`, la convention du dépôt pour « amené
  par un script ».

Concrètement, les **133 journalistes** hérités de l'ancien CRM presse (un nom, et
rien d'autre) se complètent tout seuls au lieu d'être dupliqués.

---

## La file d'attente

Table `civicrm_pending` :

| statut | sens |
|---|---|
| `pending` | vue dans un mail, en attente de recherche CiviCRM |
| `resolved` | une fiche existe |
| `absent` | CiviCRM ne la connaît pas non plus — gardée pour ne pas la rechercher chaque nuit |

```bash
python3 utils/civicrm_lookup.py --stats
python3 utils/civicrm_lookup.py --retry-absent --commit   # après avoir enrichi CiviCRM
```

Les **adresses génériques** (`redaction@`, `contact@`, `presse@`, `noreply@`…)
n'entrent jamais dans la file : elles appartiennent à une rédaction, pas à une
personne, et une fiche n'aurait aucun sens. La liste est dans
`GENERIC_LOCALPARTS` (`utils/civicrm_lookup.py`).

---

## Après une mise à jour de CiviCRM

Tout ce qui dépend de CiviCRM est dans **`utils/civicrm.py`**. C'est le seul
fichier qu'une montée de version peut casser, et il commence par un **test de
contrat** : si un champ personnalisé a été renommé ou supprimé, `assert_contract`
lève une erreur et **rien n'est écrit**.

```
ABANDON — contrat CiviCRM non respecté : contact export is missing 1 expected
field(s): Analyse_strat_gique_Pause_IA.Alignement. …
```

La marche à suivre est alors : comparer les `select` de
`utils/deploy/civicrm-sync.sh` avec `CONTACT_FIELDS` dans `utils/civicrm.py`, et
retrouver le nouveau nom technique avec

```bash
docker exec civicrm-web cv api4 CustomField.get '{"select":["name","label","custom_group_id:name"],"limit":50}' --cwd=/var/www/html
```

Avec lecture seule + arrêt sur contrat non satisfait, le pire qu'une mise à jour
puisse produire est **une synchronisation qui ne tourne plus**. Jamais de données
abîmées.

---

## Points ouverts

- **`Influenceur` et `Expert IA`** n'existent pas dans `CONTACT_TYPES` ici : ces
  contacts arrivent en `Autre`, avec leur sous-type d'origine conservé dans les
  notes. CiviCRM en compte **104 experts IA et 4 influenceurs** — les experts
  méritent probablement leur propre type, les influenceurs peuvent attendre.
- **Les rebonds fonctionnent.** Vérifié : **3 746 rebonds enregistrés**
  (`MailingEventBounce`) et **63 391 envois en file** (`MailingEventQueue`). Le
  mécanisme tourne, les adresses ont bien été sollicitées. Sur les 21 403
  adresses de journalistes, **75 sont suspendues** (73 en `on_hold = 1` après
  rebonds, 2 manuellement). Le taux est réellement bas, ce n'est pas un défaut de
  mesure — CiviCRM ne pose `on_hold` qu'au-delà d'un seuil de rebonds durs, donc
  une partie des 3 746 rebonds n'a pas encore atteint ce seuil.

  **On ne filtre volontairement pas dessus ici** : il s'agit d'identifier
  quelqu'un avec qui on a *déjà* échangé, pas de décider si on peut lui écrire.
  Une adresse morte désigne quand même une personne. Le sujet compte en revanche
  beaucoup pour les envois de CP.

### Suivre l'état des adresses

```bash
# Suspensions, sur TOUTES les adresses (pas seulement les primaires)
docker exec civicrm-web cv api4 Email.get '{"select":["on_hold","COUNT(id) AS total"],"where":[["contact_id.contact_sub_type","CONTAINS","Journaliste"]],"groupBy":["on_hold"]}' --cwd=/var/www/html

# Rebonds enregistrés, indépendamment du seuil qui déclenche on_hold
docker exec civicrm-web cv api4 MailingEventBounce.get '{"select":["COUNT(id) AS total"]}' --cwd=/var/www/html
```
- **Les relances en double.** CiviCRM porte aussi `Suivi_Activit_s_Pause_IA`
  (Résultat, Suivi Requis, Score Impact, Date Prochain Contact), qui recouvre le
  `/todo` d'ici. Il faudra décider qui fait référence pour « quand relancer qui »,
  sinon deux dates contradictoires coexisteront sur la même personne.
- **2 312 organisations non typées** dans les groupes presse de CiviCRM (contre
  168 portant le sous-type `M_dia`). Ce sont presque certainement des médias ; ça
  se corrige dans CiviCRM, pas ici.
