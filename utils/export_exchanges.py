#!/usr/bin/env python3
r"""Exporter les échanges enregistrés, en CSV ou en texte lisible.

Le CRM sait ce qui s'est échangé, mais il ne sait pas le rendre : pour relire
une série d'entretiens, préparer un bilan ou le passer à quelqu'un qui n'a pas
le mot de passe, il faut sortir les textes de la base. C'est ce que fait ce
script, sans rien y modifier — il n'ouvre la base qu'en lecture.

Les messages sont groupés par **fil** et triés dans l'ordre chronologique, donc
un aller-retour se relit comme une conversation et non comme une liste de
lignes éparses.

    # tous les échanges entre membres et élu·es, en CSV
    python3 utils/export_exchanges.py --type Politique --out echanges.csv

    # la même chose en texte, fil par fil, pour la lecture
    python3 utils/export_exchanges.py --type Politique --format txt \
        --out echanges.txt

    # la presse, sur une période
    python3 utils/export_exchanges.py --type Journaliste --depuis 2026-09-01 \
        --out presse.csv

Sans `--type`, tous les types de contact. Sans `--out`, la sortie va à l'écran.

**Ce que le fichier contient** : la correspondance réelle des membres, corps des
messages compris. Il sort du serveur avec les protections du serveur, donc il
mérite le même soin que la base elle-même — on ne le laisse pas traîner dans un
dossier partagé.
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# Le libellé affiché pour chaque sens, comme dans l'interface.
SENS = {"sent": "Envoyé", "received": "Reçu"}

REQUETE = """
    SELECT
        COALESCE(mt.thread_key, 'm' || x.id)              AS fil,
        x.id, x.mail_date, x.direction, x.subject,
        -- `summary` porte le corps du message pour les mails de membres ;
        -- `mail_bodies` le porte aussi quand il a pu être extrait. On prend le
        -- plus complet des deux plutôt que d'en privilégier un à l'aveugle.
        COALESCE(NULLIF(b.body, ''), x.summary, '')       AS corps,
        (SELECT GROUP_CONCAT(p.name, ' ; ')
           FROM mail_persons xp JOIN persons p ON p.id = xp.person_id
          WHERE xp.mail_id = x.id)                        AS interlocuteurs,
        (SELECT GROUP_CONCAT(DISTINCT p.contact_type)
           FROM mail_persons xp JOIN persons p ON p.id = xp.person_id
          WHERE xp.mail_id = x.id)                        AS types,
        (SELECT GROUP_CONCAT(DISTINCT o.name)
           FROM mail_persons xp
           JOIN person_organisations po ON po.person_id = xp.person_id
           JOIN organisations o ON o.id = po.organisation_id
          WHERE xp.mail_id = x.id)                        AS organisations,
        (SELECT GROUP_CONCAT(COALESCE(m.name, m.email), ' ; ')
           FROM mail_members mm JOIN members m ON m.id = mm.member_id
          WHERE mm.mail_id = x.id)                        AS membres
      FROM mails x
      LEFT JOIN mail_thread mt ON mt.mail_id = x.id
      LEFT JOIN mail_bodies b  ON b.mail_id = x.id
