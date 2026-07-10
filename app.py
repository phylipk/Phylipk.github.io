"""
app.py — Serveur Flask pour La Cochette Dorée
Lance un serveur local sur http://127.0.0.1:5050 et ouvre le navigateur.
"""
import sqlite3, json, os, threading, webbrowser
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory

BASE  = Path(__file__).parent
DB    = BASE / 'cochette.db'
INIT  = BASE / 'init_db.py'
FRONT = BASE / 'templates' / 'index.html'

import re as _re

def _parse_rang(val):
    """Convertit numero_portee en entier (gère textes comme 'TRUIE 01' et flottants)."""
    if val is None:
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        m = _re.search(r'\d+', str(val))
        return int(m.group()) if m else 0


app = Flask(__name__, static_folder=str(BASE / 'static'))


# Certaines colonnes de la base sont binaires (ex. ventes.sivac_data, un BLOB).
# jsonify ne sait pas sérialiser des octets → erreur 500. On rend la sérialisation
# JSON tolérante aux octets (encodage base64) pour toute l'API, une seule fois.
import base64 as _base64
from flask.json.provider import DefaultJSONProvider as _DefaultJSONProvider


class _BytesSafeJSONProvider(_DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, (bytes, bytearray)):
            return _base64.b64encode(o).decode('ascii')
        return _DefaultJSONProvider.default(o)


app.json = _BytesSafeJSONProvider(app)


# ── Utilitaires DB ──────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn



# ── Injection données salaires 2026 depuis AppliTruieSALAIREv1.xlsx ────────

def _seed_acheteurs_clients(db):
    """Importe acheteurs et clients depuis les historiques existants."""
    # ── Acheteurs depuis la table ventes ─────────────────────────────────────
    try:
        ventes = db.execute("""
            SELECT DISTINCT acheteur_nom, type_acheteur, acheteur_tel, acheteur_adresse
            FROM ventes
            WHERE acheteur_nom IS NOT NULL AND acheteur_nom != ''
        """).fetchall()
        for v in ventes:
            nom = (v['acheteur_nom'] or '').strip()
            if not nom: continue
            exists = db.execute("SELECT id FROM acheteurs WHERE nom=?", (nom,)).fetchone()
            if not exists:
                db.execute(
                    "INSERT INTO acheteurs (nom,type_acheteur,telephone,adresse) VALUES (?,?,?,?)",
                    (nom, v['type_acheteur'] or 'Acheteur local',
                     v['acheteur_tel'] or '', v['acheteur_adresse'] or '')
                )
        # Aussi importer depuis client_fournisseur si le champ existe
        cols_v = [c[1] for c in db.execute("PRAGMA table_info(ventes)").fetchall()]
        if 'client_fournisseur' in cols_v:
            rows = db.execute("SELECT DISTINCT client_fournisseur, categorie FROM ventes WHERE client_fournisseur IS NOT NULL AND client_fournisseur != ''").fetchall()
            for r in rows:
                nom = (r['client_fournisseur'] or '').strip()
                if not nom: continue
                exists = db.execute("SELECT id FROM acheteurs WHERE nom=?", (nom,)).fetchone()
                if not exists:
                    db.execute("INSERT INTO acheteurs (nom,type_acheteur) VALUES (?,?)",
                               (nom, r['categorie'] or 'Acheteur local'))
        db.commit()
        print("  ✓ Acheteurs importés depuis ventes")
    except Exception as e:
        print(f"  ⚠ Import acheteurs: {e}")

    # ── Clients depuis la table commandes ─────────────────────────────────────
    try:
        cols_c = [c[1] for c in db.execute("PRAGMA table_info(commandes)").fetchall()]
        tel_col = 'telephone' if 'telephone' in cols_c else None
        adr_col = 'adresse'   if 'adresse'   in cols_c else None
        q = f"SELECT DISTINCT client{', '+tel_col if tel_col else ''}{', '+adr_col if adr_col else ''} FROM commandes WHERE client IS NOT NULL AND client != ''"
        rows = db.execute(q).fetchall()
        for r in rows:
            nom = (r['client'] or '').strip()
            if not nom: continue
            exists = db.execute("SELECT id FROM clients_commande WHERE nom=?", (nom,)).fetchone()
            if not exists:
                db.execute("INSERT INTO clients_commande (nom,telephone,adresse) VALUES (?,?,?)",
                           (nom, r[tel_col] if tel_col else '', r[adr_col] if adr_col else ''))
        db.commit()
        print("  ✓ Clients importés depuis commandes")
    except Exception as e:
        print(f"  ⚠ Import clients: {e}")

    # ── Clients depuis ventes_individuelles ───────────────────────────────────
    try:
        rows = db.execute("SELECT DISTINCT client, telephone, adresse FROM ventes_individuelles WHERE client IS NOT NULL AND client != ''").fetchall()
        for r in rows:
            nom = (r['client'] or '').strip()
            if not nom: continue
            exists = db.execute("SELECT id FROM clients_commande WHERE nom=?", (nom,)).fetchone()
            if not exists:
                db.execute("INSERT INTO clients_commande (nom,telephone,adresse) VALUES (?,?,?)",
                           (nom, r['telephone'] or '', r['adresse'] or ''))
        db.commit()
        print("  ✓ Clients importés depuis ventes_individuelles")
    except Exception as e:
        print(f"  ⚠ Import clients VI: {e}")