"""

COLONNES = ["fil", "date", "sens", "membre", "interlocuteur", "type",
            "organisation", "objet", "corps"]


def lire(db, contact_type=None, depuis=None, jusqu=None, membres_seuls=True):
    """Les messages à exporter, groupés par fil puis par date.

    `membres_seuls` écarte les mails de campagne citoyenne, qui n'ont pas de
    membre et dont le « corps » n'est qu'une ligne générée : les exporter
    gonflerait le fichier sans rien apprendre.
    """
    where, params = [], []
    if contact_type:
        where.append(
            "EXISTS (SELECT 1 FROM mail_persons xp JOIN persons p "
            "ON p.id = xp.person_id WHERE xp.mail_id = x.id "
            "AND p.contact_type = ?)")
        params.append(contact_type)
    if membres_seuls:
        where.append("EXISTS (SELECT 1 FROM mail_members mm "
                     "WHERE mm.mail_id = x.id)")
    if depuis:
        where.append("x.mail_date >= ?")
        params.append(depuis)
    if jusqu:
        where.append("x.mail_date <= ?")
        params.append(jusqu)

    sql = REQUETE + (" WHERE " + " AND ".join(where) if where else "")
    # Le tri porte d'abord sur la date du fil, pas sur celle du message : sinon
    # les fils s'entrelacent et la lecture suivie est perdue.
    sql += """
        ORDER BY (SELECT MIN(x2.mail_date) FROM mails x2
                   LEFT JOIN mail_thread mt2 ON mt2.mail_id = x2.id
                  WHERE COALESCE(mt2.thread_key, 'm' || x2.id) = fil),
                 fil, x.mail_date, x.id
    """
    return db.execute(sql, params).fetchall()


def en_lignes(rangs):
    for r in rangs:
        yield {
            "fil": r["fil"],
            "date": r["mail_date"],
            "sens": SENS.get(r["direction"], r["direction"] or ""),
            "membre": r["membres"] or "",
            "interlocuteur": r["interlocuteurs"] or "",
            "type": r["types"] or "",
            "organisation": r["organisations"] or "",
            "objet": r["subject"] or "",
            "corps": (r["corps"] or "").strip(),
        }


def ecrire_csv(lignes, sortie):
    # `utf-8-sig` : sans la marque d'ordre des octets, Excel lit un CSV UTF-8
    # en Latin-1 et affiche « Ã© » partout. Le fichier reste de l'UTF-8 valide
    # pour tout le reste.
    graveur = csv.DictWriter(sortie, fieldnames=COLONNES, delimiter=";",
                             quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    graveur.writeheader()
    total = 0
    for ligne in lignes:
        graveur.writerow(ligne)
        total += 1
    return total


def ecrire_txt(lignes, sortie):
    """Un fil après l'autre, séparés, lisibles d'un bout à l'autre."""
    fil_courant, total, fils = None, 0, 0
    for ligne in lignes:
        if ligne["fil"] != fil_courant:
            fil_courant = ligne["fil"]
            fils += 1
            entete = ligne["objet"] or "(sans objet)"
            qui = " ; ".join(x for x in (ligne["interlocuteur"],
                                         ligne["membre"]) if x)
            sortie.write("\n" + "=" * 78 + "\n")
            sortie.write(f"{entete}\n")
            if qui:
                sortie.write(f"{qui}\n")
            sortie.write("=" * 78 + "\n")
        # « Envoyé par » / « Reçu par » : le sens seul laisse le lecteur
        # deviner qui a écrit, ce qui est précisément ce qu'il cherche.
        qui = ligne["membre"] or "?"
        verbe = "Envoyé par" if ligne["sens"] == "Envoyé" else "Reçu par"
        sortie.write(f"\n--- {ligne['date']} · {verbe} {qui} ---\n")
        sortie.write(ligne["corps"] + "\n")
        total += 1
    return total, fils


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--type", dest="contact_type",
                        help="type de contact : Politique, Journaliste… "
                             "(par défaut : tous)")
    parser.add_argument("--depuis", metavar="AAAA-MM-JJ")
    parser.add_argument("--jusqu", metavar="AAAA-MM-JJ")
    parser.add_argument("--format", choices=("csv", "txt"), default="csv")
    parser.add_argument("--out", help="fichier de sortie (défaut : l'écran)")
    parser.add_argument("--tous", action="store_true",
                        help="inclure aussi les mails sans membre (campagne "
                             "citoyenne), dont le corps n'est pas conservé")
    args = parser.parse_args()

    # Lecture seule, explicitement : ce script ne doit jamais pouvoir écrire
    # dans la base de production, même par accident de frappe.
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rangs = lire(db, args.contact_type, args.depuis, args.jusqu,
                     membres_seuls=not args.tous)
    finally:
        db.close()

    lignes = list(en_lignes(rangs))
    if args.out:
        encodage = "utf-8-sig" if args.format == "csv" else "utf-8"
        with open(args.out, "w", encoding=encodage, newline="") as sortie:
            if args.format == "csv":
                total = ecrire_csv(lignes, sortie)
                fils = len({l["fil"] for l in lignes})
            else:
                total, fils = ecrire_txt(lignes, sortie)
        taille = os.path.getsize(args.out)
        print(f"{total} message(s) dans {fils} fil(s) → {args.out} "
              f"({taille / 1024:.0f} Kio)")
        if not total:
            print("Aucun message ne correspond. Vérifiez --type (les valeurs "
                  "sont celles des fiches : Politique, Journaliste, …) et les "
                  "dates.")
    else:
        if args.format == "csv":
            ecrire_csv(lignes, sys.stdout)
        else:
            ecrire_txt(lignes, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