def _seed_salaires_2026(db):
    """Insère les lignes 2026 manquantes (idempotent : vérifie chaque ligne individuellement)."""
    # S'assurer que la colonne annee existe dans salaires
    sal_cols = [c[1] for c in db.execute('PRAGMA table_info(salaires)').fetchall()]
    for col, defn in [('annee','INTEGER'), ('salaire','INTEGER DEFAULT 0'),
                      ('frais_subsistance','INTEGER DEFAULT 0'),
                      ('total','INTEGER DEFAULT 0'), ('statut','TEXT DEFAULT "En attente"'),
                      ('observations','TEXT DEFAULT ""')]:
        if col not in sal_cols:
            db.execute(f'ALTER TABLE salaires ADD COLUMN {col} {defn}')
            db.commit()
            print(f'  ✓ Migration salaires: colonne {col} ajoutée')

    # Structure: (mois, employe, poste, salaire, subsistance, statut)
    rows = [
        (1,'MARC','Porcher',60000,40000,'Payé'),
        (1,'NARCISSE','Ouvrier',50000,10000,'Payé'),
        (1,'NOEL','Aide ouvrier',0,40000,'Payé'),
        (2,'MARC','Porcher',60000,30000,'Payé'),
        (2,'NARCISSE','Ouvrier',50000,10000,'Payé'),
        (2,'NOEL','Aide ouvrier',0,40000,'Payé'),
        (3,'MARC','Porcher',60000,40000,'Payé'),
        (3,'NARCISSE','Ouvrier',50000,10000,'Payé'),
        (3,'NOEL','Aide ouvrier',0,40000,'Payé'),
        (4,'MARC','Porcher',60000,30000,'En attente'),
        (4,'NARCISSE','Ouvrier',50000,10000,'En attente'),
        (4,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (5,'MARC','Porcher',60000,40000,'En attente'),
        (5,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (5,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (6,'MARC','Porcher',60000,30000,'En attente'),
        (6,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (6,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (7,'MARC','Porcher',60000,40000,'En attente'),
        (7,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (7,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (8,'MARC','Porcher',60000,30000,'En attente'),
        (8,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (8,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (9,'MARC','Porcher',60000,40000,'En attente'),
        (9,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (9,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (10,'MARC','Porcher',60000,30000,'En attente'),
        (10,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (10,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (11,'MARC','Porcher',60000,40000,'En attente'),
        (11,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (11,'NOEL','Aide ouvrier',0,40000,'En attente'),
        (12,'MARC','Porcher',60000,40000,'En attente'),
        (12,'COUSIN DE MARC','Ouvrier',50000,10000,'En attente'),
        (12,'NOEL','Aide ouvrier',0,40000,'En attente'),
    ]
    # Ne seeder qu'UNE SEULE FOIS : si déjà fait (flag) ou si des données 2026 existent déjà,
    # ne jamais ré-insérer (sinon les lignes supprimées par l'utilisateur réapparaîtraient).
    flag = db.execute("SELECT valeur FROM parametres WHERE cle='seed_salaires_2026'").fetchone()
    has_2026 = db.execute("SELECT COUNT(*) c FROM salaires WHERE annee=2026").fetchone()[0]
    if flag or has_2026 > 0:
        db.execute("INSERT OR IGNORE INTO parametres (cle,valeur) VALUES ('seed_salaires_2026','done')")
        db.commit()
        return

    inserted = 0
    for mois, emp, poste, sal, sub, statut in rows:
        db.execute(
            'INSERT INTO salaires (employe,poste,annee,mois,salaire,frais_subsistance,total,statut,observations) VALUES (?,?,?,?,?,?,?,?,?)',
            (emp, poste, 2026, mois, sal, sub, sal+sub, statut, '')
        )
        inserted += 1
    db.execute("INSERT OR IGNORE INTO parametres (cle,valeur) VALUES ('seed_salaires_2026','done')")
    db.commit()
    if inserted:
        print(f'  ✓ Salaires 2026 : {inserted} ligne(s) initialisée(s) (une seule fois)')

def migrate_db():
    """Migration automatique : colonnes manquantes + données Excel dans cycles."""
    db = get_db()

    # 1. Ajouter colonne observations si absente
    cols = [r[1] for r in db.execute('PRAGMA table_info(cycles)').fetchall()]
    if 'observations' not in cols:
        db.execute('ALTER TABLE cycles ADD COLUMN observations TEXT DEFAULT ""')
        db.commit()
        print("  ✓ Migration: colonne observations ajoutée à cycles")

    # 2. Injecter les données Excel (idempotent : UPDATE uniquement si la ligne existe)
    # Format: (truie_num, rang, saillie_1, saillie_2, saillie_3, date_mb,
    #          nais_vivants, morts_nes, adoption, sexe_m, sexe_f,
    #          mort_pre_m, mort_pre_f, date_sevrage, sevres, obs, code, verrat)
    EXCEL_DATA = [
    ('01',1,'2022-06-01',None,None,'2022-09-23',0,0,0,0,0,0,0,'2022-10-23',0,'Aucune gestation ni retour\nSaillie non Fécondante','DONE','SPOT'),
    ('01',2,'2022-11-18',None,None,'2023-03-13',12,0,0,6,6,0,0,'2023-04-14',12,'','DONE','SPOT'),
    ('01',3,'2023-04-22',None,None,'2023-08-16',18,0,0,4,8,3,3,'2023-09-30',12,'mort par etouffement et froid','DONE','SPOT'),
    ('01',4,'2023-10-09',None,None,'2024-02-01',11,0,0,6,5,0,0,'2024-03-15',11,'','DONE','SPOT'),
    ('01',5,'2024-03-23',None,None,'2024-07-15',14,0,0,10,4,0,0,'2024-08-28',14,'','DONE','SPOT'),
    ('01',6,'2024-09-29',None,None,'2025-01-09',5,0,0,3,2,0,0,'2025-02-12',5,'','DONE','TANOS'),
    ('01',7,'2025-03-14',None,None,'2025-07-06',8,1,0,3,5,0,0,None,8,'','DONE','SPOT'),
    ('01',8,'2025-08-31',None,None,'2025-12-24',12,0,0,0,0,0,0,'2026-01-23',0,'','MB','SPOT'),
    ('01',9,'2026-02-10',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('01',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('02',1,'2022-06-23',None,None,'2022-10-15',1,2,-1,0,0,0,0,'2022-11-14',0,'Mise en adoption à No.3','DONE','SPOT'),
    ('02',2,'2022-10-28',None,None,'2023-02-18',9,0,0,2,3,1,3,'2023-03-24',5,'','DONE','SPOT'),
    ('02',3,'2023-03-31',None,None,'2023-07-23',9,0,0,4,5,0,0,'2023-08-25',9,'','DONE','SPOT'),
    ('02',4,'2023-09-01',None,None,'2023-12-24',2,7,-2,1,1,0,0,'2024-02-07',0,'Mise en adoption à No.8 le 27 Dec 2023','DONE','SPOT'),
    ('02',5,'2024-01-20',None,None,'2024-05-14',12,0,0,4,8,0,0,'2024-07-16',12,'','DONE','SPOT'),
    ('02',6,'2024-07-18',None,None,None,0,0,0,0,0,0,0,None,0,'DOS CASSE A LA SAILLIE - REFORMER','HIDE','SPOT'),
    ('02',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('02',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('02',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('02',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('03',1,'2022-06-24',None,None,'2022-10-18',12,0,1,12,1,0,0,'2022-11-18',13,'Adoption du porcelet cochette No 2','DONE','SPOT'),
    ('03',2,'2022-12-13',None,None,'2023-04-04',15,0,0,7,6,2,0,'2023-05-07',13,'','DONE','SPOT'),
    ('03',3,'2023-05-14',None,None,'2023-09-07',11,0,0,7,1,1,2,'2023-10-23',8,'Par écrasement le 15/09/23','DONE','SPOT'),
    ('03',4,'2023-10-31',None,None,'2024-02-21',13,0,1,7,7,0,0,'2024-04-05',14,'Adoption de 1 sur 2  porcelets restant de la cochette No 5 morte','DONE','SPOT'),
    ('03',5,'2024-04-14','2024-05-26',None,'2024-09-18',7,0,0,2,5,0,0,'2024-11-12',7,'PAS DE RETOUR','DONE','TANOS'),
    ('03',6,'2025-02-27',None,None,'2025-06-22',11,1,0,7,4,0,0,'2025-08-06',11,'','DONE','TANOS'),
    ('03',7,'2025-08-31',None,None,'2025-12-24',9,1,0,0,0,0,0,'2026-02-07',0,'','MB','TANOS'),
    ('03',8,'2026-02-10',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('03',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('03',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('04',1,'2022-07-11',None,None,'2022-11-03',5,1,0,5,0,0,0,'2022-12-02',5,'','DONE','SPOT'),
    ('04',2,'2022-12-14',None,None,'2023-04-09',10,1,0,6,4,0,0,'2023-05-07',10,'','DONE','SPOT'),
    ('04',3,'2023-05-13',None,None,'2023-09-05',10,0,0,5,5,0,0,'2023-10-23',10,'','DONE','SPOT'),
    ('04',4,'2023-10-29',None,None,'2024-02-22',13,0,1,6,8,0,0,'2024-04-05',14,'Adoption de 1 sur 2  porcelets restant de la cochette No 5 morte','DONE','SPOT'),
    ('04',5,'2024-04-17',None,None,'2024-08-10',11,0,0,7,4,0,0,'2024-09-30',11,'','DONE','SPOT'),
    ('04',6,'2024-10-05',None,None,'2025-01-30',12,0,0,5,4,2,1,'2025-03-16',9,'','DONE','TANOS'),
    ('04',7,'2025-04-01',None,None,'2025-07-24',10,1,0,4,6,0,0,'2025-09-12',10,'','DONE','SPOT'),
    ('04',8,'2025-09-15',None,None,'2026-01-09',10,0,0,0,0,0,0,'2026-02-08',0,'','MB','SPOT'),
    ('04',9,'2026-02-26',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('04',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',1,'2022-07-17',None,None,'2022-11-10',5,0,0,0,5,0,0,'2022-12-02',5,'','DONE','SPOT'),
    ('05',2,'2022-12-15',None,None,'2023-04-06',7,0,0,3,4,0,0,'2023-05-07',7,'','DONE','SPOT'),
    ('05',3,'2023-05-12',None,None,'2023-09-04',16,0,0,9,5,2,0,'2023-10-23',14,'','DONE','SPOT'),
    ('05',4,'2023-10-30',None,None,'2024-02-17',12,1,0,1,1,5,5,'2024-04-02',2,'Truie Morte 3 jours plus tard','DONE','SPOT'),
    ('05',5,'2024-04-05',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('05',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',1,'2022-08-09',None,None,'2022-11-30',12,1,0,5,7,0,0,'2022-12-29',12,'','DONE','SPOT'),
    ('06',2,'2023-01-17',None,None,'2023-05-08',13,0,0,9,2,1,1,'2023-06-07',11,'','DONE','SPOT'),
    ('06',3,'2023-06-16',None,None,'2023-10-08',11,0,0,0,0,7,4,'2023-11-22',0,'Mort au servrage','DONE','SPOT'),
    ('06',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('06',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('07',1,'2022-08-10',None,None,'2022-12-12',0,10,0,0,0,0,0,'2023-01-10',0,'','DONE','SPOT'),
    ('07',2,'2022-12-20',None,None,'2023-04-16',2,0,0,1,1,0,0,'2023-05-07',2,'Reformer le 15/05/2023\nPoid: 111 KG\nPV: 1900F/KG - 211.000F','DONE','SPOT'),
    ('07',3,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('07',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE',''),
    ('08',1,'2022-09-01',None,None,'2022-12-23',12,0,0,8,4,0,0,'2023-02-15',12,'','DONE','SPOT'),
    ('08',2,'2023-02-22','2023-04-01',None,'2023-07-22',18,0,0,11,7,0,0,'2023-08-25',18,'Retour sur la 1ere saillie','DONE','SPOT'),
    ('08',3,'2023-09-04',None,None,'2023-12-26',10,0,2,4,6,0,0,'2024-02-13',12,'Adoption des porcelets cochette No 2','DONE','SPOT'),
    ('08',4,'2024-02-24','2024-05-27',None,'2024-09-18',7,0,0,3,4,0,0,'2024-11-12',7,'Retour sur la 1ere saillie','DONE','TANOS'),
    ('08',5,'2025-01-31',None,None,'2025-05-24',7,0,-4,1,3,2,1,'2025-07-08',0,'A manger 3 porcelets post mise bas','DONE','TANOS'),
    ('08',6,'2025-06-03','2025-07-22',None,'2025-11-16',7,1,0,4,3,0,0,'2025-12-31',7,'Retour sur la 1ere saillie','DONE','TANOS'),
    ('08',7,'2026-01-16',None,None,'2026-05-10',0,0,0,0,0,0,0,None,0,'','UP','SPOT'),
    ('08',8,'2026-06-27',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('08',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('08',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('09',1,'2022-09-04',None,None,'2022-12-28',10,0,0,5,4,0,1,'2023-02-07',9,'03/01/20223 - 1 mort (reste 9)','DONE','SPOT'),
    ('09',2,'2023-02-21',None,None,'2023-06-15',12,0,0,4,8,0,0,'2023-07-17',12,'','DONE','SPOT'),
    ('09',3,'2023-07-24',None,None,'2023-11-16',14,0,0,7,6,0,1,'2023-12-24',13,'','DONE','SPOT'),
    ('09',4,'2023-12-28',None,None,'2024-04-22',12,0,0,8,4,0,0,'2024-06-06',12,'','DONE','SPOT'),
    ('09',5,'2024-07-24',None,None,'2024-11-17',11,0,0,3,7,0,1,'2025-01-07',10,'PAS DE RETOUR','DONE','TANOS'),
    ('09',6,'2025-03-02',None,None,'2025-06-25',13,0,0,4,8,1,0,'2025-08-09',12,'','DONE','SPOT'),
    ('09',7,'2025-09-23',None,None,'2026-02-08',11,1,0,0,0,0,0,'2026-03-25',0,'','MB','SPOT'),
    ('09',8,'2026-03-28',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('09',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('09',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('10',1,'2022-09-13',None,None,'2023-01-07',11,0,2,5,6,0,0,'2023-02-22',11,'Adoption de 2 porcelets de la No 11 donc total 13 porcelets','DONE','SPOT'),
    ('10',2,'2023-03-14',None,None,'2023-07-05',13,0,0,6,2,3,2,'2023-08-05',8,'3 morts par écrasement','DONE','SPOT'),
    ('10',3,'2023-08-12',None,None,'2023-12-08',13,0,0,5,8,0,0,'2024-01-19',13,'','DONE','SPOT'),
    ('10',4,'2024-01-28',None,None,'2024-05-22',14,0,0,4,10,0,0,'2024-07-16',14,'','DONE','SPOT'),
    ('10',5,'2024-07-24','2024-12-07',None,'2025-04-03',11,1,0,7,3,0,1,'2025-05-31',10,'','DONE','SPOT'),
    ('10',6,'2025-06-18',None,None,'2025-10-13',10,0,0,7,3,0,0,'2025-11-15',10,'','SVG','SPOT'),
    ('10',7,'2025-11-18',None,None,'2026-03-12',0,0,0,0,0,0,0,None,0,'','UP','SPOT'),
    ('10',8,'2026-04-29',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('10',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('10',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('11',1,'2022-09-16',None,None,'2023-01-08',16,0,-2,7,7,1,1,'2023-02-22',14,'Mise en adoption de 2 porcelets à la No 10 donc total 14 porcelets','DONE','SPOT'),
    ('11',2,'2023-02-22',None,None,'2023-06-16',15,0,0,9,5,1,0,'2023-07-17',14,'','DONE','SPOT'),
    ('11',3,'2023-07-24','2023-07-25',None,'2023-11-16',15,2,0,5,8,1,1,'2024-01-16',13,'3 nés non viables','DONE','SPOT'),
    ('11',4,'2024-01-17',None,None,'2024-05-13',16,0,0,7,9,0,0,'2024-07-16',16,'','DONE','TANOS'),
    ('11',5,'2024-10-04',None,None,'2025-01-29',11,1,0,5,6,0,0,'2025-03-16',11,'','DONE','TANOS'),
    ('11',6,'2025-06-04',None,None,'2025-09-28',5,1,0,2,3,0,0,'2025-11-09',5,'','DONE','TANOS'),
    ('11',7,'2025-12-15',None,None,'2026-04-10',14,0,0,0,0,0,0,'2026-05-25',0,'','MB','TANOS'),
    ('11',8,'2026-05-28',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('11',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('11',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('12',1,'2022-09-21',None,None,'2023-01-12',12,0,0,9,3,0,0,'2023-02-22',12,'','DONE','SPOT'),
    ('12',2,'2023-03-08','2023-03-09',None,'2023-06-29',10,1,0,5,2,2,1,'2023-08-05',7,'','DONE','SPOT'),
    ('12',3,'2023-08-13',None,None,'2023-12-04',16,0,0,5,10,1,0,'2024-01-19',15,'','DONE','SPOT'),
    ('12',4,'2024-01-26',None,None,'2024-05-19',11,0,0,2,9,0,0,'2024-07-16',11,'','DONE','SPOT'),
    ('12',5,'2024-07-23',None,None,'2024-11-12',7,1,0,3,4,0,0,'2025-01-07',7,'','DONE','SPOT'),
    ('12',6,'2025-03-02',None,None,'2025-06-23',13,0,0,7,5,0,1,'2025-08-09',12,'','DONE','SPOT'),
    ('12',7,'2025-09-02',None,None,'2025-12-24',12,1,0,6,6,0,0,'2026-02-07',12,'Abattu le 22 Fev 2026 pour caude de côtes cassé après rassemblement','DONE','SPOT'),
    ('12',8,'2026-02-10',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('12',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('12',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','SPOT'),
    ('13',1,'2023-06-26',None,None,'2023-10-18',14,0,0,7,5,1,1,'2023-12-01',12,'Mort par noyade','DONE','FRONT'),
    ('13',2,'2023-12-14',None,None,'2024-04-07',11,0,0,5,5,1,0,'2024-05-29',10,'','DONE','TANOS'),
    ('13',3,'2024-06-04',None,None,'2024-09-26',11,0,0,5,6,0,0,'2024-12-02',11,'','DONE','TANOS'),
    ('13',4,'2025-01-22',None,None,'2025-05-18',11,0,4,6,9,0,0,'2025-07-02',15,'Adoption des 4 porcelets restant de la truie 08','DONE','TANOS'),
    ('13',5,'2025-08-10','2023-11-19',None,'2024-03-12',0,0,0,0,0,0,0,None,0,'Retour','UP','SPOT'),
    ('13',6,'2024-04-29',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('13',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('13',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('13',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('13',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',1,'2023-07-08','2023-07-09',None,'2023-10-30',11,0,0,5,5,0,1,'2023-12-01',10,'','DONE','TANOS'),
    ('14',2,'2023-12-15','2024-05-27',None,'2024-09-19',9,0,0,4,5,0,0,'2024-11-12',9,'Retour sur la 1ere saillie','DONE','SPOT'),
    ('14',3,'2024-12-07','2025-04-15',None,'2025-08-10',7,1,0,3,4,0,0,'2025-09-22',7,'RETOUR','DONE','TANOS'),
    ('14',4,'2025-10-28',None,None,'2026-02-17',11,1,0,0,0,0,0,'2026-04-03',0,'','MB','TANOS'),
    ('14',5,'2026-04-06',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('14',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('15',1,'2023-07-11',None,None,'2023-11-04',12,0,0,6,5,0,1,'2023-12-24',11,'','DONE','TANOS'),
    ('15',2,'2023-12-29','2024-05-07',None,'2024-08-24',10,1,0,5,5,0,0,'2024-10-14',10,'Retour sur la 1ere saillie','DONE','SPOT'),
    ('15',3,'2024-10-20',None,None,'2025-02-09',8,0,0,5,3,0,0,'2025-03-26',8,'','DONE','SPOT'),
    ('15',4,'2025-05-12',None,None,'2025-09-03',10,1,0,7,3,0,0,'2025-10-15',10,'','DONE','SPOT'),
    ('15',5,'2026-01-12',None,None,'2026-05-06',0,0,0,0,0,0,0,None,0,'','UP','TANOS'),
    ('15',6,'2026-06-23',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('15',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('15',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('15',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('15',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('16',1,'2023-08-09',None,None,'2023-12-02',7,0,0,4,3,0,0,'2024-01-16',7,'','DONE','TANOS'),
    ('16',2,'2024-01-26','2024-05-07',None,'2024-08-31',12,0,0,5,7,0,0,'2024-10-14',12,'Retour sur la 1ere saillie','DONE','TANOS'),
    ('16',3,'2024-10-20',None,None,'2025-02-11',11,1,0,9,2,0,0,'2025-03-25',11,'','DONE','TANOS'),
    ('16',4,'2025-05-16',None,None,'2025-09-10',9,0,0,4,5,0,0,'2025-10-15',9,'','DONE','TANOS'),
    ('16',5,'2025-11-19',None,None,'2026-03-13',0,0,0,0,0,0,0,None,0,'','UP','TANOS'),
    ('16',6,'2026-04-30',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('16',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('16',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('16',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('16',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',1,'2023-08-24',None,None,'2023-12-17',9,0,0,5,4,0,0,'2024-01-19',9,'','DONE','TANOS'),
    ('17',2,'2024-01-28','2024-06-01',None,'2024-09-24',10,0,0,3,7,0,0,'2024-11-12',10,'Retour sur la 1ere saillie','DONE','TANOS'),
    ('17',3,'2024-12-12','2024-05-13',None,'2024-09-06',7,0,0,4,3,0,0,'2024-10-15',7,'Retour sur la 1ere saillie','DONE','TANOS'),
    ('17',4,'2024-10-14',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('17',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',1,'2024-01-21',None,None,'2024-05-15',11,0,0,4,7,0,0,'2024-06-29',11,'','DONE','TANOS'),
    ('19',2,'2024-07-27',None,None,'2024-11-18',13,0,0,3,8,1,1,'2025-01-07',11,'','DONE','TANOS'),
    ('19',3,'2025-02-16',None,None,'2025-06-12',11,0,0,4,7,0,0,'2025-07-20',11,'','DONE','TANOS'),
    ('19',4,'2025-08-14',None,None,'2025-12-08',10,1,0,0,0,0,0,'2026-01-22',0,'','MB','TANOS'),
    ('19',5,'2026-01-25',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('19',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',1,'2024-03-12',None,None,'2024-07-01',11,0,-6,4,2,2,3,'2024-08-15',6,'Refus d\'allaiter ses porcelets. 5 morts le 2 jullet,les 6 autres mis en adoption chez la TRUI 22 qui a fait sa mise bas le 6','DONE','TANOS'),
    ('20',2,'2024-07-30',None,None,'2024-11-21',9,0,0,3,6,0,0,'2025-01-07',9,'','DONE','TANOS'),
    ('20',3,'2025-02-28','2025-04-16','2025-10-18','2026-02-08',11,0,0,0,0,0,0,'2026-03-25',0,'retour','MB','TANOS'),
    ('20',4,'2026-03-28',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('20',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',1,'2024-03-16',None,None,None,10,0,0,4,6,0,0,None,10,'Retour sur saillie','DONE','TANOS'),
    ('21',2,None,'2025-01-07','2025-04-09','2025-07-30',9,1,0,4,5,0,0,'2025-09-12',9,'A REFORMER','DONE','TANOS'),
    ('21',3,'2025-09-18',None,None,'2026-01-10',0,0,0,0,0,0,0,None,0,'','UP','TANOS'),
    ('21',4,'2026-02-27',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('21',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',1,'2024-03-17',None,None,'2024-07-06',6,1,6,6,6,0,0,'2024-08-20',12,'Adoption des 6 porcelets de la truie 20','DONE','TANOS'),
    ('22',2,'2024-08-23','2025-02-10',None,'2025-06-06',8,0,0,4,4,0,0,'2025-07-20',8,'RETOUR SUR TOUTES LES SAILLIES','SVG','TANOS'),
    ('22',3,'2025-09-02',None,None,'2025-12-25',10,0,0,0,0,0,0,'2026-02-08',0,'','MB','TANOS'),
    ('22',4,'2026-02-11',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('22',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',1,'2024-03-18','2024-05-27',None,'2024-09-21',9,0,0,6,3,0,0,'2024-12-01',9,'Retour 1ere saillie\nRetour 2ie saillie','DONE','TANOS'),
    ('23',2,'2025-01-17',None,None,'2025-05-11',7,7,0,3,4,0,0,'2025-06-25',7,'accouchement momifiés','DONE','TANOS'),
    ('23',3,'2025-06-21',None,None,'2025-10-08',5,1,0,3,2,0,0,'2025-11-22',5,'','DONE','TANOS'),
    ('23',4,'2025-12-12',None,None,'2026-04-17',10,0,0,0,0,0,0,'2026-06-01',0,'','MB','TANOS'),
    ('23',5,'2026-06-04',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('23',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',1,'2024-05-28',None,None,'2024-09-19',0,0,0,0,0,0,0,None,0,'','UP','TANOS'),
    ('24',2,'2024-11-06',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',3,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('24',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',1,'2024-06-03',None,None,'2024-09-26',12,0,0,6,6,0,0,'2024-12-01',12,'','DONE','TANOS'),
    ('25',2,'2024-11-13','2025-01-20',None,'2025-05-16',8,8,0,0,0,0,0,'2025-06-30',0,'FAUSSE COUCHE','DONE','TANOS'),
    ('25',3,'2025-08-12',None,None,'2025-12-06',14,0,0,0,0,0,0,'2026-01-20',0,'','MB','TANOS'),
    ('25',4,'2026-01-23',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('25',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','TANOS'),
    ('26',1,'2025-05-24','2025-07-08',None,'2025-10-31',0,0,0,0,0,0,0,None,0,'RETOUR','UP','ARES'),
    ('26',2,'2025-12-18',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',3,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('26',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',1,'2025-05-31',None,None,'2025-09-23',0,0,0,0,0,0,0,None,0,'','UP','ARES'),
    ('27',2,'2025-11-10',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',3,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('27',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',1,'2025-06-02',None,None,'2025-09-25',9,0,0,6,3,0,0,'2025-11-09',9,'','DONE','ARES'),
    ('28',2,'2026-01-06',None,None,'2026-04-30',0,0,0,0,0,0,0,None,0,'','UP','ARES'),
    ('28',3,'2026-06-17',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('28',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',1,'2025-07-22',None,None,'2025-11-14',0,0,0,0,0,0,0,None,0,'','UP','ARES'),
    ('29',2,'2026-01-01',None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',3,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',4,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',5,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',6,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',7,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',8,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',9,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES'),
    ('29',10,None,None,None,None,0,0,0,0,0,0,0,None,0,'','HIDE','ARES')
]

    # Construire le mapping (truie_num, rang) -> cycle_id
    cycle_rows = db.execute("""
        SELECT c.id, trim(substr(a.feuille,7)) as tnum, c.numero_portee
        FROM cycles c JOIN animaux a ON a.id=c.animal_id
        WHERE a.type_animal='truie'
    """).fetchall()
    cycle_map = {}
    for r in cycle_rows:
        try:
            cycle_map[(r['tnum'], int(r['numero_portee']))] = r['id']
        except (TypeError, ValueError):
            pass

    updated = 0
    db.execute("BEGIN")
    # Vérifier si la migration initiale a déjà été effectuée
    # (utilise la table parametres — créée plus bas mais on peut la créer ici aussi)
    db.execute("CREATE TABLE IF NOT EXISTS parametres (cle TEXT PRIMARY KEY, valeur TEXT NOT NULL)")
    already_done = db.execute(
        "SELECT valeur FROM parametres WHERE cle='excel_migration_done'"
    ).fetchone()

    if not already_done:
        for row in EXCEL_DATA:
            (tnum, rang, s1, s2, s3, mb, nv, mn, adopt, sm, sf,
             mpm, mpf, svr, sevres, obs, code, verrat) = row
            cid = cycle_map.get((tnum, rang))
            if not cid:
                continue
            db.execute("""
                UPDATE cycles SET
                    code=?, verrat_nom=?,
                    saillie_1=?, saillie_2=?, saillie_3=?,
                    date_mise_bas=?,
                    nais_vivants=?, morts_nes=?, adoption=?,
                    sexe_m=?, sexe_f=?,
                    mort_pre_m=?, mort_pre_f=?,
                    date_sevrage=?, sevres=?,
                    observations=?
                WHERE id=?
            """, (code, verrat, s1, s2, s3, mb, nv, mn, adopt,
                  sm, sf, mpm, mpf, svr, sevres, obs, cid))
            updated += 1
        db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('excel_migration_done','1')")
        db.commit()
        if updated:
            print(f"  ✓ Migration initiale: {updated} portées chargées depuis Excel")
    else:
        print("  ✓ Migration Excel déjà effectuée — données utilisateur conservées")

    # 2bis. Correctif rétroactif : sevres_m/sevres_f n'étaient jamais écrits par les
    # anciennes routes (add/update_portee) — la carte loge maternité et les KPI
    # lisaient donc des valeurs à 0, ou (import Excel) des valeurs ne tenant pas
    # compte de l'adoption, créant un double comptage donneur/receveuse
    # (PROBLÈME C — ex. T20 r1 donnait 6 porcelets à T22 mais gardait sevres_m/f
    # non nuls). On recalcule pour TOUTES les portées via la règle (d) :
    # sevres_m/f = max(0, nés - morts_pré_sevrage + adopt), cohérente avec
    # epCalcSevres() côté front. Idempotent (flag dédié) — n'écrase pas les
    # saisies faites depuis la mise en place du correctif (date plus récente).
    already_sevres_fix = db.execute(
        "SELECT valeur FROM parametres WHERE cle='sevres_mf_backfill_v2'"
    ).fetchone()
    if not already_sevres_fix:
        db.execute("""
            UPDATE cycles SET
                sevres_m = MAX(0, COALESCE(sexe_m,0) - COALESCE(mort_pre_m,0) + COALESCE(adopt_m,0)),
                sevres_f = MAX(0, COALESCE(sexe_f,0) - COALESCE(mort_pre_f,0) + COALESCE(adopt_f,0))
        """)
        db.execute("UPDATE cycles SET sevres = sevres_m + sevres_f")
        n_fixed = db.execute("SELECT changes()").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('sevres_mf_backfill_v2','1')")
        db.commit()
        print(f"  ✓ Correctif sevres_m/sevres_f recalculés pour {n_fixed} portées (PROBLÈME C)")

    # 3. Créer la table parametres si absente
    db.execute("""
        CREATE TABLE IF NOT EXISTS parametres (
            cle   TEXT PRIMARY KEY,
            valeur TEXT NOT NULL
        )
    """)
    # Insérer les valeurs par défaut si la table est vide
    defaults = [
        ('gestation',  '114'),
        ('allaitement', '28'),
        ('retour_chaleur', '7'),
        ('retard_chaleur', '1'),
        ('nb_verrats', '4'),
    ]
    for cle, val in defaults:
        db.execute("INSERT OR IGNORE INTO parametres (cle, valeur) VALUES (?,?)", (cle, val))

    # 4. Ajouter les colonnes manquantes à cycles
    cols_cycles = [c['name'] for c in db.execute("PRAGMA table_info(cycles)").fetchall()]
    new_cols = [
        ("poids_svr_json",   "TEXT"),
        ("observations",     "TEXT DEFAULT ''"),
        ("type_saillie",     "TEXT DEFAULT 'Accouplement'"),
        ("insem_labo",       "TEXT DEFAULT ''"),
        ("insem_serie",      "TEXT DEFAULT ''"),
        ("insem_lot",        "TEXT DEFAULT ''"),
        ("insem_race",       "TEXT DEFAULT ''"),
        ("insem_extraction", "TEXT"),
        ("insem_peremption", "TEXT"),
        ("autre_verrat_nom", "TEXT DEFAULT ''"),
        ("autre_verrat_race","TEXT DEFAULT ''"),
        ("autre_verrat_ferme","TEXT DEFAULT ''"),
        ("adopt_m",           "INTEGER DEFAULT 0"),
        ("adopt_f",           "INTEGER DEFAULT 0"),
        ("adopt_truie",       "TEXT DEFAULT ''"),
    ]
    for col, defn in new_cols:
        if col not in cols_cycles:
            db.execute(f"ALTER TABLE cycles ADD COLUMN {col} {defn}")
            print(f"  ✓ Migration: colonne cycles.{col} ajoutée")

    # 5. Tables stock_congelateur et ventes_individuelles
    db.execute('''CREATE TABLE IF NOT EXISTS employes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        poste TEXT DEFAULT '',
        salaire INTEGER DEFAULT 0,
        subsistance INTEGER DEFAULT 0,
        tel TEXT DEFAULT '',
        localisation TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        actif INTEGER DEFAULT 1
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS stock_congelateur (
        id INTEGER PRIMARY KEY AUTOINCREMENT, casier INTEGER, coupe TEXT,
        poids REAL, prix_unitaire INTEGER, statut TEXT DEFAULT "Disponible",
        date_abattage TEXT, date_vente TEXT, client TEXT DEFAULT "", prix_vente INTEGER DEFAULT 0,
        observations TEXT DEFAULT "", created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS ventes_individuelles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date_vente TEXT, client TEXT,
        telephone TEXT DEFAULT "", coupe TEXT, poids REAL, prix_unitaire INTEGER,
        prix_total INTEGER, casier INTEGER, stock_id INTEGER,
        mode_paiement TEXT DEFAULT "Espèces", statut_paiement TEXT DEFAULT "Payé",
        observations TEXT DEFAULT "", transaction_id TEXT DEFAULT "",
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS commandes_detail (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date_commande TEXT, client TEXT,
        telephone TEXT DEFAULT "", adresse_livraison TEXT DEFAULT "",
        mode_paiement TEXT DEFAULT "Espèces",
        statut_paiement TEXT DEFAULT "En attente de paiement",
        statut_livraison TEXT DEFAULT "En attente de livraison",
        lignes_json TEXT DEFAULT "[]", total_fcfa INTEGER DEFAULT 0,
        observations TEXT DEFAULT "", created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    # 6. Normaliser numero_portee (convertir textes en entiers)
    import re as _re2
    cycles_raw = db.execute("SELECT id, numero_portee FROM cycles").fetchall()
    for row in cycles_raw:
        val = row['numero_portee']
        try:
            int(float(val))
        except (ValueError, TypeError):
            m = _re2.search(r'\d+', str(val) if val else '')
            new_val = int(m.group()) if m else 1
            db.execute("UPDATE cycles SET numero_portee=? WHERE id=?", (new_val, row['id']))
            print(f"  ✓ Migration cycles.{row['id']}: numero_portee '{val}' → {new_val}")

    # Migration table commandes : ajouter colonnes manquantes
    cols_commandes = [c[1] for c in db.execute("PRAGMA table_info(commandes)").fetchall()]
    for col, defn in [
        ('telephone',       'TEXT DEFAULT ""'),
        ('adresse',         'TEXT DEFAULT ""'),
        ('details_morceaux','TEXT DEFAULT ""'),
        ('montant_note',    'TEXT DEFAULT ""'),
        ('note_raw',        'TEXT DEFAULT ""'),
        ('statut_paiement', 'TEXT DEFAULT "En attente de paiement"'),
        ('mode_paiement',   'TEXT DEFAULT "Espèces"'),
        ('observations',    'TEXT DEFAULT ""'),
    ]:
        if col not in cols_commandes:
            db.execute(f'ALTER TABLE commandes ADD COLUMN {col} {defn}')
            print(f'  ✓ Migration commandes: colonne {col} ajoutée')

    # Tables acheteurs et clients_commande
    db.execute('''CREATE TABLE IF NOT EXISTS acheteurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        type_acheteur TEXT DEFAULT 'Acheteur local',
        telephone TEXT DEFAULT '',
        adresse TEXT DEFAULT '',
        email TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at DATETIME DEFAULT (datetime('now'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS clients_commande (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        telephone TEXT DEFAULT '',
        adresse TEXT DEFAULT '',
        email TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at DATETIME DEFAULT (datetime('now'))
    )''')

    # Colonne origine dans truies
    try:
        db.execute("ALTER TABLE truies ADD COLUMN origine TEXT DEFAULT '—'")
    except: pass
    try:
        db.execute("UPDATE truies SET origine=COALESCE((SELECT a.origine FROM animaux a WHERE a.type_animal='truie' AND trim(substr(a.feuille,7))=truies.numero LIMIT 1),'—') WHERE origine IS NULL OR origine IN ('—','')")
    except: pass

    # Ajustement historique : porcs vendus par commande depuis 2024 (modifiable)
    db.execute("INSERT OR IGNORE INTO parametres (cle, valeur) VALUES ('porcs_commande_historique', '23')")
    # Population active de référence (effectif réel recensé) — base de calcul
    import json as _json
    _pop_base = _json.dumps({"verrats":3,"truies":17,"sous_mere":29,"sevres_demarrage":6,
                             "demarrage":23,"finition":41,"cochettes":4,"date_base":"2026-05-31"})
    db.execute("INSERT OR IGNORE INTO parametres (cle, valeur) VALUES ('population_base', ?)", (_pop_base,))
    db.execute("INSERT OR IGNORE INTO parametres (cle, valeur) VALUES ('capacite_cheptel', '150')")
    _seed_salaires_2026(db)
    _seed_acheteurs_clients(db)

    # Identification & Traçabilité (tatouage)
    db.execute('''CREATE TABLE IF NOT EXISTS identification_porcelets (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        code_tattoo    TEXT NOT NULL UNIQUE,
        sexe           TEXT DEFAULT 'M',
        date_naissance TEXT,
        truie_mere     TEXT DEFAULT '',
        verrat_pere    TEXT DEFAULT '',
        rang_portee    INTEGER DEFAULT 1,
        poids_naissance REAL DEFAULT 0,
        poids_sevrage   REAL DEFAULT 0,
        statut         TEXT DEFAULT 'Actif',
        destination    TEXT DEFAULT '',
        notes          TEXT DEFAULT '',
        created_at     DATETIME DEFAULT (datetime('now'))
    )''')

    # ── Stock premix & suppléments (par groupe/fournisseur) ──────────────
    db.execute('''CREATE TABLE IF NOT EXISTS premix_stock (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        groupe        TEXT NOT NULL DEFAULT 'NUTRIKA',
        categorie     TEXT NOT NULL DEFAULT 'premix',
        nom           TEXT NOT NULL,
        taille_sac_kg REAL DEFAULT 25,
        stock_kg      REAL DEFAULT 0,
        seuil_kg      REAL DEFAULT 0,
        prix_sac      REAL DEFAULT 0,
        created_at    DATETIME DEFAULT (datetime('now'))
    )''')
    # Capacité de jauge réglable par article (kg) — 0 = défaut selon catégorie
    _ps_cols = [r[1] for r in db.execute("PRAGMA table_info(premix_stock)").fetchall()]
    if 'capacite_kg' not in _ps_cols:
        db.execute("ALTER TABLE premix_stock ADD COLUMN capacite_kg REAL DEFAULT 0")
    db.execute('''CREATE TABLE IF NOT EXISTS premix_commandes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        date_commande TEXT,
        groupe        TEXT DEFAULT 'NUTRIKA',
        lignes_json   TEXT DEFAULT '[]',
        total_kg      REAL DEFAULT 0,
        total_sacs    INTEGER DEFAULT 0,
        total_montant REAL DEFAULT 0,
        statut        TEXT DEFAULT 'Commandé',
        created_at    DATETIME DEFAULT (datetime('now'))
    )''')
    # Colonnes paiement & livraison (ajout progressif si absentes)
    _pc_cols = [r[1] for r in db.execute("PRAGMA table_info(premix_commandes)").fetchall()]
    _pc_add = {
        'mode_paiement':   "TEXT DEFAULT 'Espèces'",
        'versements_json': "TEXT DEFAULT '[]'",
        'statut_paiement': "TEXT DEFAULT 'Non payé'",
        'frais_livraison': "REAL DEFAULT 0",
        'lieu_livraison':  "TEXT DEFAULT ''",
        'date_livraison':  "TEXT DEFAULT ''",
        'statut_livraison':"TEXT DEFAULT 'En attente'",
        'fournisseur':     "TEXT DEFAULT ''",
    }
    for _c, _t in _pc_add.items():
        if _c not in _pc_cols:
            db.execute(f"ALTER TABLE premix_commandes ADD COLUMN {_c} {_t}")

    # --- Lot 2 : flag d'application au stock (entrée à la livraison) ---
    if 'stock_applique' not in _pc_cols:
        db.execute("ALTER TABLE premix_commandes ADD COLUMN stock_applique INTEGER DEFAULT 0")
        # Reprise : l'ancien code ajoutait les kg au stock dès la création.
        # On marque donc TOUTES les commandes existantes comme déjà appliquées
        # pour éviter un double comptage lors d'une future livraison.
        db.execute("UPDATE premix_commandes SET stock_applique = 1")

    # --- Lot 3 : table des aliments finis disponibles ---
    db.execute('''CREATE TABLE IF NOT EXISTS aliments_stock (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        cle           TEXT UNIQUE,
        libelle       TEXT,
        stock_kg      REAL DEFAULT 0,
        taille_sac_kg REAL DEFAULT 50,
        capacite_kg   REAL DEFAULT 0,
        seuil_kg      REAL DEFAULT 0,
        date_maj      TEXT
    )''')
    if db.execute("SELECT COUNT(*) c FROM aliments_stock").fetchone()['c'] == 0:
        _today = datetime.now().strftime('%Y-%m-%d')
        # Seed initial : date_maj = aujourd'hui pour ne pas décrémenter rétroactivement
        db.executemany(
            "INSERT INTO aliments_stock (cle,libelle,stock_kg,taille_sac_kg,capacite_kg,seuil_kg,date_maj) VALUES (?,?,?,?,?,?,?)",
            [
                ('demarrage', 'Aliment démarrage',  100.0,  50.0, 1000.0,  100.0, _today),
                ('allaitante','Aliment allaitante', 400.0,  50.0, 1000.0,  100.0, _today),
                ('finition',  'Aliment finition',  7300.0,  50.0, 8000.0, 1000.0, _today),
                ('granule',   'Granulé pré-sevrage',  0.0,  20.0,  500.0,   50.0, _today),
            ])
    # Paramètre conso/jour granulé par porcelet sous mère (G2)
    if db.execute("SELECT COUNT(*) c FROM parametres WHERE cle='granule_kgjour_par_porcelet'").fetchone()['c'] == 0:
        db.execute("INSERT INTO parametres (cle,valeur) VALUES ('granule_kgjour_par_porcelet','0.25')")
    # Vide sanitaire maternité : durée FIXE globale (jours), défaut 5
    if db.execute("SELECT COUNT(*) c FROM parametres WHERE cle='vide_sanitaire_jours'").fetchone()['c'] == 0:
        db.execute("INSERT INTO parametres (cle,valeur) VALUES ('vide_sanitaire_jours','5')")

    # Articles par défaut si la table est vide
    if db.execute("SELECT COUNT(*) c FROM premix_stock").fetchone()['c'] == 0:
        defaults = [
            ('NUTRIKA','premix','PREMIX Croissance/Finition',25,0,50,38000),
            ('NUTRIKA','premix','PREMIX Truie 2,5',25,0,50,34000),
            ('NUTRIKA','premix','PREMIX Démarrage 2,5',25,0,50,35000),
            ('NUTRIKA','supplement','TOXFIN (capteur mycotoxine)',25,0,25,50000),
            ('NUTRIKA','supplement','CLOSTAT (Probiotique santé intestinale)',25,0,25,125000),
            ('NUTRIKA','supplement','SALCURB (Traitement aliment contre les bactéries)',25,0,25,37500),
            ('NUTRIKA','autre','Sac (emballage)',1,0,50,200),
            ('NUTRIKA','autre','Sac de Chaux',1,0,5,3000),
        ]
        db.executemany(
            "INSERT INTO premix_stock (groupe,categorie,nom,taille_sac_kg,stock_kg,seuil_kg,prix_sac) VALUES (?,?,?,?,?,?,?)",
            defaults)

    # ── Loges & statut (migration douce) ─────────────────────────────────────
    db.execute('''CREATE TABLE IF NOT EXISTS loges (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        nom             TEXT NOT NULL,
        type            TEXT    DEFAULT '',
        capacite        INTEGER DEFAULT 1,
        statut          TEXT    DEFAULT 'disponible',
        occupant_type   TEXT    DEFAULT '',
        occupant_id     INTEGER,
        occupant_label  TEXT    DEFAULT '',
        raison_vide     TEXT    DEFAULT '',
        travaux_a_faire TEXT    DEFAULT '',
        etat_pipette    TEXT    DEFAULT 'fonctionnelle',
        ordre           INTEGER DEFAULT 0,
        notes           TEXT    DEFAULT ''
    )''')

    # ── Multi-occupants : table de liaison (1 animal ↔ 1 loge max, exclusif) ──
    db.execute('''CREATE TABLE IF NOT EXISTS loge_occupants (
        loge_id   INTEGER NOT NULL,
        animal_id INTEGER NOT NULL,
        PRIMARY KEY (loge_id, animal_id)
    )''')
    db.execute("CREATE INDEX IF NOT EXISTS idx_loge_occ_animal ON loge_occupants(animal_id)")

    # ── Loges : extension schéma topologie (migration douce, idempotente) ────
    _loge_cols = [r[1] for r in db.execute("PRAGMA table_info(loges)").fetchall()]
    for _c, _ddl in (('batiment',      "batiment TEXT DEFAULT ''"),
                     ('cote',          "cote TEXT DEFAULT ''"),
                     ('position',      "position INTEGER DEFAULT 0"),
                     ('vis_a_vis_id',  "vis_a_vis_id INTEGER"),
                     ('occupant_autre', "occupant_autre TEXT DEFAULT ''"),
                     ('date_debut_occupation', "date_debut_occupation TEXT DEFAULT ''")):
        if _c not in _loge_cols:
            db.execute("ALTER TABLE loges ADD COLUMN " + _ddl)

    # ── Pré-remplissage des 40 loges réelles (idempotent, slot par slot) ─────
    #   Bât. A (26) : côté A = 12 gestantes vis-à-vis + 2 orphelines ; côté B = 12 maternités
    #   Bât. B (14) : 2 × 7 croissance/finition en vis-à-vis
    #   IMPORTANT : s'exécute même si la table contient déjà des loges manuelles.
    #   Chaque slot (batiment,cote,position) n'est inséré que s'il est libre, donc
    #   aucune loge manuelle existante n'est écrasée ni dupliquée.
    #   Flag v2 : redéclenche le seed sur les bases où l'ancien flag avait été posé à tort.
    _seeded = db.execute(
        "SELECT valeur FROM parametres WHERE cle='loges_topologie_done_v2'").fetchone()
    if not _seeded:
        _plan = []
        for _p in range(1, 15):   # Bât A côté A : 1-12 vis-à-vis + 13,14 orphelines
            _plan.append(('Gest. A%d' % _p, 'gestante', 'A', 'A', _p))
        for _p in range(1, 13):   # Bât A côté B : 12 maternités
            _plan.append(('Mat. B%d' % _p, 'maternite', 'A', 'B', _p))
        for _p in range(1, 8):    # Bât B côté A : 7
            _plan.append(('Crois. A%d' % _p, 'engraissement', 'B', 'A', _p))
        for _p in range(1, 8):    # Bât B côté B : 7
            _plan.append(('Crois. B%d' % _p, 'engraissement', 'B', 'B', _p))
        _ordre = db.execute("SELECT COALESCE(MAX(ordre),0) m FROM loges").fetchone()['m']
        for _nom, _typ, _bat, _cote, _pos in _plan:
            _exists = db.execute(
                "SELECT id FROM loges WHERE batiment=? AND cote=? AND position=?",
                (_bat, _cote, _pos)).fetchone()
            if _exists:
                continue   # slot déjà occupé (loge manuelle) → on n'y touche pas
            _ordre += 1
            db.execute(
                "INSERT INTO loges (nom,type,capacite,statut,batiment,cote,position,ordre) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_nom, _typ, 1, 'disponible', _bat, _cote, _pos, _ordre))
        # Liens vis-à-vis : côté A position p ↔ côté B position p (par bâtiment)
        for _bat, _maxp in (('A', 12), ('B', 7)):
            for _p in range(1, _maxp + 1):
                _ra = db.execute("SELECT id FROM loges WHERE batiment=? AND cote='A' AND position=?",
                                 (_bat, _p)).fetchone()
                _rb = db.execute("SELECT id FROM loges WHERE batiment=? AND cote='B' AND position=?",
                                 (_bat, _p)).fetchone()
                if _ra and _rb:
                    db.execute("UPDATE loges SET vis_a_vis_id=? WHERE id=?", (_rb['id'], _ra['id']))
                    db.execute("UPDATE loges SET vis_a_vis_id=? WHERE id=?", (_ra['id'], _rb['id']))
        db.execute("INSERT OR IGNORE INTO parametres (cle,valeur) VALUES ('loges_topologie_done_v2','1')")

    db.commit()
    db.close()


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ── Utilitaire calcul d'âge ─────────────────────────────────────────────────

MOIS_FR = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
}

def calc_age_from_mois_annee(mois_naissance, annee_naissance):
    """Calcule l'âge en années et mois à partir de mois_naissance (fr) et annee_naissance."""
    if not mois_naissance or not annee_naissance:
        return None
    try:
        mois = MOIS_FR.get(mois_naissance.strip().lower())
        annee = int(annee_naissance)
        if not mois:
            return None
        today = datetime.now().date()
        naissance = datetime(annee, mois, 1).date()
        delta_mois = (today.year - naissance.year) * 12 + (today.month - naissance.month)
        annees = delta_mois // 12
        mois_rest = delta_mois % 12
        if annees > 0:
            return f"{annees} an{'s' if annees > 1 else ''} {mois_rest} mois" if mois_rest else f"{annees} an{'s' if annees > 1 else ''}"
        return f"{mois_rest} mois"
    except Exception:
        return None


def calc_age_from_date(date_str):
    """Calcule l'âge à partir d'une date ISO (YYYY-MM-DD)."""
    if not date_str:
        return None
    try:
        naissance = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        today = datetime.now().date()
        delta_mois = (today.year - naissance.year) * 12 + (today.month - naissance.month)
        annees = delta_mois // 12
        mois_rest = delta_mois % 12
        if annees > 0:
            return f"{annees} an{'s' if annees > 1 else ''} {mois_rest} mois" if mois_rest else f"{annees} an{'s' if annees > 1 else ''}"
        return f"{mois_rest} mois"
    except Exception:
        return None


# ── Page principale ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_file(FRONT)


# ═══════════════════════════════════════════════════════════════════════════
# TRUIES
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/parametres', methods=['GET'])
def get_parametres():
    db = get_db()
    rows = db.execute("SELECT cle, valeur FROM parametres").fetchall()
    db.close()
    resp = jsonify({r['cle']: r['valeur'] for r in rows})
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/api/parametres', methods=['POST'])
def save_parametres():
    d = request.json  # {'gestation': '114', 'allaitement': '28', ...}
    db = get_db()
    for cle, valeur in d.items():
        db.execute(
            "INSERT INTO parametres (cle, valeur) VALUES (?,?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
            (cle, str(valeur))
        )
    db.commit()
    db.close()
    return jsonify({'message': 'Paramètres sauvegardés'})


@app.route('/api/truies', methods=['GET'])
def get_truies():
    db = get_db()
    rows = db.execute("SELECT * FROM truies ORDER BY numero").fetchall()
    truies = rows_to_list(rows)
    # Enrichir avec l'âge depuis la table animaux
    animaux_naiss = {}
    animaux_orig  = {}
    for a in db.execute(
        "SELECT feuille, mois_naissance, annee_naissance, origine FROM animaux WHERE type_animal='truie'"
    ).fetchall():
        num_padded = a['feuille'].replace('TRUIE', '').strip()
        animaux_naiss[num_padded] = (a['mois_naissance'], a['annee_naissance'])
        animaux_orig[num_padded]  = a['origine'] or '—'
    # Calculer sevres total, sevres_m, sevres_f depuis cycles (source de vérité)
    # NB: sevres_m/sevres_f sont déjà nets de l'adoption (signée +reçu/-donné côté
    # saisie), donc une SUM directe ne double-compte PAS un porcelet donné/reçu.
    cycles_stats = {}
    for r in db.execute("""
        SELECT trim(substr(a.feuille,7)) as num,
               SUM(c.sevres_m + c.sevres_f) as total_svr,
               SUM(c.sevres_m)  as total_m,
               SUM(c.sevres_f)  as total_f
        FROM cycles c
        JOIN animaux a ON a.id = c.animal_id
        WHERE a.type_animal='truie'
        GROUP BY a.feuille
    """).fetchall():
        cycles_stats[r['num']] = {
            'sevres':   r['total_svr'] or 0,
            'sevres_m': r['total_m']   or 0,
            'sevres_f': r['total_f']   or 0,
        }
    for t in truies:
        num = str(t.get('numero', '')).strip()
        mois, annee = animaux_naiss.get(num, (None, None))
        t['age'] = calc_age_from_mois_annee(mois, annee)
        t['mois_naissance'] = mois
        t['annee_naissance'] = annee
        stats = cycles_stats.get(num, {'sevres': 0, 'sevres_m': 0, 'sevres_f': 0})
        t['sevres']   = stats['sevres']
        t['sevres_m'] = stats['sevres_m']
        t['sevres_f'] = stats['sevres_f']
        if not t.get('origine') or t['origine'] in ('—', None, ''):
            t['origine'] = animaux_orig.get(num, '—')
    db.close()
    return jsonify(truies)


@app.route('/api/truies', methods=['POST'])
def add_truie():
    d = request.json
    db = get_db()
    cur = db.execute("""
        INSERT INTO truies (numero, race, statut, rang, sevres,
                            derniere_mb, reformer, observations)
        VALUES (?,?,?,?,?,?,?,?)
    """, (d['numero'], d.get('race',''), d.get('statut','Non saillée'),
          d.get('rang',0), d.get('sevres',0),
          d.get('derniere_mb'), 1 if d.get('reformer') else 0,
          d.get('observations','')))
    db.commit()
    new_id = cur.lastrowid
    # Sauvegarder mois/annee naissance dans animaux si fournis
    mois_naiss  = d.get('mois_naissance')
    annee_naiss = str(d.get('annee_naissance','')) if d.get('annee_naissance') else None
    num_padded  = str(d['numero']).zfill(2)
    feuille     = f"TRUIE {num_padded}"
    existing = db.execute("SELECT id FROM animaux WHERE feuille=? AND type_animal='truie'", (feuille,)).fetchone()
    if existing:
        db.execute("UPDATE animaux SET mois_naissance=?, annee_naissance=? WHERE feuille=? AND type_animal='truie'",
                   (mois_naiss, annee_naiss, feuille))
    else:
        db.execute("""INSERT INTO animaux (feuille, type_animal, numero_pk, nom, race, mois_naissance, annee_naissance, origine, reformer, elevage)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (feuille, 'truie', '', f"TRUIE {num_padded}", d.get('race',''),
                    mois_naiss, annee_naiss, d.get('origine',''), 0, 'LA COCHETTE DOREE'))
    db.commit()
    db.close()
    return jsonify({'id': new_id, 'message': 'Truie ajoutée'}), 201


@app.route('/api/truies/<int:tid>', methods=['PUT'])
def update_truie(tid):
    d = request.json
    db = get_db()
    db.execute("""
        UPDATE truies SET race=?, statut=?, rang=?, sevres=?,
          derniere_mb=?, reformer=?, observations=?,
          updated_at=datetime('now')
        WHERE id=?
    """, (d.get('race',''), d.get('statut',''), d.get('rang',0),
          d.get('sevres',0), d.get('derniere_mb'),
          1 if d.get('reformer') else 0,
          d.get('observations',''), tid))
    # Mettre à jour mois/annee de naissance dans la table animaux (toujours, même si vide)
    mois_naiss  = d.get('mois_naissance') or None
    annee_naiss = str(d.get('annee_naissance')).strip() if d.get('annee_naissance') else None
    truie = db.execute("SELECT numero FROM truies WHERE id=?", (tid,)).fetchone()
    if truie:
        num_padded = str(truie['numero']).zfill(2)
        feuille = f"TRUIE {num_padded}"
        existing = db.execute(
            "SELECT id FROM animaux WHERE feuille=? AND type_animal='truie'", (feuille,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE animaux SET mois_naissance=?, annee_naissance=? WHERE feuille=? AND type_animal='truie'",
                (mois_naiss, annee_naiss, feuille)
            )
        else:
            db.execute(
                """INSERT INTO animaux (feuille, type_animal, numero_pk, nom, race, mois_naissance, annee_naissance, reformer, elevage)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (feuille, 'truie', '', feuille, d.get('race',''),
                 mois_naiss, annee_naiss, 0, 'LA COCHETTE DOREE')
            )
    db.commit(); db.close()
    return jsonify({'message': 'Truie mise à jour'})


@app.route('/api/truies/<int:tid>', methods=['DELETE'])
def delete_truie(tid):
    db = get_db()
    db.execute("DELETE FROM truies WHERE id=?", (tid,))
    db.commit(); db.close()
    return jsonify({'message': 'Truie supprimée'})


# ═══════════════════════════════════════════════════════════════════════════
# VERRATS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/verrats', methods=['GET'])
def get_verrats():
    db = get_db()
    rows = db.execute("SELECT * FROM verrats ORDER BY numero").fetchall()
    verrats = rows_to_list(rows)
    # Enrichir avec l'âge : chercher dans animaux par feuille (ex: VERRAT 01) construite depuis le numéro
    for v in verrats:
        age = None
        mois, annee = None, None
        if v.get('date_naiss'):
            age = calc_age_from_date(v['date_naiss'])
        if not age:
            # Construire la feuille depuis le numéro du verrat (ex: V01 → VERRAT 01)
            num_str = str(v.get('numero', '')).replace('V', '').replace('v', '').strip().zfill(2)
            feuille = f"VERRAT {num_str}"
            a = db.execute(
                "SELECT mois_naissance, annee_naissance FROM animaux WHERE feuille=? AND type_animal='verrat'",
                (feuille,)
            ).fetchone()
            if a:
                mois, annee = a['mois_naissance'], a['annee_naissance']
                age = calc_age_from_mois_annee(mois, annee)
        v['mois_naissance'] = mois
        v['annee_naissance'] = annee
        v['age'] = age
    db.close()
    return jsonify(verrats)


@app.route('/api/verrats', methods=['POST'])
def add_verrat():
    d = request.json
    db = get_db()
    cur = db.execute("""
        INSERT INTO verrats (numero, nom, race, origine, date_naiss,
                             nb_saillies, fertilite, statut, reformer, observations)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (d['numero'], d.get('nom',''), d.get('race',''), d.get('origine',''),
          d.get('date_naiss'), d.get('nb_saillies',0), d.get('fertilite',0),
          d.get('statut','Actif'), 1 if d.get('reformer') else 0,
          d.get('observations','')))
    db.commit()
    new_id = cur.lastrowid
    # Sauvegarder mois/annee naissance dans animaux si fournis
    mois_naiss  = d.get('mois_naissance')
    annee_naiss = str(d.get('annee_naissance','')) if d.get('annee_naissance') else None
    nom_verrat  = d.get('nom', '').strip()
    num_padded  = str(d['numero']).replace('V','').zfill(2)
    feuille     = f"VERRAT {num_padded}"
    existing = db.execute("SELECT id FROM animaux WHERE feuille=? AND type_animal='verrat'", (feuille,)).fetchone()
    if existing:
        db.execute("UPDATE animaux SET mois_naissance=?, annee_naissance=?, nom=? WHERE feuille=? AND type_animal='verrat'",
                   (mois_naiss, annee_naiss, nom_verrat.upper(), feuille))
    else:
        db.execute("""INSERT INTO animaux (feuille, type_animal, numero_pk, nom, race, mois_naissance, annee_naissance, origine, reformer, elevage)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (feuille, 'verrat', '', nom_verrat.upper(), d.get('race',''),
                    mois_naiss, annee_naiss, d.get('origine',''), 0, 'LA COCHETTE DOREE'))
    db.commit()
    db.close()
    return jsonify({'id': new_id, 'message': 'Verrat ajouté'}), 201


@app.route('/api/verrats/<int:vid>', methods=['PUT'])
def update_verrat(vid):
    d = request.json
    db = get_db()
    db.execute("""
        UPDATE verrats SET nom=?, race=?, origine=?, date_naiss=?,
          nb_saillies=?, fertilite=?, statut=?, reformer=?, observations=?,
          updated_at=datetime('now')
        WHERE id=?
    """, (d.get('nom',''), d.get('race',''), d.get('origine',''),
          d.get('date_naiss'), d.get('nb_saillies',0), d.get('fertilite',0),
          d.get('statut','Actif'), 1 if d.get('reformer') else 0,
          d.get('observations',''), vid))
    # Mettre à jour mois/annee de naissance dans la table animaux (toujours, même si vide)
    mois_naiss  = d.get('mois_naissance') or None
    annee_naiss = str(d.get('annee_naissance')).strip() if d.get('annee_naissance') else None
    verrat = db.execute("SELECT numero, nom FROM verrats WHERE id=?", (vid,)).fetchone()
    if verrat:
        num_str = str(verrat['numero']).replace('V', '').replace('v', '').strip().zfill(2)
        feuille = f"VERRAT {num_str}"
        existing = db.execute(
            "SELECT id FROM animaux WHERE feuille=? AND type_animal='verrat'", (feuille,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE animaux SET mois_naissance=?, annee_naissance=? WHERE feuille=? AND type_animal='verrat'",
                (mois_naiss, annee_naiss, feuille)
            )
        else:
            db.execute(
                """INSERT INTO animaux (feuille, type_animal, numero_pk, nom, race, mois_naissance, annee_naissance, reformer, elevage)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (feuille, 'verrat', '', str(verrat['nom']).upper(), d.get('race', ''),
                 mois_naiss, annee_naiss, 0, 'LA COCHETTE DOREE')
            )
    db.commit(); db.close()
    return jsonify({'message': 'Verrat mis à jour'})


@app.route('/api/verrats/<int:vid>', methods=['DELETE'])
def delete_verrat(vid):
    db = get_db()
    db.execute("DELETE FROM verrats WHERE id=?", (vid,))
    db.commit(); db.close()
    return jsonify({'message': 'Verrat supprimé'})


# ═══════════════════════════════════════════════════════════════════════════
# PORTÉES
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/portees', methods=['GET'])
def get_portees():
    """Lit les portées depuis la table cycles (source de vérité réelle)."""
    truie_id = request.args.get('truie_id')
    db = get_db()
    if truie_id:
        # Retrouver le numéro de la truie depuis truies.id
        t = db.execute("SELECT numero FROM truies WHERE id=?", (truie_id,)).fetchone()
        num = t['numero'] if t else None
        if num:
            rows = db.execute("""
                SELECT c.*, substr(a.feuille,7) as truie_num
                FROM cycles c
                JOIN animaux a ON a.id = c.animal_id
                WHERE a.type_animal='truie'
                  AND trim(substr(a.feuille,7)) = ?
                ORDER BY c.numero_portee
            """, (num.strip(),)).fetchall()
        else:
            rows = []
    else:
        rows = db.execute("""
            SELECT c.*, trim(substr(a.feuille,7)) as truie_num
            FROM cycles c
            JOIN animaux a ON a.id = c.animal_id
            WHERE a.type_animal = 'truie'
            ORDER BY CAST(trim(substr(a.feuille,7)) AS INTEGER), c.numero_portee
        """).fetchall()
    db.close()

    result = []
    for r in rows:
        row = dict(r)
        sevres_total = row.get('sevres') or 0
        result.append({
            'id':                  row['id'],
            'truie_num':           row.get('truie_num','').strip(),
            'rang_portee':         _parse_rang(row.get('numero_portee')),
            'code_statut':         row.get('code') or 'DONE',
            'verrat_nom':          (row.get('verrat_nom') or '').strip(),
            'type_saillie':        row.get('type_saillie') or 'Accouplement',
            'insem_labo':          row.get('insem_labo') or '',
            'insem_race':          row.get('insem_race') or '',
            'insem_serie':         row.get('insem_serie') or '',
            'insem_lot':           row.get('insem_lot') or '',
            'insem_extraction':    row.get('insem_extraction') or '',
            'insem_peremption':    row.get('insem_peremption') or '',
            'autre_verrat_nom':    row.get('autre_verrat_nom') or '',
            'autre_verrat_race':   row.get('autre_verrat_race') or '',
            'autre_verrat_ferme':  row.get('autre_verrat_ferme') or '',
            'date_saillie_1':      row.get('saillie_1'),
            'date_saillie_2':      row.get('saillie_2'),
            'date_saillie_3':      row.get('saillie_3'),
            'date_mb':             row.get('date_mise_bas'),
            'date_mb_prevue':      None,
            'nes_vivants':         row.get('nais_vivants') or 0,
            'morts_nes':           row.get('morts_nes') or 0,
            'adoptions':           row.get('adoption') or 0,
            'males':               row.get('sexe_m') or 0,
            'femelles':            row.get('sexe_f') or 0,
            'mort_pre_sev_m':      row.get('mort_pre_m') or 0,
            'mort_pre_sev_f':      row.get('mort_pre_f') or 0,
            'mort_post_sev_m':     row.get('mort_post_m') or 0,
            'mort_post_sev_f':     row.get('mort_post_f') or 0,
            'date_sevrage':        row.get('date_sevrage'),
            'date_sevrage_prevue': (
                (datetime.strptime(row['date_mise_bas'], '%Y-%m-%d') + timedelta(days=28)).strftime('%Y-%m-%d')
                if row.get('date_mise_bas') else None
            ),
            'sevres_m':            row.get('sevres_m') or 0,
            'sevres_f':            row.get('sevres_f') or 0,
            'sevres':              row.get('sevres')  or 0,
            'proch_saillie_prevue':None,
            'adopt_m':            row.get('adopt_m')    or 0,
            'adopt_f':            row.get('adopt_f')    or 0,
            'adopt_truie':        row.get('adopt_truie') or '',
            'observations':        row.get('observations') or '',
            'poids_svr':           json.loads(row['poids_svr_json']) if row.get('poids_svr_json') else None,
        })
    return jsonify(result)


@app.route('/api/portees', methods=['POST'])
def add_portee():
    """Ajoute une portée dans cycles."""
    d = request.json
    db = get_db()
    # Retrouver animal_id depuis le numéro de truie
    truie_num = d.get('truie_num') or ''
    animal = db.execute(
        "SELECT id FROM animaux WHERE type_animal='truie' AND trim(substr(feuille,7))=?",
        (truie_num.strip(),)
    ).fetchone()
    if not animal:
        db.close()
        return jsonify({'error': 'Truie introuvable'}), 404
    animal_id = animal['id']

    # Vérifier les colonnes disponibles pour adapter l'INSERT
    cols_ok = [c['name'] for c in db.execute("PRAGMA table_info(cycles)").fetchall()]
    has_new = 'type_saillie' in cols_ok

    if has_new:
        cur = db.execute("""
            INSERT INTO cycles (animal_id, code, numero_portee, verrat_nom,
              saillie_1, saillie_2, saillie_3, date_mise_bas,
              nais_vivants, morts_nes, adoption, sexe_m, sexe_f,
              mort_pre_m, mort_pre_f, date_sevrage, sevres, sevres_m, sevres_f,
              type_saillie, insem_labo, insem_serie, insem_lot, insem_race,
              insem_extraction, insem_peremption,
              autre_verrat_nom, autre_verrat_race, autre_verrat_ferme,
              adopt_m, adopt_f, adopt_truie,
              observations)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            animal_id,
            d.get('code_statut','UP'),
            d.get('rang_portee', 1),
            d.get('verrat_nom',''),
            d.get('date_saillie_1'), d.get('date_saillie_2'), d.get('date_saillie_3'),
            d.get('date_mb'),
            d.get('nes_vivants', 0), d.get('morts_nes', 0), d.get('adoptions', 0),
            d.get('males', 0), d.get('femelles', 0),
            d.get('mort_pre_sev_m', 0), d.get('mort_pre_sev_f', 0),
            d.get('date_sevrage'),
            (d.get('sevres_m', 0) or 0) + (d.get('sevres_f', 0) or 0),
            d.get('sevres_m', 0) or 0, d.get('sevres_f', 0) or 0,
            d.get('type_saillie', 'Accouplement'),
            d.get('insem_labo',''), d.get('insem_serie',''), d.get('insem_lot',''),
            d.get('insem_race',''),
            d.get('insem_extraction') or None, d.get('insem_peremption') or None,
            d.get('autre_verrat_nom',''), d.get('autre_verrat_race',''), d.get('autre_verrat_ferme',''),
            d.get('adopt_m', 0), d.get('adopt_f', 0), d.get('adopt_truie', ''),
            d.get('observations',''),
        ))
    else:
        # Fallback pour DB sans les nouvelles colonnes (migration en cours)
        cur = db.execute("""
            INSERT INTO cycles (animal_id, code, numero_portee, verrat_nom,
              saillie_1, saillie_2, saillie_3, date_mise_bas,
              nais_vivants, morts_nes, adoption, sexe_m, sexe_f,
              mort_pre_m, mort_pre_f, date_sevrage, sevres)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            animal_id,
            d.get('code_statut','UP'),
            d.get('rang_portee', 1),
            d.get('verrat_nom',''),
            d.get('date_saillie_1'), d.get('date_saillie_2'), d.get('date_saillie_3'),
            d.get('date_mb'),
            d.get('nes_vivants', 0), d.get('morts_nes', 0), d.get('adoptions', 0),
            d.get('males', 0), d.get('femelles', 0),
            d.get('mort_pre_sev_m', 0), d.get('mort_pre_sev_f', 0),
            d.get('date_sevrage'),
            (d.get('sevres_m', 0) or 0) + (d.get('sevres_f', 0) or 0),
        ))
    db.commit()
    new_id = cur.lastrowid
    db.close()
    return jsonify({'id': new_id, 'message': 'Portée ajoutée'}), 201


@app.route('/api/portees/<int:pid>/poids', methods=['PUT'])
def update_portee_poids(pid):
    """Met à jour uniquement le champ poids_svr_json d'une portée."""
    d = request.json or {}
    db = get_db()
    db.execute(
        "UPDATE cycles SET poids_svr_json=? WHERE id=?",
        (d.get('poids_svr_json', ''), pid)
    )
    db.commit()
    return jsonify({'ok': True, 'message': 'Poids sevrage sauvegardés'})


@app.route('/api/portees/<int:pid>', methods=['PUT'])
def update_portee(pid):
    """Met à jour une portée dans cycles."""
    d = request.json or {}
    print(f"[PUT /api/portees/{pid}] date_sevrage={d.get('date_sevrage')} code={d.get('code_statut')} verrat={d.get('verrat_nom')}")
    db = get_db()
    cols = [c['name'] for c in db.execute("PRAGMA table_info(cycles)").fetchall()]
    has_new = 'type_saillie' in cols

    sevres_total = (d.get('sevres_m', 0) or 0) + (d.get('sevres_f', 0) or 0)

    # Ajouter les colonnes manquantes si nécessaire avant l'UPDATE
    for col, defn in [('type_saillie','TEXT DEFAULT "Accouplement"'),
                      ('insem_labo','TEXT DEFAULT ""'), ('insem_serie','TEXT DEFAULT ""'),
                      ('insem_lot','TEXT DEFAULT ""'), ('insem_race','TEXT DEFAULT ""'),
                      ('insem_extraction','TEXT'), ('insem_peremption','TEXT'),
                      ('autre_verrat_nom','TEXT DEFAULT ""'), ('autre_verrat_race','TEXT DEFAULT ""'),
                      ('autre_verrat_ferme','TEXT DEFAULT ""'), ('observations','TEXT DEFAULT ""'),
                      ('adopt_m','INTEGER DEFAULT 0'), ('adopt_f','INTEGER DEFAULT 0'), ('adopt_truie','TEXT DEFAULT ""')]:
        if col not in cols:
            db.execute(f"ALTER TABLE cycles ADD COLUMN {col} {defn}")
            db.commit()

    try:
     db.execute("""
        UPDATE cycles SET
          code=?, verrat_nom=?,
          saillie_1=?, saillie_2=?, saillie_3=?,
          date_mise_bas=?,
          nais_vivants=?, morts_nes=?, adoption=?,
          sexe_m=?, sexe_f=?,
          mort_pre_m=?, mort_pre_f=?,
          date_sevrage=?, sevres=?, sevres_m=?, sevres_f=?,
          type_saillie=?,
          insem_labo=?, insem_serie=?, insem_lot=?, insem_race=?,
          insem_extraction=?, insem_peremption=?,
          autre_verrat_nom=?, autre_verrat_race=?, autre_verrat_ferme=?,
          adopt_m=?, adopt_f=?, adopt_truie=?,
          observations=?
        WHERE id=?
    """, (
        d.get('code_statut'), (d.get('verrat_nom') or '').strip(),
        d.get('date_saillie_1'), d.get('date_saillie_2'), d.get('date_saillie_3'),
        d.get('date_mb'),
        d.get('nes_vivants', 0), d.get('morts_nes', 0), d.get('adoptions', 0),
        d.get('males', 0), d.get('femelles', 0),
        d.get('mort_pre_sev_m', 0), d.get('mort_pre_sev_f', 0),
        d.get('date_sevrage'), sevres_total, d.get('sevres_m', 0) or 0, d.get('sevres_f', 0) or 0,
        d.get('type_saillie', 'Accouplement'),
        d.get('insem_labo', ''), d.get('insem_serie', ''), d.get('insem_lot', ''), d.get('insem_race', ''),
        d.get('insem_extraction') or None, d.get('insem_peremption') or None,
        d.get('autre_verrat_nom', ''), d.get('autre_verrat_race', ''), d.get('autre_verrat_ferme', ''),
        d.get('adopt_m', 0), d.get('adopt_f', 0), d.get('adopt_truie', ''),
        d.get('observations', ''),
        pid
    ))
     db.commit()
    except Exception as e:
        db.close()
        print(f"[PUT /api/portees/{pid}] ERREUR SQL: {e}")
        return jsonify({'error': str(e)}), 500
    # Vérifier que la valeur est bien en DB
    check = db.execute("SELECT date_sevrage, code, adopt_m, adopt_f, adopt_truie FROM cycles WHERE id=?", (pid,)).fetchone()
    if check:
        print(f"[PUT /api/portees/{pid}] COMMIT OK — adopt_m={check['adopt_m']} adopt_f={check['adopt_f']} adopt_truie={check['adopt_truie']}")
    else:
        print(f"[PUT /api/portees/{pid}] ATTENTION: aucune ligne trouvée avec id={pid}")
    db.close()
    return jsonify({'ok': True, 'message': 'Portée mise à jour', 'pid': pid})


@app.route('/api/portees/<int:pid>', methods=['DELETE'])
def delete_portee(pid):
    """Supprime une portée de cycles."""
    db = get_db()
    db.execute("DELETE FROM cycles WHERE id=?", (pid,))
    db.commit(); db.close()
    return jsonify({'message': 'Portée supprimée'})


# ═══════════════════════════════════════════════════════════════════════════
# PORCELETS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/porcelets', methods=['GET'])
def get_porcelets():
    db = get_db()
    rows = db.execute("""
        SELECT p.*, t.numero as truie_num FROM porcelets p
        LEFT JOIN truies t ON t.id=p.truie_id
        ORDER BY p.date_naiss DESC
    """).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/porcelets', methods=['POST'])
def add_porcelet():
    d = request.json
    db = get_db()
    cur = db.execute("""
        INSERT INTO porcelets
          (truie_id, portee_id, date_naiss, nes_vivants, males, femelles,
           adoptions_recus, adoptions_donnes, stade, nb_sevres, observations)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (d.get('truie_id'), d.get('portee_id'), d.get('date_naiss'),
          d.get('nes_vivants',0), d.get('males',0), d.get('femelles',0),
          d.get('adoptions_recus',0), d.get('adoptions_donnes',0),
          d.get('stade','Sous mère'), d.get('nb_sevres',0),
          d.get('observations','')))
    db.commit()
    new_id = cur.lastrowid; db.close()
    return jsonify({'id': new_id, 'message': 'Porcelet ajouté'}), 201


@app.route('/api/porcelets/<int:pid>', methods=['PUT'])
def update_porcelet(pid):
    d = request.json
    db = get_db()
    db.execute("""
        UPDATE porcelets SET stade=?, nb_sevres=?, observations=?,
          updated_at=datetime('now') WHERE id=?
    """, (d.get('stade'), d.get('nb_sevres',0), d.get('observations',''), pid))
    db.commit(); db.close()
    return jsonify({'message': 'Mis à jour'})


# ═══════════════════════════════════════════════════════════════════════════
# VÉTÉRINAIRE  (table réelle : trousse_veterinaire)
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_veto_cols(db):
    """Migration douce : ajoute les colonnes catégorie / stock_min / observations si absentes."""
    cols = [c[1] for c in db.execute("PRAGMA table_info(trousse_veterinaire)").fetchall()]
    for col, defn in (('categorie', "TEXT DEFAULT ''"),
                      ('stock_min', "INTEGER DEFAULT 0"),
                      ('observations', "TEXT DEFAULT ''")):
        if col not in cols:
            db.execute(f"ALTER TABLE trousse_veterinaire ADD COLUMN {col} {defn}")
    db.commit()


@app.route('/api/veterinaire', methods=['GET'])
def get_veto():
    db = get_db()
    _ensure_veto_cols(db)
    rows = db.execute("SELECT * FROM trousse_veterinaire ORDER BY id").fetchall()
    db.close()
    result = []
    for r in rows:
        row = dict(r)
        # Mapper les colonnes de trousse_veterinaire vers les noms attendus
        result.append({
            'id':              row['id'],
            'nom':             row.get('libelle') or '',
            'quantite':        row.get('quantite') or 0,
            'prix_unit':       row.get('prix_unitaire') or 0,
            'montant':         row.get('montant') or 0,
            'date_achat':      row.get('date_achat') or '',
            'date_peremption': row.get('date_peremption') or '',
            'statut':          row.get('statut') or 'OK',
            'categorie':       row.get('categorie') or '',
            'stock_min':       row.get('stock_min') or 0,
            'observations':    row.get('observations') or '',
        })
    return jsonify(result)


@app.route('/api/veterinaire', methods=['POST'])
def add_veto():
    d = request.json
    db = get_db()
    _ensure_veto_cols(db)
    montant = d.get('montant', (d.get('quantite',0) or 0) * (d.get('prix_unit',0) or 0))
    cur = db.execute("""
        INSERT INTO trousse_veterinaire
          (libelle, quantite, prix_unitaire, montant, date_achat, date_peremption, statut,
           categorie, stock_min, observations)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (d.get('nom',''), d.get('quantite',0), d.get('prix_unit',0),
          montant, d.get('date_achat') or None,
          d.get('date_peremption') or None, d.get('statut','OK'),
          d.get('categorie',''), d.get('stock_min',0), d.get('observations','')))
    db.commit()
    new_id = cur.lastrowid; db.close()
    return jsonify({'id': new_id, 'message': 'Article ajouté'}), 201


@app.route('/api/veterinaire/<int:vid>', methods=['PUT'])
def update_veto(vid):
    d = request.json
    montant = d.get('montant', (d.get('quantite',0) or 0) * (d.get('prix_unit',0) or 0))
    db = get_db()
    _ensure_veto_cols(db)
    db.execute("""
        UPDATE trousse_veterinaire
        SET libelle=?, quantite=?, prix_unitaire=?, montant=?,
            date_achat=?, date_peremption=?, statut=?,
            categorie=?, stock_min=?, observations=?
        WHERE id=?
    """, (d.get('nom',''), d.get('quantite',0), d.get('prix_unit',0),
          montant, d.get('date_achat') or None,
          d.get('date_peremption') or None, d.get('statut','OK'),
          d.get('categorie',''), d.get('stock_min',0), d.get('observations',''), vid))
    db.commit(); db.close()
    return jsonify({'message': 'Article mis à jour'})


@app.route('/api/veterinaire/<int:vid>', methods=['DELETE'])
def delete_veto(vid):
    db = get_db()
    db.execute("DELETE FROM trousse_veterinaire WHERE id=?", (vid,))
    db.commit(); db.close()
    return jsonify({'message': 'Supprimé'})


# ── Liste de commande de médicaments (table dédiée, migration auto) ──

def _ensure_veto_cmd(db):
    """Crée la table veto_commandes si absente (style _ensure_veto_cols)."""
    db.execute("""CREATE TABLE IF NOT EXISTS veto_commandes (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        nom  TEXT NOT NULL,
        qte  INTEGER DEFAULT 1,
        note TEXT DEFAULT '')""")
    db.commit()


@app.route('/api/veterinaire/commandes', methods=['GET'])
def get_veto_cmd():
    db = get_db()
    _ensure_veto_cmd(db)
    rows = db.execute("SELECT * FROM veto_commandes ORDER BY id").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/veterinaire/commandes', methods=['POST'])
def add_veto_cmd():
    d = request.json
    db = get_db()
    _ensure_veto_cmd(db)
    cur = db.execute("INSERT INTO veto_commandes (nom, qte, note) VALUES (?,?,?)",
                     (d.get('nom', ''), d.get('qte', 1), d.get('note', '')))
    db.commit()
    new_id = cur.lastrowid; db.close()
    return jsonify({'id': new_id, 'message': 'Ligne de commande ajoutée'}), 201


@app.route('/api/veterinaire/commandes/<int:cid>', methods=['PUT'])
def update_veto_cmd(cid):
    d = request.json
    db = get_db()
    _ensure_veto_cmd(db)
    db.execute("UPDATE veto_commandes SET nom=?, qte=?, note=? WHERE id=?",
               (d.get('nom', ''), d.get('qte', 1), d.get('note', ''), cid))
    db.commit(); db.close()
    return jsonify({'message': 'Ligne de commande mise à jour'})


@app.route('/api/veterinaire/commandes/<int:cid>', methods=['DELETE'])
def delete_veto_cmd(cid):
    db = get_db()
    _ensure_veto_cmd(db)
    db.execute("DELETE FROM veto_commandes WHERE id=?", (cid,))
    db.commit(); db.close()
    return jsonify({'message': 'Ligne de commande supprimée'})


# ═══════════════════════════════════════════════════════════════════════════
# VENTES & ACHATS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/ventes', methods=['GET'])
def get_ventes():
    annee = request.args.get('annee')
    db = get_db()
    # Exclure sivac_data (BLOB binaire) qui empêche la sérialisation JSON
    cols = [c['name'] for c in db.execute('PRAGMA table_info(ventes)').fetchall()
            if c['name'] != 'sivac_data']
    sel = ', '.join(cols)
    if annee:
        rows = db.execute(
            f"SELECT {sel} FROM ventes WHERE annee=? ORDER BY mois", (annee,)
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT {sel} FROM ventes ORDER BY annee DESC, mois"
        ).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/ventes', methods=['POST'])
def add_vente():
    d = request.json or {}
    db = get_db()
    cur = db.execute("""
        INSERT INTO ventes
          (annee, mois, type_acheteur, prix_kg, quantite_kg, prix_brut,
           transport, hebergement, svc_veterinaire, total,
           date_transport, date_abattage, date_paiement,
           acheteur_nom, acheteur_tel, acheteur_adresse,
           nb_porcs, nb_males, nb_femelles, poids_carcasse_json, sivac_nom)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (d.get('annee'), d.get('mois'), d.get('type_acheteur',''),
          d.get('prix_kg', 0), d.get('quantite_kg', 0), d.get('prix_brut', 0),
          d.get('transport', 0), d.get('hebergement', 0),
          d.get('svc_veterinaire', 0),
          d.get('prix_brut', 0) - d.get('transport', 0) - d.get('hebergement', 0) - d.get('svc_veterinaire', 0),
          d.get('date_transport', ''), d.get('date_abattage', ''), d.get('date_paiement', ''),
          d.get('acheteur_nom', ''), d.get('acheteur_tel', ''), d.get('acheteur_adresse', ''),
          d.get('nb_porcs', 0), d.get('nb_males', 0), d.get('nb_femelles', 0),
          d.get('poids_carcasse_json', ''), d.get('sivac_nom', '')))
    db.commit()
    new_id = cur.lastrowid
    db.close()
    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/ventes/<int:vid>', methods=['PUT'])
def update_vente(vid):
    d = request.json or {}
    db = get_db()
    row = db.execute("SELECT * FROM ventes WHERE id=?", (vid,)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    r = dict(row)
    prix_brut  = d.get('prix_brut',   r.get('prix_brut', 0))
    transport  = d.get('transport',   r.get('transport', 0))
    hebergement= d.get('hebergement', r.get('hebergement', 0))
    svc_veto   = d.get('svc_veterinaire', r.get('svc_veterinaire', 0))
    total      = prix_brut - transport - hebergement - svc_veto
    db.execute("""
        UPDATE ventes SET
          annee=?, mois=?, type_acheteur=?, prix_kg=?, quantite_kg=?,
          prix_brut=?, transport=?, hebergement=?, svc_veterinaire=?, total=?,
          date_transport=?, date_abattage=?, date_paiement=?,
          acheteur_nom=?, acheteur_tel=?, acheteur_adresse=?,
          nb_porcs=?, nb_males=?, nb_femelles=?, poids_carcasse_json=?, sivac_nom=?
        WHERE id=?
    """, (d.get('annee', r['annee']), d.get('mois', r['mois']),
          d.get('type_acheteur', r['type_acheteur']),
          d.get('prix_kg', r['prix_kg']), d.get('quantite_kg', r['quantite_kg']),
          prix_brut, transport, hebergement, svc_veto, total,
          d.get('date_transport', r.get('date_transport', '')),
          d.get('date_abattage', r.get('date_abattage', '')),
          d.get('date_paiement', r.get('date_paiement', '')),
          d.get('acheteur_nom', r.get('acheteur_nom', '')),
          d.get('acheteur_tel', r.get('acheteur_tel', '')),
          d.get('acheteur_adresse', r.get('acheteur_adresse', '')),
          d.get('nb_porcs', r.get('nb_porcs', 0)),
          d.get('nb_males', r.get('nb_males', 0)),
          d.get('nb_femelles', r.get('nb_femelles', 0)),
          d.get('poids_carcasse_json', r.get('poids_carcasse_json', '')),
          d.get('sivac_nom', r.get('sivac_nom', '')),
          vid))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/ventes/<int:vid>', methods=['DELETE'])
def delete_vente(vid):
    db = get_db()
    db.execute("DELETE FROM ventes WHERE id=?", (vid,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/ventes/<int:vid>/sivac', methods=['POST'])
def upload_sivac(vid):
    """Upload du PDF SIVAC associé à une vente."""
    if 'file' not in request.files:
        return jsonify({'error': 'Aucun fichier'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Format PDF uniquement'}), 400
    data = f.read()
    nom  = f.filename
    db = get_db()
    db.execute("UPDATE ventes SET sivac_data=?, sivac_nom=? WHERE id=?", (data, nom, vid))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'nom': nom}), 200


@app.route('/api/ventes/<int:vid>/sivac', methods=['GET'])
def download_sivac(vid):
    """Téléchargement du PDF SIVAC d'une vente."""
    db = get_db()
    row = db.execute("SELECT sivac_data, sivac_nom FROM ventes WHERE id=?", (vid,)).fetchone()
    db.close()
    if not row or not row['sivac_data']:
        return jsonify({'error': 'Aucun PDF disponible'}), 404
    from flask import Response
    return Response(
        row['sivac_data'],
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{row["sivac_nom"] or "sivac.pdf"}"'}
    )


# ═══════════════════════════════════════════════════════════════════════════
# SALAIRES
# ═══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════
# EMPLOYES
# ══════════════════════════════════════════════════
@app.route('/api/employes', methods=['GET'])
def get_employes():
    db = get_db()
    rows = db.execute("SELECT * FROM employes ORDER BY nom").fetchall()
    db.close()
    return jsonify(rows_to_list(rows))

@app.route('/api/employes', methods=['POST'])
def add_employe():
    d = request.json
    db = get_db()
    cur = db.execute(
        "INSERT INTO employes (nom,poste,salaire,subsistance,tel,localisation,notes,actif) VALUES (?,?,?,?,?,?,?,?)",
        (d['nom'], d.get('poste',''), d.get('salaire',0), d.get('subsistance',0),
         d.get('tel',''), d.get('localisation',''), d.get('notes',''), 1)
    )
    db.commit(); eid = cur.lastrowid; db.close()
    return jsonify({'id': eid, 'ok': True}), 201

@app.route('/api/employes/<int:eid>', methods=['PUT'])
def update_employe(eid):
    d = request.json
    db = get_db()
    db.execute(
        "UPDATE employes SET nom=?,poste=?,salaire=?,subsistance=?,tel=?,localisation=?,notes=?,actif=? WHERE id=?",
        (d.get('nom',''), d.get('poste',''), d.get('salaire',0), d.get('subsistance',0),
         d.get('tel',''), d.get('localisation',''), d.get('notes',''), d.get('actif',1), eid)
    )
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/employes/<int:eid>', methods=['DELETE'])
def delete_employe(eid):
    db = get_db()
    db.execute("UPDATE employes SET actif=0 WHERE id=?", (eid,))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/salaires/generer-annee', methods=['POST'])
def generer_salaires_annee():
    d = request.json
    annee = int(d.get('annee', 2026))
    db = get_db()
    employes = db.execute("SELECT * FROM employes WHERE actif=1").fetchall()
    # S'assurer que les colonnes annee et mois existent
    cols = [c[1] for c in db.execute("PRAGMA table_info(salaires)").fetchall()]
    created = 0
    for e in employes:
        for mois in range(1, 13):
            chk = "SELECT id FROM salaires WHERE employe=? AND mois=?"
            params = [e["nom"], mois]
            if "annee" in cols:
                chk += " AND annee=?"; params.append(annee)
            exists = db.execute(chk, params).fetchone()
            if not exists:
                tot = (e["salaire"] or 0) + (e["subsistance"] or 0)
                if "annee" in cols:
                    db.execute(
                        "INSERT INTO salaires (employe,poste,annee,mois,salaire,frais_subsistance,total,statut,observations) VALUES (?,?,?,?,?,?,?,?,?)",
                        (e["nom"], e["poste"], annee, mois, e["salaire"] or 0, e["subsistance"] or 0, tot, "En attente", "")
                    )
                else:
                    db.execute(
                        "INSERT INTO salaires (employe,poste,mois,salaire,frais_subsistance,total,statut,observations) VALUES (?,?,?,?,?,?,?,?)",
                        (e["nom"], e["poste"], mois, e["salaire"] or 0, e["subsistance"] or 0, tot, "En attente", "")
                    )
                created += 1
    db.commit(); db.close()
    return jsonify({'ok': True, 'created': created})


# ══════════════════════════════════════════════════
# ACHETEURS EN GROS
# ══════════════════════════════════════════════════
@app.route('/api/acheteurs', methods=['GET'])
def get_acheteurs():
    db = get_db()
    rows = db.execute("SELECT * FROM acheteurs ORDER BY nom").fetchall()
    db.close()
    return jsonify(rows_to_list(rows))

@app.route('/api/acheteurs', methods=['POST'])
def add_acheteur():
    d = request.json; db = get_db()
    cur = db.execute(
        "INSERT INTO acheteurs (nom,type_acheteur,telephone,adresse,email,notes) VALUES (?,?,?,?,?,?)",
        (d['nom'],d.get('type_acheteur','Acheteur local'),d.get('telephone',''),
         d.get('adresse',''),d.get('email',''),d.get('notes',''))
    )
    db.commit(); eid=cur.lastrowid; db.close()
    return jsonify({'id':eid,'ok':True}), 201

@app.route('/api/acheteurs/<int:aid>', methods=['PUT'])
def update_acheteur(aid):
    d = request.json; db = get_db()
    db.execute(
        "UPDATE acheteurs SET nom=?,type_acheteur=?,telephone=?,adresse=?,email=?,notes=? WHERE id=?",
        (d['nom'],d.get('type_acheteur',''),d.get('telephone',''),
         d.get('adresse',''),d.get('email',''),d.get('notes',''),aid)
    )
    db.commit(); db.close()
    return jsonify({'ok':True})

@app.route('/api/acheteurs/<int:aid>', methods=['DELETE'])
def delete_acheteur(aid):
    db = get_db()
    db.execute("DELETE FROM acheteurs WHERE id=?", (aid,))
    db.commit(); db.close()
    return jsonify({'ok':True})

# ══════════════════════════════════════════════════
# CLIENTS PRÉ-COMMANDE
# ══════════════════════════════════════════════════
@app.route('/api/clients-commande', methods=['GET'])
def get_clients_commande():
    db = get_db()
    rows = db.execute("SELECT * FROM clients_commande ORDER BY nom").fetchall()
    db.close()
    return jsonify(rows_to_list(rows))

@app.route('/api/clients-commande', methods=['POST'])
def add_client_commande():
    d = request.json; db = get_db()
    cur = db.execute(
        "INSERT INTO clients_commande (nom,telephone,adresse,email,notes) VALUES (?,?,?,?,?)",
        (d['nom'],d.get('telephone',''),d.get('adresse',''),d.get('email',''),d.get('notes',''))
    )
    db.commit(); eid=cur.lastrowid; db.close()
    return jsonify({'id':eid,'ok':True}), 201

@app.route('/api/clients-commande/<int:cid>', methods=['PUT'])
def update_client_commande(cid):
    d = request.json; db = get_db()
    db.execute(
        "UPDATE clients_commande SET nom=?,telephone=?,adresse=?,email=?,notes=? WHERE id=?",
        (d['nom'],d.get('telephone',''),d.get('adresse',''),d.get('email',''),d.get('notes',''),cid)
    )
    db.commit(); db.close()
    return jsonify({'ok':True})

@app.route('/api/clients-commande/<int:cid>', methods=['DELETE'])
def delete_client_commande(cid):
    db = get_db()
    db.execute("DELETE FROM clients_commande WHERE id=?", (cid,))
    db.commit(); db.close()
    return jsonify({'ok':True})

@app.route('/api/salaires', methods=['GET'])
def get_salaires():
    db = get_db()
    rows = db.execute("SELECT * FROM salaires ORDER BY mois DESC, employe").fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/salaires', methods=['POST'])
def add_salaire():
    d = request.json
    db = get_db()
    sal   = d.get('salaire', d.get('salaire_base', 0)) or 0
    sub   = d.get('frais_subsistance', 0) or 0
    total = d.get('total', sal + sub)
    statut = d.get('statut', d.get('statut_paiement', 'En attente'))
    cur = db.execute("""
        INSERT INTO salaires
          (annee, mois, poste, employe, salaire, frais_subsistance, total,
           statut, statut_paiement, mode_paiement, observations)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        d.get('annee'), str(d.get('mois', '')), d.get('poste', ''), d.get('employe', ''),
        sal, sub, total,
        statut, d.get('statut_paiement', statut),
        d.get('mode_paiement', 'Espèces'), d.get('observations', '')
    ))
    db.commit()
    new_id = cur.lastrowid; db.close()
    return jsonify({'id': new_id, 'message': 'Salaire enregistré'}), 201


@app.route('/api/salaires/<int:sid>', methods=['PUT'])
def update_salaire(sid):
    d = request.json
    db = get_db()
    # Colonnes réelles de la table salaires
    cols = [r[1] for r in db.execute("PRAGMA table_info(salaires)").fetchall()]
    # Mise à jour complète si les colonnes existent
    if 'employe' in cols and 'salaire' in cols:
        db.execute("""
            UPDATE salaires SET employe=?, poste=?, salaire=?,
              frais_subsistance=?, total=?, statut=?
            WHERE id=?
        """, (
            d.get('employe'), d.get('poste'),
            d.get('salaire', 0), d.get('frais_subsistance', 0),
            d.get('total', 0),
            d.get('statut_paiement', d.get('statut', 'Payé')),
            sid
        ))
    else:
        # Fallback colonnes anciennes
        db.execute("UPDATE salaires SET statut_paiement=? WHERE id=?",
                   (d.get('statut_paiement','Payé'), sid))
    db.commit(); db.close()
    return jsonify({'message': 'Salaire mis à jour'})


@app.route('/api/salaires/<int:sid>', methods=['DELETE'])
def delete_salaire(sid):
    db = get_db()
    db.execute("DELETE FROM salaires WHERE id=?", (sid,))
    db.commit(); db.close()
    return jsonify({'message': 'Salaire supprimé'})


# ═══════════════════════════════════════════════════════════════════════════
# TABLEAU DE BORD — statistiques
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/dashboard')
def dashboard():
    db = get_db()
    truies = rows_to_list(db.execute("SELECT * FROM truies").fetchall())
    verrats = rows_to_list(db.execute("SELECT * FROM verrats").fetchall())
    portees = rows_to_list(db.execute(
        "SELECT * FROM portees WHERE code_statut != 'HIDE'"
    ).fetchall())
    porcelets = rows_to_list(db.execute("SELECT * FROM porcelets").fetchall())
    veto = rows_to_list(db.execute("SELECT * FROM veterinaire").fetchall())

    # Calculs statistiques
    stats = {
        'truies': {
            'total': len(truies),
            'gestantes':   sum(1 for t in truies if t['statut']=='Gestante'),
            'allaitantes': sum(1 for t in truies if t['statut']=='Allaitante'),
            'sevrees':     sum(1 for t in truies if t['statut']=='Sevrée'),
            'reformees':   sum(1 for t in truies if t['reformer']),
            'non_saillies':sum(1 for t in truies if t['statut']=='Non saillée'),
        },
        'verrats': {
            'total': len(verrats),
            'actifs': sum(1 for v in verrats if v['statut']=='Actif'),
        },
        'portees': {
            'total': len(portees),
            'total_sevres': sum(p.get('sevres_m',0)+p.get('sevres_f',0) for p in portees),
            'total_nv':     sum(p.get('nes_vivants',0) for p in portees),
        },
        'veto': {
            'total_articles': len(veto),
            'perimes': sum(1 for v in veto if v['statut']=='Périmé'),
            'valeur_stock': sum(v.get('montant',0) for v in veto),
        }
    }
    db.close()
    return jsonify(stats)


# ═══════════════════════════════════════════════════════════════════════════
# SUIVI REPRODUCTEURS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/suivi-reproducteurs')
def suivi_reproducteurs():
    """Retourne les données structurées pour le tableau Suivi reproducteurs."""
    db   = get_db()
    today = datetime.now().date()

    truies  = rows_to_list(db.execute("SELECT * FROM truies  ORDER BY numero").fetchall())
    verrats = rows_to_list(db.execute("SELECT * FROM verrats ORDER BY numero").fetchall())
    portees = rows_to_list(db.execute(
        "SELECT p.*, t.numero as truie_num FROM portees p "
        "JOIN truies t ON t.id=p.truie_id "
        "WHERE p.code_statut != 'HIDE' ORDER BY t.numero, p.rang_portee"
    ).fetchall())

    # ── Dernière portée par truie ─────────────────────────────────────────
    last_portee = {}
    for p in portees:
        tid = p['truie_id']
        if tid not in last_portee or p['rang_portee'] > last_portee[tid]['rang_portee']:
            last_portee[tid] = p

    def days_from_today(d_str):
        if not d_str:
            return None
        try:
            d = datetime.strptime(d_str[:10], '%Y-%m-%d').date()
            return (d - today).days
        except:
            return None

    def fmt_date(d_str):
        if not d_str:
            return '—'
        try:
            return datetime.strptime(d_str[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
        except:
            return d_str

    # ── Non saillies / Sevrées ────────────────────────────────────────────
    non_saillies = []
    for t in truies:
        if t['statut'] not in ('Non saillée', 'Sevrée') or t['reformer']:
            continue
        lp = last_portee.get(t['id'])
        chaleur_prevue = None
        jours = None
        if lp and lp.get('proch_saillie_prevue'):
            chaleur_prevue = lp['proch_saillie_prevue']
            jours = days_from_today(chaleur_prevue)
        non_saillies.append({
            'numero':         t['numero'],
            'race':           t['race'],
            'statut':         t['statut'],
            'dernier_sevrage': fmt_date(lp['date_sevrage'] if lp else None),
            'derniere_mb':    fmt_date(lp['date_mb'] if lp else None),
            'chaleur_prevue': fmt_date(chaleur_prevue),
            'jours_restants': jours,
        })

    # ── Gestantes ─────────────────────────────────────────────────────────
    gestantes = []
    for t in truies:
        if t['statut'] != 'Gestante' or t['reformer']:
            continue
        lp = last_portee.get(t['id'])
        mb_prevue = lp['date_mb_prevue'] if lp else None
        jours = days_from_today(mb_prevue)
        gestantes.append({
            'numero':      t['numero'],
            'race':        t['race'],
            'verrat':      lp['verrat_nom'] if lp else '—',
            'saillie_1':   fmt_date(lp['date_saillie_1'] if lp else None),
            'saillie_2':   fmt_date(lp['date_saillie_2'] if lp else None),
            'saillie_3':   fmt_date(lp['date_saillie_3'] if lp else None),
            'mb_prevue':   fmt_date(mb_prevue),
            'jours_restants': jours,
        })
    gestantes.sort(key=lambda x: x['jours_restants'] if x['jours_restants'] is not None else 9999)

    # ── Allaitantes ───────────────────────────────────────────────────────
    allaitantes = []
    for t in truies:
        if t['statut'] != 'Allaitante' or t['reformer']:
            continue
        lp = last_portee.get(t['id'])
        svr_prevu = lp['date_sevrage_prevue'] if lp else None
        jours = days_from_today(svr_prevu)
        nes = (lp['nes_vivants'] or 0) if lp else 0
        males = (lp['males'] or 0) if lp else 0
        femelles = (lp['femelles'] or 0) if lp else 0
        allaitantes.append({
            'numero':        t['numero'],
            'race':          t['race'],
            'verrat':        lp['verrat_nom'] if lp else '—',
            'date_mb':       fmt_date(lp['date_mb'] if lp else None),
            'nes_vivants':   nes,
            'males':         males,
            'femelles':      femelles,
            'sevrage_prevu': fmt_date(svr_prevu),
            'jours_restants': jours,
        })
    allaitantes.sort(key=lambda x: x['jours_restants'] if x['jours_restants'] is not None else 9999)

    # ── Réformés / À réformer ─────────────────────────────────────────────
    reformes = []
    # Truies réformées
    for t in truies:
        if not t['reformer']:
            continue
        lp = last_portee.get(t['id'])
        total_sevres = sum(
            (p.get('sevres_m', 0) or 0) + (p.get('sevres_f', 0) or 0)
            for p in portees if p['truie_id'] == t['id']
        )
        reformes.append({
            'type':           'Truie',
            'numero':         t['numero'],
            'race':           t['race'],
            'total_sevres':   total_sevres,
            'derniere_mb':    fmt_date(lp['date_mb'] if lp else None),
            'observations':   t.get('observations', '') or '',
        })
    # Verrats réformés
    for v in verrats:
        if not v['reformer']:
            continue
        reformes.append({
            'type':           'Verrat',
            'numero':         v['numero'],
            'race':           v['race'],
            'total_sevres':   '—',
            'derniere_mb':    '—',
            'observations':   v.get('observations', '') or '',
        })

    db.close()
    return jsonify({
        'non_saillies': non_saillies,
        'gestantes':    gestantes,
        'allaitantes':  allaitantes,
        'reformes':     reformes,
        'counts': {
            'non_saillies': len(non_saillies),
            'gestantes':    len(gestantes),
            'allaitantes':  len(allaitantes),
            'reformes':     len(reformes),
        }
    })


# ═══════════════════════════════════════════════════════════════════════════
# HISTORIQUE VENTES (table ventes — données par année/mois)
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/ventes-historique', methods=['GET'])
def get_ventes_historique():
    annee = request.args.get('annee')
    db = get_db()
    if annee:
        rows = db.execute(
            "SELECT * FROM ventes WHERE annee=? ORDER BY mois", (annee,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM ventes ORDER BY annee, mois"
        ).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/salaires-historique', methods=['GET'])
def get_salaires_historique():
    annee = request.args.get('annee')
    db = get_db()
    if annee:
        rows = db.execute(
            "SELECT * FROM salaires WHERE annee=? ORDER BY mois, employe", (annee,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM salaires ORDER BY annee, mois, employe"
        ).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))




# ═══════════════════════════════════════════════════════════════════════════
# COMMANDES (ventes au détail par client)
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/commandes', methods=['GET'])
def get_commandes():
    annee = request.args.get('annee')
    db = get_db()
    if annee:
        rows = db.execute(
            "SELECT * FROM commandes WHERE annee=? ORDER BY mois, client", (annee,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM commandes ORDER BY annee DESC, mois, client"
        ).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/commandes', methods=['POST'])
def add_commande():
    d = request.json
    db = get_db()
    cur = db.execute(
        """INSERT INTO commandes (annee, mois, client, quantite_kg, prix_kg, montant, statut,
                                  note_raw, telephone, adresse, details_morceaux, montant_note, observations)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d['annee'], d['mois'], d['client'], d.get('quantite_kg', 0),
         d.get('prix_kg', 0), d.get('montant', 0), d.get('statut', 'Livre'),
         d.get('note_raw',''), d.get('telephone',''), d.get('adresse',''),
         d.get('details_morceaux',''), d.get('montant_note',''), d.get('observations',''))
    )
    db.commit()
    db.close()
    return jsonify({'id': cur.lastrowid, 'message': 'Commande ajoutee'}), 201


@app.route('/api/commandes/<int:cid>', methods=['PUT'])
def update_commande(cid):
    d = request.json or {}
    db = get_db()
    # Lire l'enregistrement existant pour ne pas écraser les champs non fournis
    row = db.execute("SELECT * FROM commandes WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    r = dict(row)
    db.execute(
        """UPDATE commandes SET client=?, annee=?, mois=?, quantite_kg=?, prix_kg=?, montant=?, statut=?,
           statut_paiement=?, mode_paiement=?, telephone=?, adresse=?, details_morceaux=?,
           montant_note=?, note_raw=?, observations=?
           WHERE id=?""",
        (d.get('client',          r.get('client','')),
         d.get('annee',           r.get('annee', 0)),
         d.get('mois',            r.get('mois', 0)),
         d.get('quantite_kg',     r.get('quantite_kg', 0)),
         d.get('prix_kg',         r.get('prix_kg', 0)),
         d.get('montant',         r.get('montant', 0)),
         d.get('statut',          r.get('statut', 'En attente de livraison')),
         d.get('statut_paiement', r.get('statut_paiement', 'En attente de paiement')),
         d.get('mode_paiement',   r.get('mode_paiement', 'Espèces')),
         d.get('telephone',       r.get('telephone', '')),
         d.get('adresse',         r.get('adresse', '')),
         d.get('details_morceaux',r.get('details_morceaux', '')),
         d.get('montant_note',    r.get('montant_note', '')),
         d.get('note_raw',        r.get('note_raw', '')),
         d.get('observations',    r.get('observations', '')),
         cid)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/commandes/<int:cid>', methods=['DELETE'])
def delete_commande(cid):
    db = get_db()
    db.execute("DELETE FROM commandes WHERE id=?", (cid,))
    db.commit()
    db.close()
    return jsonify({'message': 'Commande supprimee'})


# ═══════════════════════════════════════════════════════════════════════════
# KPI TRUIES & VERRATS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/kpi/truies-verrats', methods=['GET'])
def kpi_truies_verrats():
    """
    Calcule les KPI de production par truie et en global :
      - Nés vivants (total, mâles, femelles)
      - Morts nés (total, mâles via morts_nes proportionnel, femelles)
      - Morts avant sevrage (total, mâles, femelles)
      - Morts après sevrage (total, mâles, femelles)
      - Sevrés (total, mâles, femelles)
    """
    db = get_db()

    # ── KPI globaux ──────────────────────────────────────────────────────────
    row = db.execute("""
        SELECT
            SUM(c.nais_vivants)                        AS nv_total,
            SUM(c.sexe_m)                              AS nv_m,
            SUM(c.sexe_f)                              AS nv_f,
            SUM(c.morts_nes)                           AS mn_total,
            SUM(c.mort_pre_m)                          AS mort_pre_m,
            SUM(c.mort_pre_f)                          AS mort_pre_f,
            SUM(COALESCE(c.mort_post_m,0))             AS mort_post_m,
            SUM(COALESCE(c.mort_post_f,0))             AS mort_post_f,
            SUM(c.sevres_m + c.sevres_f)               AS svr_total,
            SUM(c.sevres_m)                            AS svr_m_calc,
            SUM(c.sevres_f)                            AS svr_f_calc
        FROM cycles c
        JOIN animaux a ON a.id = c.animal_id
        WHERE a.type_animal = 'truie'
          AND c.code NOT IN ('HIDE')
          AND (c.nais_vivants > 0 OR c.morts_nes > 0)
    """).fetchone()

    def safe(v):
        return int(v) if v is not None else 0

    nv_total   = safe(row['nv_total'])
    nv_m       = safe(row['nv_m'])
    nv_f       = safe(row['nv_f'])
    mn_total   = safe(row['mn_total'])
    # Les morts nés ne sont pas sexés dans la DB — on répartit proportionnellement
    mn_m = round(mn_total * nv_m / nv_total) if nv_total > 0 else 0
    mn_f = mn_total - mn_m

    mort_pre_m   = safe(row['mort_pre_m'])
    mort_pre_f   = safe(row['mort_pre_f'])
    mort_pre_tot = mort_pre_m + mort_pre_f

    mort_post_m   = safe(row['mort_post_m'])
    mort_post_f   = safe(row['mort_post_f'])
    mort_post_tot = mort_post_m + mort_post_f

    svr_total = safe(row['svr_total'])
    svr_m     = max(0, safe(row['svr_m_calc']))
    svr_f     = max(0, safe(row['svr_f_calc']))

    global_kpi = {
        'nes_vivants':        {'total': nv_total,      'm': nv_m,         'f': nv_f},
        'morts_nes':          {'total': mn_total,      'm': mn_m,         'f': mn_f},
        'morts_avant_sevrage':{'total': mort_pre_tot,  'm': mort_pre_m,   'f': mort_pre_f},
        'morts_apres_sevrage':{'total': mort_post_tot, 'm': mort_post_m,  'f': mort_post_f},
        'sevres':             {'total': svr_total,     'm': svr_m,        'f': svr_f},
    }

    # ── KPI par truie ────────────────────────────────────────────────────────
    rows_t = db.execute("""
        SELECT
            trim(substr(a.feuille,7))              AS truie_num,
            SUM(c.nais_vivants)                    AS nv_total,
            SUM(c.sexe_m)                          AS nv_m,
            SUM(c.sexe_f)                          AS nv_f,
            SUM(c.morts_nes)                       AS mn_total,
            SUM(c.mort_pre_m)                      AS mort_pre_m,
            SUM(c.mort_pre_f)                      AS mort_pre_f,
            SUM(COALESCE(c.mort_post_m,0))         AS mort_post_m,
            SUM(COALESCE(c.mort_post_f,0))         AS mort_post_f,
            SUM(c.sevres_m + c.sevres_f)           AS svr_total,
            SUM(c.sevres_m)                        AS svr_m_calc,
            SUM(c.sevres_f)                        AS svr_f_calc
        FROM cycles c
        JOIN animaux a ON a.id = c.animal_id
        WHERE a.type_animal = 'truie'
          AND c.code NOT IN ('HIDE')
          AND (c.nais_vivants > 0 OR c.morts_nes > 0)
        GROUP BY a.feuille
        ORDER BY CAST(trim(substr(a.feuille,7)) AS INTEGER)
    """).fetchall()

    par_truie = []
    for r in rows_t:
        t_nv    = safe(r['nv_total'])
        t_nv_m  = safe(r['nv_m'])
        t_nv_f  = safe(r['nv_f'])
        t_mn    = safe(r['mn_total'])
        t_mn_m  = round(t_mn * t_nv_m / t_nv) if t_nv > 0 else 0
        t_mn_f  = t_mn - t_mn_m
        t_pm    = safe(r['mort_pre_m'])
        t_pf    = safe(r['mort_pre_f'])
        t_qm    = safe(r['mort_post_m'])
        t_qf    = safe(r['mort_post_f'])
        t_svr   = safe(r['svr_total'])
        t_svm   = max(0, safe(r['svr_m_calc']))
        t_svf   = max(0, safe(r['svr_f_calc']))
        par_truie.append({
            'truie': r['truie_num'],
            'nes_vivants':        {'total': t_nv,        'm': t_nv_m, 'f': t_nv_f},
            'morts_nes':          {'total': t_mn,        'm': t_mn_m, 'f': t_mn_f},
            'morts_avant_sevrage':{'total': t_pm + t_pf, 'm': t_pm,   'f': t_pf},
            'morts_apres_sevrage':{'total': t_qm + t_qf, 'm': t_qm,   'f': t_qf},
            'sevres':             {'total': t_svr,        'm': t_svm,  'f': t_svf},
        })

    db.close()
    return jsonify({'global': global_kpi, 'par_truie': par_truie})


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT / BACKUP
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/export/db')
def export_db():
    """Télécharge une copie de la base de données."""
    return send_file(DB, as_attachment=True,
                     download_name=f"cochette_{datetime.now().strftime('%Y%m%d')}.db")


@app.route('/api/export/json')
def export_json():
    """Exporte toutes les données en JSON."""
    db = get_db()
    data = {
        'truies':      rows_to_list(db.execute("SELECT * FROM truies").fetchall()),
        'verrats':     rows_to_list(db.execute("SELECT * FROM verrats").fetchall()),
        'portees':     rows_to_list(db.execute("SELECT * FROM portees").fetchall()),
        'porcelets':   rows_to_list(db.execute("SELECT * FROM porcelets").fetchall()),
        'veterinaire': rows_to_list(db.execute("SELECT * FROM veterinaire").fetchall()),
        'ventes':      rows_to_list(db.execute("SELECT * FROM ventes_achats").fetchall()),
        'salaires':    rows_to_list(db.execute("SELECT * FROM salaires").fetchall()),
        'exported_at': datetime.now().isoformat(),
    }
    db.close()
    return jsonify(data)


# ═══════════════════════════════════════════════════════════════════════════
# LANCEMENT
# ═══════════════════════════════════════════════════════════════════════════

def open_browser():
    import time; time.sleep(1.2)
    webbrowser.open('http://127.0.0.1:5050')




# ══════════════════════════════════════════════════
# CONGÉLATEUR — stock des coupes
# ══════════════════════════════════════════════════
@app.route('/api/congelateur', methods=['GET'])
def get_congelateur():
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS stock_congelateur (
        id INTEGER PRIMARY KEY AUTOINCREMENT, casier INTEGER, coupe TEXT,
        poids REAL, prix_unitaire INTEGER, statut TEXT DEFAULT "Disponible",
        date_abattage TEXT, date_vente TEXT, client TEXT DEFAULT "",
        prix_vente INTEGER DEFAULT 0, observations TEXT DEFAULT "",
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    rows = db.execute("""SELECT id,casier,coupe,poids,prix_unitaire,statut,
        date_abattage,date_vente,client,prix_vente,observations
        FROM stock_congelateur ORDER BY casier,coupe,poids""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/abattages-count', methods=['GET'])
def abattages_count():
    """Nombre d'animaux abattus pour la vente en détail = combinaisons distinctes casier+date_abattage."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(DISTINCT IFNULL(casier,'')||'|'||IFNULL(date_abattage,'')) AS n "
            "FROM stock_congelateur WHERE casier IS NOT NULL"
        ).fetchone()
        n = row['n'] if row else 0
    except Exception:
        n = 0
    db.close()
    return jsonify({'animaux_abattus': n})


# ══════════════════════════════════════════════════════════════════════════
# STOCK & COMMANDES PREMIX / SUPPLÉMENTS
# ══════════════════════════════════════════════════════════════════════════
@app.route('/api/premix-stock', methods=['GET'])
def get_premix_stock():
    db = get_db()
    groupe = request.args.get('groupe')
    if groupe:
        rows = db.execute("SELECT * FROM premix_stock WHERE groupe=? ORDER BY categorie,nom", (groupe,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM premix_stock ORDER BY groupe,categorie,nom").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/premix-stock', methods=['POST'])
def add_premix_stock():
    d = request.json; db = get_db()
    cur = db.execute("""INSERT INTO premix_stock (groupe,categorie,nom,taille_sac_kg,stock_kg,seuil_kg,prix_sac,capacite_kg)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get('groupe','NUTRIKA'), d.get('categorie','premix'), d.get('nom',''),
         d.get('taille_sac_kg',25), d.get('stock_kg',0), d.get('seuil_kg',0), d.get('prix_sac',0), d.get('capacite_kg',0)))
    db.commit(); nid = cur.lastrowid; db.close()
    return jsonify({'id': nid, 'ok': True})

@app.route('/api/premix-stock/reset', methods=['POST'])
def reset_premix_stock():
    """Remet à 0 le stock de tous les articles d'un groupe (ou de tous si non précisé)."""
    d = request.json or {}
    groupe = d.get('groupe')
    db = get_db()
    if groupe:
        db.execute("UPDATE premix_stock SET stock_kg=0 WHERE groupe=?", (groupe,))
    else:
        db.execute("UPDATE premix_stock SET stock_kg=0")
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/premix-stock/<int:sid>', methods=['PUT'])
def update_premix_stock(sid):
    d = request.json; db = get_db()
    db.execute("""UPDATE premix_stock SET groupe=?,categorie=?,nom=?,taille_sac_kg=?,stock_kg=?,seuil_kg=?,prix_sac=?,
        capacite_kg=COALESCE(?, capacite_kg) WHERE id=?""",
        (d.get('groupe','NUTRIKA'), d.get('categorie','premix'), d.get('nom',''),
         d.get('taille_sac_kg',25), d.get('stock_kg',0), d.get('seuil_kg',0), d.get('prix_sac',0),
         d.get('capacite_kg', None), sid))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/premix-stock/<int:sid>', methods=['DELETE'])
def delete_premix_stock(sid):
    db = get_db(); db.execute("DELETE FROM premix_stock WHERE id=?", (sid,)); db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/premix-commandes', methods=['GET'])
def get_premix_commandes():
    db = get_db()
    rows = db.execute("SELECT * FROM premix_commandes ORDER BY date_commande DESC, id DESC").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/premix-commandes', methods=['POST'])
def add_premix_commande():
    """Crée un bon de commande. Lot 2 : N'ajoute PLUS au stock à la création.
    L'entrée en stock se fait uniquement au passage à 'Livré' (cf. PUT)."""
    import json as _json
    d = request.json; db = get_db()
    lignes = d.get('lignes', [])   # [{id, nom, categorie, sacs, taille_sac_kg, prix_sac}]
    total_kg = 0; total_sacs = 0; total_montant = 0
    for l in lignes:
        sacs = float(l.get('sacs', 0) or 0)
        taille = float(l.get('taille_sac_kg', 25) or 25)
        prix = float(l.get('prix_sac', 0) or 0)
        kg = sacs * taille
        total_kg += kg; total_sacs += sacs; total_montant += sacs * prix
    frais_liv = float(d.get('frais_livraison', 0) or 0)
    statut_liv = d.get('statut_livraison', 'En attente')
    cur = db.execute("""INSERT INTO premix_commandes
        (date_commande,groupe,fournisseur,lignes_json,total_kg,total_sacs,total_montant,statut,
         mode_paiement,versements_json,statut_paiement,frais_livraison,lieu_livraison,date_livraison,statut_livraison,stock_applique)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('date_commande'), d.get('groupe','NUTRIKA'), d.get('fournisseur',''),
         _json.dumps(lignes, ensure_ascii=False), total_kg, int(total_sacs), total_montant, 'Commandé',
         d.get('mode_paiement','Espèces'), '[]', 'Non payé',
         frais_liv, d.get('lieu_livraison',''), d.get('date_livraison',''), statut_liv, 0))
    nid = cur.lastrowid
    # Si la commande est créée directement en 'Livré', on applique le stock tout de suite.
    if statut_liv == 'Livré':
        _appliquer_stock_commande(db, lignes, +1)
        db.execute("UPDATE premix_commandes SET stock_applique=1 WHERE id=?", (nid,))
    db.commit(); db.close()
    return jsonify({'id': nid, 'ok': True, 'total_kg': total_kg, 'total_sacs': int(total_sacs), 'total_montant': total_montant})


def _appliquer_stock_commande(db, lignes, sens):
    """Applique (+1) ou reverse (-1) les quantités d'une commande sur les stocks.
    - Tous les articles → premix_stock (match par id de ligne).
    - Les lignes de catégorie 'granule' → aussi aliments_stock (cle 'granule'), cf. lot 3.3.
    """
    for l in lignes:
        sacs = float(l.get('sacs', 0) or 0)
        taille = float(l.get('taille_sac_kg', 25) or 25)
        kg = sacs * taille * sens
        if l.get('id'):
            if sens > 0:
                db.execute("UPDATE premix_stock SET stock_kg = stock_kg + ? WHERE id=?", (kg, l['id']))
            else:
                db.execute("UPDATE premix_stock SET stock_kg = MAX(0, stock_kg + ?) WHERE id=?", (kg, l['id']))
        if (l.get('categorie') or '') == 'granule':
            if sens > 0:
                db.execute("UPDATE aliments_stock SET stock_kg = stock_kg + ? WHERE cle='granule'", (kg,))
            else:
                db.execute("UPDATE aliments_stock SET stock_kg = MAX(0, stock_kg + ?) WHERE cle='granule'", (kg,))

@app.route('/api/premix-commandes/<int:cid>', methods=['PUT'])
def update_premix_commande(cid):
    """Met à jour paiement / livraison d'une commande (sans toucher au stock)."""
    import json as _json
    d = request.json; db = get_db()
    # État courant (pour gérer la transition de livraison de façon idempotente)
    row = db.execute("SELECT total_montant, frais_livraison, lignes_json, statut_livraison, stock_applique FROM premix_commandes WHERE id=?", (cid,)).fetchone()
    if not row:
        db.close(); return jsonify({'ok': False, 'error': 'introuvable'}), 404
    total_du = (row['total_montant'] or 0) + float(d.get('frais_livraison', row['frais_livraison'] or 0) or 0)
    versements = d.get('versements', None)
    statut_paiement = d.get('statut_paiement', None)
    if versements is not None:
        paye = sum(float(v.get('montant', 0) or 0) for v in versements)
        if paye <= 0: statut_paiement = 'Non payé'
        elif paye + 0.001 < total_du: statut_paiement = 'Partiel'
        else: statut_paiement = 'Payé'

    # --- Lot 2 : transition de statut_livraison ↔ stock (idempotent) ---
    new_liv = d.get('statut_livraison', None)
    applique = int(row['stock_applique'] or 0)
    if new_liv is not None:
        try: lignes = _json.loads(row['lignes_json'] or '[]')
        except Exception: lignes = []
        if new_liv == 'Livré' and applique == 0:
            _appliquer_stock_commande(db, lignes, +1)
            applique = 1
        elif new_liv != 'Livré' and applique == 1:
            _appliquer_stock_commande(db, lignes, -1)
            applique = 0

    sets = []; vals = []
    fields = {
        'mode_paiement': d.get('mode_paiement'),
        'statut_paiement': statut_paiement,
        'frais_livraison': d.get('frais_livraison'),
        'lieu_livraison': d.get('lieu_livraison'),
        'date_livraison': d.get('date_livraison'),
        'statut_livraison': new_liv,
        'fournisseur': d.get('fournisseur'),
    }
    for k, v in fields.items():
        if v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if new_liv is not None:
        sets.append("stock_applique=?"); vals.append(applique)
    if versements is not None:
        sets.append("versements_json=?"); vals.append(_json.dumps(versements, ensure_ascii=False))
    if sets:
        vals.append(cid)
        db.execute("UPDATE premix_commandes SET " + ",".join(sets) + " WHERE id=?", vals)
    db.commit()
    db.close()
    return jsonify({'ok': True, 'statut_paiement': statut_paiement})

@app.route('/api/premix-commandes/<int:cid>', methods=['DELETE'])
def delete_premix_commande(cid):
    """Annule une commande. Lot 2 : ne retire du stock QUE si le stock avait été appliqué."""
    import json as _json
    db = get_db()
    row = db.execute("SELECT lignes_json, stock_applique FROM premix_commandes WHERE id=?", (cid,)).fetchone()
    if row and int(row['stock_applique'] or 0) == 1:
        try:
            lignes = _json.loads(row['lignes_json'] or '[]')
            _appliquer_stock_commande(db, lignes, -1)
        except Exception:
            pass
    db.execute("DELETE FROM premix_commandes WHERE id=?", (cid,))
    db.commit(); db.close()
    return jsonify({'ok': True})


# ============================================================
#  Lot 3 — Stock d'aliments finis disponibles
# ============================================================
def _aliment_kgjour(db, cle):
    """Conso/jour recalculée depuis les paramètres courants.
    - demarrage/allaitante/finition : formule_kgjour[cle] × formule_nbsujets[cle]
    - granule : granule_kgjour_par_porcelet × population_base.sous_mere
    """
    import json as _json
    def _param(k, default):
        r = db.execute("SELECT valeur FROM parametres WHERE cle=?", (k,)).fetchone()
        return r['valeur'] if r else default
    if cle in ('demarrage', 'allaitante', 'finition'):
        try: kgj = _json.loads(_param('formule_kgjour', '{}'))
        except Exception: kgj = {}
        try: nb = _json.loads(_param('formule_nbsujets', '{}'))
        except Exception: nb = {}
        return float(kgj.get(cle, 0) or 0) * float(nb.get(cle, 0) or 0)
    if cle == 'granule':
        try: pk = float(_param('granule_kgjour_par_porcelet', '0.25') or 0)
        except Exception: pk = 0.25
        try: pop = _json.loads(_param('population_base', '{}'))
        except Exception: pop = {}
        return pk * float(pop.get('sous_mere', 0) or 0)
    return 0.0


@app.route('/api/aliments-stock', methods=['GET'])
def get_aliments_stock():
    """Renvoie les aliments disponibles, avec rattrapage paresseux du décrément quotidien."""
    db = get_db()
    today = datetime.now().date()
    rows = db.execute("SELECT * FROM aliments_stock ORDER BY id").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        kgjour = _aliment_kgjour(db, item['cle'])
        dm = item.get('date_maj') or ''
        try:
            last = datetime.strptime(dm, '%Y-%m-%d').date()
            n = (today - last).days
        except Exception:
            n = 0
        if n > 0 and kgjour > 0:
            item['stock_kg'] = max(0.0, (item['stock_kg'] or 0) - n * kgjour)
        if n > 0:
            db.execute("UPDATE aliments_stock SET stock_kg=?, date_maj=? WHERE id=?",
                       (item['stock_kg'], today.isoformat(), item['id']))
        item['kgjour'] = round(kgjour, 2)
        item['autonomie_j'] = round((item['stock_kg'] / kgjour), 1) if kgjour > 0 else None
        out.append(item)
    db.commit(); db.close()
    return jsonify(out)


@app.route('/api/aliments-stock/<int:aid>/reception', methods=['POST'])
def reception_aliment(aid):
    """Incrément manuel (réception/production). Body: {kg} ou {sacs} (×taille_sac_kg)."""
    d = request.json or {}; db = get_db()
    row = db.execute("SELECT taille_sac_kg FROM aliments_stock WHERE id=?", (aid,)).fetchone()
    if not row:
        db.close(); return jsonify({'ok': False, 'error': 'introuvable'}), 404
    kg = float(d.get('kg', 0) or 0)
    if not kg and d.get('sacs') is not None:
        kg = float(d.get('sacs', 0) or 0) * float(row['taille_sac_kg'] or 50)
    db.execute("UPDATE aliments_stock SET stock_kg = stock_kg + ? WHERE id=?", (kg, aid))
    db.commit(); db.close()
    return jsonify({'ok': True, 'kg': kg})


@app.route('/api/aliments-stock/<int:aid>', methods=['PUT'])
def update_aliment_stock(aid):
    """Édition seuil / capacité / taille_sac / stock (correction manuelle)."""
    d = request.json or {}; db = get_db()
    sets = []; vals = []
    for k in ('stock_kg', 'taille_sac_kg', 'capacite_kg', 'seuil_kg', 'libelle'):
        if d.get(k) is not None:
            sets.append(f"{k}=?"); vals.append(d.get(k))
    if sets:
        vals.append(aid)
        db.execute("UPDATE aliments_stock SET " + ",".join(sets) + " WHERE id=?", vals)
        db.commit()
    db.close()
    return jsonify({'ok': True})

@app.route('/api/congelateur', methods=['POST'])
def add_stock_congelateur():
    d = request.json; db = get_db()
    db.execute("""INSERT INTO stock_congelateur
        (casier,coupe,poids,prix_unitaire,statut,date_abattage,observations)
        VALUES (?,?,?,?,?,?,?)""",
        (d.get('casier',1),d.get('coupe',''),d.get('poids',0),d.get('prix_unitaire',4000),
         d.get('statut','Disponible'),d.get('date_abattage',''),d.get('observations','')))
    db.commit(); return jsonify({'ok':True})

@app.route('/api/congelateur/<int:sid>', methods=['PUT'])
def update_stock_congelateur(sid):
    d = request.json; db = get_db()
    db.execute("""UPDATE stock_congelateur SET statut=?,date_vente=?,client=?,prix_vente=?,observations=? WHERE id=?""",
        (d.get('statut','Disponible'),d.get('date_vente'),d.get('client',''),d.get('prix_vente',0),d.get('observations',''),sid))
    db.commit(); return jsonify({'ok':True})

@app.route('/api/congelateur/<int:sid>', methods=['DELETE'])
def delete_stock_congelateur(sid):
    db = get_db(); db.execute("DELETE FROM stock_congelateur WHERE id=?",(sid,)); db.commit()
    return jsonify({'ok':True})

# ══════════════════════════════════════════════════
# VENTES INDIVIDUELLES
# ══════════════════════════════════════════════════
@app.route('/api/ventes-individuelles', methods=['GET'])
def get_ventes_individuelles():
    db = get_db()
    # Auto-créer colonnes manquantes
    cols = [c['name'] for c in db.execute('PRAGMA table_info(ventes_individuelles)').fetchall()]
    for col, defn in [('adresse','TEXT DEFAULT ""'), ('statut_livraison','TEXT DEFAULT "À livrer"')]:
        if col not in cols:
            db.execute(f'ALTER TABLE ventes_individuelles ADD COLUMN {col} {defn}')
            db.commit()
    rows = db.execute("""SELECT id,date_vente,client,telephone,coupe,poids,
        prix_unitaire,prix_total,casier,mode_paiement,statut_paiement,
        statut_livraison,adresse,observations,transaction_id
        FROM ventes_individuelles ORDER BY date_vente DESC,id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/ventes-individuelles', methods=['POST'])
def add_vente_individuelle():
    d = request.json; db = get_db()
    cur = db.execute("""INSERT INTO ventes_individuelles
        (date_vente,client,telephone,coupe,poids,prix_unitaire,prix_total,casier,
         stock_id,mode_paiement,statut_paiement,statut_livraison,adresse,observations,transaction_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('date_vente',''),d.get('client',''),d.get('telephone',''),d.get('coupe',''),
         d.get('poids',0),d.get('prix_unitaire',0),d.get('prix_total',0),d.get('casier'),
         d.get('stock_id'),d.get('mode_paiement','Espèces'),
         d.get('statut_paiement','Payé'), d.get('statut_livraison','À livrer'),
         d.get('adresse',''), d.get('observations',''),
         d.get('transaction_id','')))
    if d.get('stock_id'):
        db.execute("UPDATE stock_congelateur SET statut='Vendu',date_vente=?,client=?,prix_vente=? WHERE id=?",
            (d['date_vente'],d['client'],d['prix_total'],d['stock_id']))
    db.commit(); return jsonify({'ok':True,'id':cur.lastrowid})

@app.route('/api/ventes-individuelles/<int:vid>/statut', methods=['PUT'])
def update_vente_individuelle(vid):
    d = request.json or {}
    db = get_db()
    cols = [c['name'] for c in db.execute('PRAGMA table_info(ventes_individuelles)').fetchall()]
    for col, defn in [('adresse','TEXT DEFAULT ""'),('statut_livraison','TEXT DEFAULT "À livrer"')]:
        if col not in cols:
            db.execute(f'ALTER TABLE ventes_individuelles ADD COLUMN {col} {defn}')
            db.commit()
    # Récupérer le transaction_id pour mettre à jour toutes les pièces du groupe
    row = db.execute('SELECT transaction_id FROM ventes_individuelles WHERE id=?', (vid,)).fetchone()
    txn_id = row['transaction_id'] if row else None
    if txn_id:
        db.execute("""UPDATE ventes_individuelles SET
            client=?, telephone=?, statut_paiement=?, statut_livraison=?, adresse=?, observations=?
            WHERE transaction_id=?""",
            (d.get('client',''), d.get('telephone',''),
             d.get('statut_paiement','Payé'), d.get('statut_livraison','À livrer'),
             d.get('adresse',''), d.get('observations',''), txn_id))
    else:
        db.execute("""UPDATE ventes_individuelles SET
            client=?, telephone=?, statut_paiement=?, statut_livraison=?, adresse=?, observations=?
            WHERE id=?""",
            (d.get('client',''), d.get('telephone',''),
             d.get('statut_paiement','Payé'), d.get('statut_livraison','À livrer'),
             d.get('adresse',''), d.get('observations',''), vid))
    db.commit()
    db.close()
    return jsonify({'ok': True})



@app.route('/api/ventes-individuelles/<int:vid>', methods=['DELETE'])
def delete_vente_individuelle(vid):
    db = get_db()
    row = db.execute("SELECT stock_id FROM ventes_individuelles WHERE id=?",(vid,)).fetchone()
    if row and row['stock_id']:
        db.execute("UPDATE stock_congelateur SET statut='Disponible',date_vente=NULL,client='',prix_vente=0 WHERE id=?",(row['stock_id'],))
    db.execute("DELETE FROM ventes_individuelles WHERE id=?",(vid,)); db.commit()
    db.close()
    return jsonify({'ok':True})

# ══════════════════════════════════════════════════
# COMMANDES DÉTAIL (pré-commande multi-coupes)
# ══════════════════════════════════════════════════
@app.route('/api/commandes-detail', methods=['GET'])
def get_commandes_detail():
    db = get_db()
    import json
    rows = db.execute("""SELECT id,date_commande,client,telephone,adresse_livraison,
        mode_paiement,statut_paiement,statut_livraison,lignes_json,total_fcfa,observations
        FROM commandes_detail ORDER BY date_commande DESC,id DESC""").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try: d['lignes'] = json.loads(d['lignes_json'] or '[]')
        except: d['lignes'] = []
        result.append(d)
    return jsonify(result)

@app.route('/api/commandes-detail', methods=['POST'])
def add_commande_detail():
    import json
    d = request.json; db = get_db()
    lignes = d.get('lignes', [])
    total  = sum(l.get('prix_total',0) for l in lignes)
    cur = db.execute("""INSERT INTO commandes_detail
        (date_commande,client,telephone,adresse_livraison,mode_paiement,
         statut_paiement,statut_livraison,lignes_json,total_fcfa,observations)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d.get('date_commande',''),d.get('client',''),d.get('telephone',''),
         d.get('adresse_livraison',''),d.get('mode_paiement','Espèces'),
         d.get('statut_paiement','En attente de paiement'),
         d.get('statut_livraison','En attente de livraison'),
         json.dumps(lignes, ensure_ascii=False),total,d.get('observations','')))
    db.commit(); return jsonify({'ok':True,'id':cur.lastrowid})

@app.route('/api/commandes-detail/<int:cid>', methods=['PUT'])
def update_commande_detail(cid):
    d = request.json; db = get_db()
    # Lire l'état actuel pour ne pas écraser les champs non envoyés
    row = db.execute("SELECT statut_paiement, statut_livraison, observations FROM commandes_detail WHERE id=?", (cid,)).fetchone()
    if not row: return jsonify({'ok': False, 'error': 'Not found'}), 404
    statut_paiement  = d.get('statut_paiement',  row['statut_paiement'])
    statut_livraison = d.get('statut_livraison', row['statut_livraison'])
    observations     = d.get('observations',     row['observations'] or '')
    db.execute("UPDATE commandes_detail SET statut_paiement=?,statut_livraison=?,observations=? WHERE id=?",
        (statut_paiement, statut_livraison, observations, cid))
    db.commit(); return jsonify({'ok':True})


@app.route('/api/commandes-detail/<int:cid>/full', methods=['PUT'])
def update_commande_detail_full(cid):
    import json
    d = request.json; db = get_db()
    lignes = d.get('lignes', [])
    total  = sum(l.get('prix_total',0) for l in lignes)
    db.execute("""UPDATE commandes_detail SET
        date_commande=?,client=?,telephone=?,adresse_livraison=?,mode_paiement=?,
        statut_paiement=?,statut_livraison=?,lignes_json=?,total_fcfa=?,observations=?
        WHERE id=?""",
        (d.get('date_commande'),d.get('client'),d.get('telephone'),
         d.get('adresse_livraison'),d.get('mode_paiement'),
         d.get('statut_paiement'),d.get('statut_livraison'),
         json.dumps(lignes,ensure_ascii=False),total,d.get('observations'),cid))
    db.commit(); return jsonify({'ok':True})

@app.route('/api/commandes-detail/<int:cid>', methods=['DELETE'])
def delete_commande_detail(cid):
    db = get_db(); db.execute("DELETE FROM commandes_detail WHERE id=?",(cid,)); db.commit()
    return jsonify({'ok':True})


@app.route('/api/diag', methods=['GET'])
def diag():
    """Route de diagnostic : confirme le chemin DB et teste une écriture."""
    import os, time
    db = get_db()
    # Tester une écriture réelle
    ts = str(time.time())
    try:
        db.execute("CREATE TABLE IF NOT EXISTS _diag (val TEXT)")
        db.execute("DELETE FROM _diag")
        db.execute("INSERT INTO _diag (val) VALUES (?)", (ts,))
        db.commit()
        # Relire pour confirmer
        row = db.execute("SELECT val FROM _diag").fetchone()
        written = row['val'] if row else 'AUCUNE VALEUR'
        db.execute("DROP TABLE IF EXISTS _diag")
        db.commit()
    except Exception as e:
        written = 'ERREUR: '+str(e)
    db.close()
    return jsonify({
        'db_path':    str(DB),
        'db_exists':  os.path.exists(DB),
        'db_size_kb': round(os.path.getsize(DB)/1024) if os.path.exists(DB) else 0,
        'write_test': written,
        'write_ok':   written == ts,
        'cwd':        os.getcwd(),
    })


@app.route('/api/portees-test', methods=['GET'])
def portees_test():
    """Teste si les portées sont lisibles et modifiables."""
    db = get_db()
    count = db.execute("SELECT COUNT(*) as n FROM cycles").fetchone()['n']
    # Lire portée rang 7 de truie 01
    row = db.execute("""
        SELECT c.id, c.numero_portee, c.code, c.date_sevrage, c.verrat_nom
        FROM cycles c JOIN animaux a ON a.id=c.animal_id
        WHERE trim(substr(a.feuille,7))='01' AND CAST(c.numero_portee AS INTEGER)=7
    """).fetchone()
    db.close()
    return jsonify({
        'total_cycles': count,
        'portee_01_rang7': dict(row) if row else None,
    })



# ══════════════════════════════════════════════════════════════════════════
# IDENTIFICATION & TRAÇABILITÉ — Tatouage / Étiquetage des porcelets
# ══════════════════════════════════════════════════════════════════════════

@app.route('/api/identification', methods=['GET'])
def get_identification():
    db = get_db()
    rows = db.execute("""
        SELECT i.*,
               (SELECT COUNT(*) FROM identification_porcelets i2
                WHERE i2.truie_mere=i.truie_mere AND i2.verrat_pere=i.verrat_pere
                AND i2.id != i.id) as nb_meme_parents
        FROM identification_porcelets i
        ORDER BY i.date_naissance DESC, i.code_tattoo
    """).fetchall()
    db.close()
    return jsonify(rows_to_list(rows))


@app.route('/api/identification', methods=['POST'])
def add_identification():
    d = request.json; db = get_db()
    exists = db.execute("SELECT id FROM identification_porcelets WHERE code_tattoo=?", (d['code_tattoo'],)).fetchone()
    if exists:
        db.close(); return jsonify({'error': 'Code tattoo déjà utilisé'}), 409
    cur = db.execute("""
        INSERT INTO identification_porcelets
        (code_tattoo,sexe,date_naissance,truie_mere,verrat_pere,rang_portee,
         poids_naissance,poids_sevrage,statut,destination,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (d['code_tattoo'], d.get('sexe','M'), d.get('date_naissance'),
         d.get('truie_mere',''), d.get('verrat_pere',''), d.get('rang_portee',1),
         d.get('poids_naissance',0), d.get('poids_sevrage',0),
         d.get('statut','Actif'), d.get('destination',''), d.get('notes','')))
    db.commit(); pid=cur.lastrowid; db.close()
    return jsonify({'id': pid, 'ok': True}), 201


@app.route('/api/identification/<int:pid>', methods=['PUT'])
def update_identification(pid):
    d = request.json; db = get_db()
    db.execute("""UPDATE identification_porcelets
        SET code_tattoo=?,sexe=?,date_naissance=?,truie_mere=?,verrat_pere=?,
            rang_portee=?,poids_naissance=?,poids_sevrage=?,statut=?,destination=?,notes=?
        WHERE id=?""",
        (d['code_tattoo'],d.get('sexe','M'),d.get('date_naissance'),
         d.get('truie_mere',''),d.get('verrat_pere',''),d.get('rang_portee',1),
         d.get('poids_naissance',0),d.get('poids_sevrage',0),
         d.get('statut','Actif'),d.get('destination',''),d.get('notes',''),pid))
    db.commit(); db.close()
    return jsonify({'ok': True})


@app.route('/api/identification/<int:pid>', methods=['DELETE'])
def delete_identification(pid):
    db = get_db()
    db.execute("DELETE FROM identification_porcelets WHERE id=?", (pid,))
    db.commit(); db.close()
    return jsonify({'ok': True})


@app.route('/api/identification/check-consanguinite', methods=['GET'])
def check_consanguinite():
    truie  = request.args.get('truie','')
    verrat = request.args.get('verrat','')
    if not truie or not verrat:
        return jsonify({'risques':[],'niveau':'faible'})
    db = get_db(); risques = []

    # Verrat fils de la truie ?
    v = db.execute("SELECT truie_mere FROM identification_porcelets WHERE code_tattoo=? LIMIT 1",(verrat,)).fetchone()
    if v and v['truie_mere']==truie:
        risques.append('Le verrat est fils de la truie {} → consanguinité directe mère-fils'.format(truie))

    # Truie fille du verrat ?
    t = db.execute("SELECT verrat_pere FROM identification_porcelets WHERE code_tattoo=? LIMIT 1",(truie,)).fetchone()
    if t and t['verrat_pere']==verrat:
        risques.append('La truie est fille du verrat {} → consanguinité directe père-fille'.format(verrat))

    # Portées déjà issues du même couple
    n = db.execute("SELECT COUNT(*) as n FROM identification_porcelets WHERE truie_mere=? AND verrat_pere=?",(truie,verrat)).fetchone()['n']
    if n>0:
        risques.append('{} porcelet(s) déjà issus de ce couple → risque fratrie'.format(n))

    # Ancêtres communs (grand-parents)
    tp = db.execute("SELECT truie_mere,verrat_pere FROM identification_porcelets WHERE code_tattoo=? LIMIT 1",(truie,)).fetchone()
    vp = db.execute("SELECT truie_mere,verrat_pere FROM identification_porcelets WHERE code_tattoo=? LIMIT 1",(verrat,)).fetchone()
    if tp and vp:
        if tp['truie_mere'] and tp['truie_mere']==vp['truie_mere']:
            risques.append('Même grand-mère ({}) → consanguinité 2ème degré'.format(tp['truie_mere']))
        if tp['verrat_pere'] and tp['verrat_pere']==vp['verrat_pere']:
            risques.append('Même grand-père ({}) → consanguinité 2ème degré'.format(tp['verrat_pere']))

    db.close()
    return jsonify({'risques':risques,'niveau':'eleve' if risques else 'faible'})


@app.route('/api/identification/generate-code', methods=['GET'])
def generate_tattoo_code():
    db = get_db()
    annee = datetime.now().strftime('%Y')
    prefix = 'CDO-{}-'.format(annee)
    last = db.execute(
        "SELECT code_tattoo FROM identification_porcelets WHERE code_tattoo LIKE ? ORDER BY code_tattoo DESC LIMIT 1",
        (prefix+'%',)).fetchone()
    seq = 1
    if last:
        try: seq = int(last['code_tattoo'].split('-')[-1]) + 1
        except: pass
    db.close()
    return jsonify({'code': '{}{:04d}'.format(prefix, seq)})

# ══════════════════════════════════════════════════════════════════════════
#  LOGES & STATUT TRUIES
# ══════════════════════════════════════════════════════════════════════════

def _d(s):
    """Parse une date 'YYYY-MM-DD' tolérante -> datetime.date | None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


@app.route('/api/statut-truies', methods=['GET'])
def statut_truies():
    """Calcule, depuis `cycles`, les truies allaitantes et sevrées non saillies.
       Logique basée sur le DERNIER cycle significatif (saillie ou mise bas) de
       chaque truie — robuste face aux trous de données historiques."""
    db = get_db()
    rows = db.execute("""
        SELECT trim(substr(a.feuille,7)) AS truie_num, a.id AS animal_id,
               c.numero_portee, c.saillie_1, c.date_mise_bas, c.date_sevrage,
               c.nais_vivants, c.sevres, c.code, c.observations
        FROM cycles c JOIN animaux a ON a.id = c.animal_id
        WHERE a.type_animal = 'truie'
    """).fetchall()
    db.close()

    # Regrouper par truie
    by_truie = {}
    for r in rows:
        by_truie.setdefault(r['truie_num'], []).append(dict(r))

    today = datetime.now().date()
    allaitantes, sevrees = [], []

    for tnum, cyc in by_truie.items():
        def rang(c):
            try: return int(c['numero_portee'])
            except (TypeError, ValueError): return 0
        # cycles "significatifs" = au moins une saillie ou une mise bas
        meaningful = [c for c in cyc if c.get('saillie_1') or c.get('date_mise_bas')]
        if not meaningful:
            continue
        cur = max(meaningful, key=rang)
        mb  = _d(cur.get('date_mise_bas'))
        svr = _d(cur.get('date_sevrage'))
        obs = (cur.get('observations') or '').upper()
        if 'REFORM' in obs:
            continue

        if mb and not svr:
            # A. allaitante : a mis bas, pas encore sevrée
            allaitantes.append({
                'truie_num':       tnum,
                'rang_portee':     rang(cur),
                'date_mb':         cur.get('date_mise_bas'),
                'jours_allaitement': (today - mb).days,
                'nes_vivants':     cur.get('nais_vivants') or 0,
                'sevrage_prevu':   (mb + timedelta(days=28)).strftime('%Y-%m-%d'),
            })
        elif svr:
            # B. sevrée non saillie : sevrage fait, aucune saillie postérieure
            has_newer_saillie = any(rang(c) > rang(cur) and c.get('saillie_1') for c in cyc)
            if not has_newer_saillie:
                sevrees.append({
                    'truie_num':           tnum,
                    'rang_portee':         rang(cur),
                    'date_sevrage':        cur.get('date_sevrage'),
                    'jours_depuis_sevrage':(today - svr).days,
                })

    allaitantes.sort(key=lambda x: (len(x['truie_num']), x['truie_num']))
    sevrees.sort(key=lambda x: -x['jours_depuis_sevrage'])  # trou improductif le + long en tête
    return jsonify({
        'date_ref': today.strftime('%Y-%m-%d'),
        'allaitantes': allaitantes,
        'sevrees_non_saillies': sevrees,
    })


_LOGE_FIELDS = ('nom', 'type', 'capacite', 'statut', 'occupant_type', 'occupant_id',
                'occupant_label', 'raison_vide', 'travaux_a_faire', 'etat_pipette',
                'ordre', 'notes', 'batiment', 'cote', 'position', 'vis_a_vis_id',
                'occupant_autre', 'date_debut_occupation')


def _is_real_verrat_name(name):
    """Rejette les libellés génériques/corrompus ('', 'VERRAT 01', 'FICHE VERRAT')."""
    n = (name or '').strip()
    if not n:
        return False
    u = n.upper().replace(' ', '')
    if u.startswith('FICHEVERRAT') or u.startswith('VERRAT'):
        return False
    return True


def _animal_label(feuille, type_animal, anom=None, vnom=None):
    """Truie -> 'Truie 01'. Verrat -> son vrai nom (priorité table `verrats`,
       puis `animaux.nom`), avec repli sur 'Verrat 01' si nom absent/générique."""
    num = (feuille or '')[6:].strip()
    if (type_animal or '') == 'verrat':
        for cand in (vnom, anom):
            if _is_real_verrat_name(cand):
                return cand.strip()
        return ('Verrat ' + num).strip() if num else (feuille or 'Verrat')
    return ('Truie ' + num).strip() if num else (feuille or 'Truie')


def _truie_label(db, animal_id):
    """Libellé court d'une truie ('Truie 08') à partir de son id."""
    row = db.execute("SELECT feuille FROM animaux WHERE id=?", (animal_id,)).fetchone()
    return _animal_label(row['feuille'] if row else None, 'truie')


def _set_loge_occupants(db, lid, animal_ids):
    """Affecte la liste d'animaux à la loge lid de façon EXCLUSIVE (chaque animal
       est d'abord retiré de toute autre loge). Renvoie la liste des libellés."""
    try:
        ids = [int(a) for a in (animal_ids or []) if str(a).strip() != '']
    except (TypeError, ValueError):
        ids = []
    # purge : la loge courante + retrait des animaux d'éventuelles autres loges
    db.execute("DELETE FROM loge_occupants WHERE loge_id=?", (lid,))
    for aid in ids:
        db.execute("DELETE FROM loge_occupants WHERE animal_id=?", (aid,))
    labels = []
    for aid in ids:
        db.execute("INSERT OR IGNORE INTO loge_occupants (loge_id,animal_id) VALUES (?,?)",
                   (lid, aid))
        row = db.execute("""
            SELECT a.feuille, a.type_animal, a.nom AS anom,
                   (SELECT v.nom FROM verrats v
                     WHERE printf('%02d', CAST(replace(replace(v.numero,'V',''),'v','') AS INTEGER))
                           = trim(substr(a.feuille,7)) LIMIT 1) AS vnom
            FROM animaux a WHERE a.id=?""", (aid,)).fetchone()
        if row:
            labels.append(_animal_label(row['feuille'], row['type_animal'],
                                        row['anom'], row['vnom']))
    return labels


def _sync_occupant_label(db, lid, labels, autre):
    """Reconstruit le libellé d'affichage dénormalisé (animaux + 'autre')."""
    parts = list(labels)
    autre = (autre or '').strip()
    if autre:
        parts.append(autre)
    db.execute("UPDATE loges SET occupant_label=? WHERE id=?", (', '.join(parts), lid))


def _reconcile_loge(db, lid, d):
    """Synchronise loge_occupants <-> statut. La table de liaison FAIT FOI.
       - occupants fournis -> (ré)affectation exclusive + libellé reconstruit ;
       - statut vidé explicitement (!= 'occupee') -> purge de la liaison ;
       - statut final DÉRIVÉ de la liaison : présence d'occupant/autre -> 'occupee',
         sinon on conserve l'état vide choisi (et on rebascule un 'occupee' devenu
         orphelin vers 'disponible'). Évite toute désynchro carte/éditeur/menu.
    """
    has_occ_key = ('occupants' in d) or ('occupant_autre' in d)
    if has_occ_key:
        labels = _set_loge_occupants(db, lid, d.get('occupants'))
        _sync_occupant_label(db, lid, labels, d.get('occupant_autre', ''))

    st = d.get('statut')
    if (not has_occ_key) and st is not None and st != 'occupee':
        # Vidage piloté par le statut, seulement si la liaison n'est pas fournie.
        db.execute("DELETE FROM loge_occupants WHERE loge_id=?", (lid,))
        db.execute("UPDATE loges SET occupant_label='', occupant_autre='' WHERE id=?", (lid,))

    n = db.execute("SELECT COUNT(*) FROM loge_occupants WHERE loge_id=?", (lid,)).fetchone()[0]
    row = db.execute("SELECT statut, occupant_autre FROM loges WHERE id=?", (lid,)).fetchone()
    autre = ((row['occupant_autre'] if row else '') or '').strip()
    cur_statut = (row['statut'] if row else '') or ''
    if n > 0 or autre:
        db.execute("UPDATE loges SET statut='occupee', raison_vide='' WHERE id=?", (lid,))
    elif cur_statut == 'occupee':
        db.execute("UPDATE loges SET statut='disponible', occupant_label='' WHERE id=?", (lid,))


@app.route('/api/reproducteurs-actifs', methods=['GET'])
def reproducteurs_actifs():
    """Reproducteurs ACTIFS (truies + verrats, hors réforme) + loge actuelle
       éventuelle (pour griser ceux déjà affectés ailleurs côté frontend)."""
    db = get_db()
    rows = db.execute("""
        SELECT a.id, a.feuille, a.type_animal, a.nom AS anom,
               (SELECT v.nom FROM verrats v
                 WHERE printf('%02d', CAST(replace(replace(v.numero,'V',''),'v','') AS INTEGER))
                       = trim(substr(a.feuille,7)) LIMIT 1) AS vnom,
               (SELECT lo.loge_id FROM loge_occupants lo
                 WHERE lo.animal_id = a.id LIMIT 1) AS loge_actuelle_id
        FROM animaux a
        WHERE a.type_animal IN ('truie','verrat')
          AND COALESCE(a.reformer, 0) = 0
        ORDER BY a.type_animal,
                 CAST(trim(substr(a.feuille,7)) AS INTEGER)
    """).fetchall()
    db.close()
    return jsonify([{
        'id': r['id'],
        'label': _animal_label(r['feuille'], r['type_animal'], r['anom'], r['vnom']),
        'type': r['type_animal'],
        'loge_actuelle_id': r['loge_actuelle_id'],
    } for r in rows])


@app.route('/api/loges/<int:lid>/maternite', methods=['PUT'])
def update_loge_maternite(lid):
    """Sync LOGE → CYCLE : saisie de la mise bas / morts / sevrage directement
       depuis la carte loge. Résout le MÊME cycle que GET /api/loges (cycle
       ouvert prioritaire, sinon rang max) — on ne fait JAMAIS confiance à un
       id de cycle envoyé par le client.
       Règle (d) : si sevres_m/sevres_f ne sont pas fournis explicitement par le
       client, on les calcule = max(0, nés - morts_pré_sevrage + adopt) afin de
       rester cohérent avec epCalcSevres() côté portée et d'éviter toute
       désynchronisation."""
    d = request.json or {}
    db = get_db()

    loge = db.execute("SELECT type FROM loges WHERE id=?", (lid,)).fetchone()
    if not loge:
        db.close()
        return jsonify({'error': 'Loge introuvable'}), 404
    if (loge['type'] or '') != 'maternite':
        db.close()
        return jsonify({'error': "Cette loge n'est pas une maternité"}), 400

    occ_truie = db.execute("""
        SELECT lo.animal_id FROM loge_occupants lo
        JOIN animaux a ON a.id = lo.animal_id
        WHERE lo.loge_id=? AND a.type_animal='truie'
        LIMIT 1
    """, (lid,)).fetchone()
    if not occ_truie:
        db.close()
        return jsonify({'error': "Aucune truie occupante dans cette loge"}), 400
    aid = occ_truie['animal_id']

    # Même résolution de cycle que GET /api/loges (cycle ouvert prioritaire, sinon rang max)
    cyc = db.execute("""
        SELECT id, sexe_m, sexe_f, mort_pre_m, mort_pre_f, adopt_m, adopt_f
        FROM cycles WHERE animal_id=?
        ORDER BY (date_sevrage IS NOT NULL),
                 CAST(numero_portee AS INTEGER) DESC, id DESC
        LIMIT 1
    """, (aid,)).fetchone()
    if not cyc:
        db.close()
        return jsonify({'error': "Aucun cycle trouvé pour cette truie"}), 404
    cid = cyc['id']

    sexe_m     = d.get('mat_sexe_m',     cyc['sexe_m'])      or 0
    sexe_f     = d.get('mat_sexe_f',     cyc['sexe_f'])      or 0
    mort_pre_m = d.get('mat_mort_pre_m', cyc['mort_pre_m'])  or 0
    mort_pre_f = d.get('mat_mort_pre_f', cyc['mort_pre_f'])  or 0
    adopt_m    = cyc['adopt_m'] or 0
    adopt_f    = cyc['adopt_f'] or 0

    if 'mat_sevres_m' in d or 'mat_sevres_f' in d:
        sevres_m = d.get('mat_sevres_m', 0) or 0
        sevres_f = d.get('mat_sevres_f', 0) or 0
    else:
        sevres_m = max(0, sexe_m - mort_pre_m + adopt_m)
        sevres_f = max(0, sexe_f - mort_pre_f + adopt_f)

    sets, vals = [], []
    if 'mat_date_mb' in d:
        sets.append('date_mise_bas=?'); vals.append(d.get('mat_date_mb') or None)
    sets += ['sexe_m=?', 'sexe_f=?', 'mort_pre_m=?', 'mort_pre_f=?',
             'sevres_m=?', 'sevres_f=?', 'sevres=?']
    vals += [sexe_m, sexe_f, mort_pre_m, mort_pre_f, sevres_m, sevres_f, sevres_m + sevres_f]
    if 'mat_date_sevrage' in d:
        sets.append('date_sevrage=?'); vals.append(d.get('mat_date_sevrage') or None)
    vals.append(cid)

    db.execute(f"UPDATE cycles SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()
    db.close()
    return jsonify({'ok': True, 'cycle_id': cid,
                     'sevres_m': sevres_m, 'sevres_f': sevres_f})


@app.route('/api/loges', methods=['GET'])
def get_loges():
    db = get_db()
    rows = db.execute("SELECT * FROM loges ORDER BY ordre, id").fetchall()
    occ = db.execute("""
        SELECT lo.loge_id, lo.animal_id, a.type_animal
        FROM loge_occupants lo
        LEFT JOIN animaux a ON a.id = lo.animal_id
    """).fetchall()
    # Vide sanitaire : durée FIXE globale (jours)
    _vs = db.execute("SELECT valeur FROM parametres WHERE cle='vide_sanitaire_jours'").fetchone()
    try:
        vs_days = int(float(_vs['valeur'])) if _vs and _vs['valeur'] is not None else 5
    except (TypeError, ValueError):
        vs_days = 5
    _gd = db.execute("SELECT valeur FROM parametres WHERE cle='gestation'").fetchone()
    try:
        gest_days = int(float(_gd['valeur'])) if _gd and _gd['valeur'] is not None else 114
    except (TypeError, ValueError):
        gest_days = 114
    occ_map, verrat_loges, occ_truies = {}, set(), {}
    for o in occ:
        occ_map.setdefault(o['loge_id'], []).append(o['animal_id'])
        if (o['type_animal'] or '') == 'verrat':
            verrat_loges.add(o['loge_id'])
        if (o['type_animal'] or '') == 'truie':
            occ_truies.setdefault(o['loge_id'], []).append(o['animal_id'])
    today = datetime.now().strftime('%Y-%m-%d')
    out = []
    for r in rows:
        d = dict(r)
        d['occupants'] = occ_map.get(r['id'], [])
        d['has_verrat'] = r['id'] in verrat_loges
        d['vide_sanitaire_actif'] = False
        d['vide_sanitaire_jours'] = vs_days
        # ── Maternité : joindre le cycle ACTIF de CHAQUE truie occupante ──
        if (r['type'] or '') == 'maternite':
            mat_truies = []
            for aid in occ_truies.get(r['id'], []):
                cyc = db.execute("""
                    SELECT date_mise_bas, sexe_m, sexe_f, mort_pre_m, mort_pre_f,
                           sevres_m, sevres_f, date_sevrage, numero_portee
                    FROM cycles WHERE animal_id=?
                    ORDER BY (date_sevrage IS NOT NULL),
                             CAST(numero_portee AS INTEGER) DESC, id DESC
                    LIMIT 1
                """, (aid,)).fetchone()
                if not cyc:
                    continue
                mb  = cyc['date_mise_bas']
                dsv = cyc['date_sevrage']            # = date de LIBÉRATION (décision)
                sevrage_prevu = None
                if mb:
                    try:
                        sevrage_prevu = (datetime.strptime(mb, '%Y-%m-%d')
                                         + timedelta(days=28)).strftime('%Y-%m-%d')
                    except ValueError:
                        sevrage_prevu = None
                entry = {
                    'animal_id':        aid,
                    'nom':              _truie_label(db, aid),
                    'mat_date_mb':      mb,
                    'mat_sexe_m':       cyc['sexe_m'] or 0,
                    'mat_sexe_f':       cyc['sexe_f'] or 0,
                    'mat_mort_pre_m':   cyc['mort_pre_m'] or 0,
                    'mat_mort_pre_f':   cyc['mort_pre_f'] or 0,
                    'mat_sevres_m':     cyc['sevres_m'] or 0,
                    'mat_sevres_f':     cyc['sevres_f'] or 0,
                    'mat_date_sevrage': dsv,
                    'mat_sevrage_prevu': sevrage_prevu,
                    'vide_sanitaire_actif': False,
                }
                if dsv:
                    try:
                        fin = (datetime.strptime(dsv, '%Y-%m-%d')
                               + timedelta(days=vs_days)).strftime('%Y-%m-%d')
                        entry['vide_sanitaire_fin'] = fin
                        entry['vide_sanitaire_actif'] = (dsv <= today <= fin)
                    except ValueError:
                        pass
                mat_truies.append(entry)
            if mat_truies:
                d['mat_truies'] = mat_truies
                # Champs "legacy" (1ère truie) pour compat ascendante
                first = mat_truies[0]
                d['mat_date_mb']       = first['mat_date_mb']
                d['mat_sexe_m']        = first['mat_sexe_m']
                d['mat_sexe_f']        = first['mat_sexe_f']
                d['mat_mort_pre_m']    = first['mat_mort_pre_m']
                d['mat_mort_pre_f']    = first['mat_mort_pre_f']
                d['mat_sevres_m']      = first['mat_sevres_m']
                d['mat_sevres_f']      = first['mat_sevres_f']
                d['mat_date_sevrage']  = first['mat_date_sevrage']
                d['mat_sevrage_prevu'] = first['mat_sevrage_prevu']
                if first.get('vide_sanitaire_actif'):
                    d['vide_sanitaire_fin']   = first.get('vide_sanitaire_fin')
                    d['vide_sanitaire_actif'] = True
        # ── Gestante : joindre le cycle OUVERT (non mis bas) de CHAQUE truie occupante ──
        if (r['type'] or '') == 'gestante':
            gest_truies = []
            for aid in occ_truies.get(r['id'], []):
                cyc = db.execute("""
                    SELECT saillie_1, saillie_2, saillie_3, verrat_nom
                    FROM cycles WHERE animal_id=? AND date_mise_bas IS NULL
                    ORDER BY CAST(numero_portee AS INTEGER) DESC, id DESC
                    LIMIT 1
                """, (aid,)).fetchone()
                if not cyc:
                    continue
                saillie = None
                for k in ('saillie_3', 'saillie_2', 'saillie_1'):
                    if cyc[k]:
                        saillie = cyc[k]
                        break
                if not saillie:
                    continue
                try:
                    sd = datetime.strptime(saillie, '%Y-%m-%d')
                    mb_prevue = (sd + timedelta(days=gest_days)).strftime('%Y-%m-%d')
                    jours = (datetime.strptime(today, '%Y-%m-%d') - sd).days
                    gest_truies.append({
                        'animal_id':          aid,
                        'nom':                _truie_label(db, aid),
                        'gest_date_saillie':  saillie,
                        'gest_jours':         max(0, min(jours, gest_days)),
                        'gest_jours_total':   gest_days,
                        'gest_mb_prevue':     mb_prevue,
                        'gest_verrat':        cyc['verrat_nom'] or '',
                    })
                except ValueError:
                    pass
            if gest_truies:
                d['gest_truies'] = gest_truies
                # Champs "legacy" (1ère truie) pour compat ascendante
                first = gest_truies[0]
                d['gest_date_saillie'] = first['gest_date_saillie']
                d['gest_jours']        = first['gest_jours']
                d['gest_jours_total']  = first['gest_jours_total']
                d['gest_mb_prevue']    = first['gest_mb_prevue']
                d['gest_verrat']       = first['gest_verrat']
        out.append(d)
    db.close()
    return jsonify(out)


@app.route('/api/loges', methods=['POST'])
def add_loge():
    d = request.json or {}
    if not (d.get('nom') or '').strip():
        return jsonify({'error': 'Nom de loge requis'}), 400
    db = get_db()
    cur = db.execute("""
        INSERT INTO loges (nom,type,capacite,statut,occupant_type,occupant_id,
            occupant_label,raison_vide,travaux_a_faire,etat_pipette,ordre,notes,
            batiment,cote,position,occupant_autre,date_debut_occupation)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        d.get('nom', '').strip(), d.get('type', ''), d.get('capacite', 1),
        d.get('statut', 'disponible'), d.get('occupant_type', ''), d.get('occupant_id'),
        d.get('occupant_label', ''), d.get('raison_vide', ''), d.get('travaux_a_faire', ''),
        d.get('etat_pipette', 'fonctionnelle'), d.get('ordre', 0), d.get('notes', ''),
        d.get('batiment', ''), d.get('cote', ''), d.get('position', 0),
        d.get('occupant_autre', ''), d.get('date_debut_occupation', ''),
    ))
    lid = cur.lastrowid
    _reconcile_loge(db, lid, d)
    db.commit()
    db.close()
    return jsonify({'ok': True, 'id': lid})


@app.route('/api/loges/<int:lid>', methods=['PUT'])
def update_loge(lid):
    d = request.json or {}
    sets, vals = [], []
    for f in _LOGE_FIELDS:
        if f in d:
            sets.append(f + '=?')
            vals.append(d[f])
    db = get_db()
    if sets:
        db.execute("UPDATE loges SET " + ', '.join(sets) + " WHERE id=?", vals + [lid])
    _reconcile_loge(db, lid, d)
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/loges/<int:lid>', methods=['DELETE'])
def delete_loge(lid):
    db = get_db()
    db.execute("DELETE FROM loge_occupants WHERE loge_id=?", (lid,))
    db.execute("DELETE FROM loges WHERE id=?", (lid,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    # Initialiser la DB si elle n'existe pas
    if not DB.exists():
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("init_db", INIT)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.init_db()

    # Migrations automatiques
    migrate_db()

    print("=" * 55)
    print("  🐷  La Cochette Dorée — Système de Gestion")
    print("=" * 55)
    print(f"  📂  Base de données : {DB}")
    print(f"  🌐  Interface      : http://127.0.0.1:5050")
    print("  (Ctrl+C pour quitter)")
    print("=" * 55)

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='127.0.0.1', port=5050, debug=False)
