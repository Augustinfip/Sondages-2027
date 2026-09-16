#!/usr/bin/env python3
"""
Script de mise à jour automatique des sondages depuis Wikipédia.

Ce script :
1. Récupère la page Wikipédia des sondages pour la présidentielle 2027
2. Repère les nouveaux sondages absents de data.json
3. Les ajoute à data.json en respectant la même structure que l'outil

IMPORTANT — limites connues (à lire avant de faire confiance à ce script) :
- La page Wikipédia change de colonnes (candidats) au fil du temps, en repartant sur
  un nouveau tableau à chaque fois qu'un candidat entre/sort de la course. Ce script
  ne traite QUE le tout premier tableau de la section "First-Round Polling"
  (le plus récent).
- Les noms de candidats sur Wikipédia sont différents des nôtres (ex. "Marine Le Pen"
  vs "Le Pen"). Le dictionnaire NAME_MAP fait la correspondance ; tout nom absent de
  ce dictionnaire est ignoré avec un avertissement plutôt que deviné.
- Wikipédia ne donne pas de nom à chaque hypothèse (contrairement à l'Excel d'origine).
  Ce script en génère un automatiquement, avec la même logique que le bouton "Ajouter
  un sondage" de l'outil (reconnaissance par signature de candidats testés).
- Ce script ne gère PAS le second tour (duels), uniquement le premier tour.
- Il ne modifie et ne supprime jamais rien : il ne fait qu'ajouter des lignes qui
  n'existent pas encore (comparaison par institut + date + candidats testés).

En cas d'échec, le script s'arrête proprement sans modifier data.json — la mise à jour
suivante réessaiera automatiquement le lendemain.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

WIKI_URL = "https://en.wikipedia.org/wiki/Opinion_polling_for_the_2027_French_presidential_election"
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
WIKI_PAGE_TITLE = "Opinion_polling_for_the_2027_French_presidential_election"
DATA_PATH = Path(__file__).parent.parent / "data.json"

# Correspondance nom Wikipédia (anglais, complet) -> nom utilisé dans l'outil.
# À compléter si un nouveau candidat apparaît (voir le rapport du script en cas de nom inconnu).
NAME_MAP = {
    "Nathalie Arthaud": "Arthaud",
    "Philippe Poutou": "Poutou",
    "Fabien Roussel": "Roussel",
    "Jean-Luc Mélenchon": "Mélenchon",
    "François Ruffin": "Ruffin",
    "Olivier Faure": "Faure",
    "François Hollande": "Hollande",
    "Raphaël Glucksmann": "Glucksmann",
    "Marine Tondelier": "Tondelier",
    "Gabriel Attal": "Attal",
    "Édouard Philippe": "Philippe",
    "Dominique de Villepin": "de Villepin",
    "Bruno Retailleau": "Retailleau",
    "Nicolas Dupont-Aignan": "Dupont-Aignan",
    "Marine Le Pen": "Le Pen",
    "Éric Zemmour": "Zemmour",
    "Sarah Knafo": "Knafo",
    "Jordan Bardella": "Bardella",
    "Xavier Bertrand": "Bertrand",
    "David Lisnard": "Lisnard",
    "Bruno Le Maire": "Le Maire",
    "Jean Lassalle": "Lasalle",
}

BLOC_OF = {}  # rempli dynamiquement à partir de data.json ci-dessous


def load_data():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def candidate_signature(scores):
    names = sorted(n for n, v in scores.items() if v is not None)
    return "|".join(names)


def next_hypothesis_number(polls):
    max_n = 0
    for p in polls:
        m = re.search(r"Hypoth[eè]se\s*(?:unique\s*)?(\d+)", p["hypothese"], re.IGNORECASE)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def generate_hypothesis_label(scores, candidates_order, polls):
    names = [c["name"] for c in candidates_order if scores.get(c["name"]) is not None]
    n = next_hypothesis_number(polls)
    return f"Hypothèse {n} : " + ", ".join(names)


def find_matching_hypothesis(scores, polls):
    sig = candidate_signature(scores)
    if not sig:
        return None
    for p in polls:
        if candidate_signature(p["scores"]) == sig:
            return p["hypothese"]
    return None


def parse_percent(cell):
    cell = (cell or "").strip()
    if cell in ("", "–", "-", "—"):
        return None
    cell = cell.replace("%", "").replace("<", "").strip()
    try:
        return float(cell)
    except ValueError:
        return None


def parse_sample(cell):
    cell = (cell or "").strip().replace(",", "").replace(".", "")
    if not cell or not cell.isdigit():
        return None
    return int(cell)


def parse_date_range(cell, ref_year_hint=None):
    """
    Convertit une plage de type '9-10 Sep 2026' ou '30 Sep - 1 Oct 2025' en date M/D/YY
    (on retient la date de FIN de la vague, comme dans le reste du jeu de données).
    Retourne None si non reconnu (ex : lignes d'annonce de candidature).
    """
    cell = (cell or "").strip()
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    m = re.search(
        r"(?:\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s*$",
        cell
    )
    if not m:
        return None
    day, mon, year = m.groups()
    mon_num = months.get(mon.lower()[:3])
    if not mon_num:
        return None
    return f"{mon_num}/{int(day)}/{str(year)[2:]}"


def fetch_wikipedia_table():
    """
    Récupère le tout premier tableau de sondages premier tour de la page (le plus
    récent). Utilise pandas.read_html pour profiter de sa gestion des rowspan/colspan.

    Passe par l'API MediaWiki (action=parse) plutôt que par l'URL publique de
    l'article : la page publique passe par un cache de contenu (CDN) qui peut
    renvoyer des versions différentes de la page selon le serveur qui répond,
    ce qui a été observé concrètement sur cette page très éditée. L'API interroge
    directement le cache de rendu de MediaWiki, mis à jour à chaque modification,
    et est donc beaucoup plus fiable pour un usage automatisé comme celui-ci.
    """
    import io
    import pandas as pd

    resp = requests.get(
        WIKI_API_URL,
        params={
            "action": "parse",
            "page": WIKI_PAGE_TITLE,
            "format": "json",
            "formatversion": "2",
            "prop": "text",
        },
        headers={"User-Agent": "Mozilla/5.0 (compatible; poll-update-script/1.0; +github-actions)"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"Erreur API MediaWiki : {payload['error']}")
    html = payload["parse"]["text"]

    # pandas récent exige un objet fichier-like (io.StringIO), pas une chaîne brute,
    # sinon il tente d'interpréter le HTML comme un chemin de fichier.
    tables = pd.read_html(io.StringIO(html))
    if not tables:
        raise RuntimeError("Aucun tableau trouvé sur la page.")

    # On identifie le bon tableau par la PRÉSENCE de colonnes "firm"/"date" plutôt
    # que par un texte exact : plus robuste aux variations de mise en forme Wikipédia
    # (ex. libellé caché dans une infobulle plutôt que dans le texte visible).
    for t in tables:
        cols_lower = [str(c).lower() for c in t.columns]
        has_firm = any("firm" in c for c in cols_lower)
        has_date = any("date" in c or "fieldwork" in c for c in cols_lower)
        if has_firm and has_date:
            # On renomme les colonnes repérées vers des noms fixes, pour que le
            # reste du script n'ait pas à se soucier du libellé exact trouvé.
            rename = {}
            for c in t.columns:
                cl = str(c).lower()
                if "firm" in cl:
                    rename[c] = "Polling firm"
                elif "date" in cl or "fieldwork" in cl:
                    rename[c] = "Fieldwork date"
                elif "sample" in cl:
                    rename[c] = "Sample size"
            return t.rename(columns=rename)

    raise RuntimeError(
        "Aucun tableau de sondages identifiable (colonnes 'firm'/'date' introuvables). "
        f"{len(tables)} tableau(x) trouvé(s) au total, aucun ne correspond."
    )


def main():
    data = load_data()
    known_candidates = data["candidates"]
    known_names = {c["name"] for c in known_candidates}

    try:
        df = fetch_wikipedia_table()
    except Exception as e:
        print(f"::warning::Échec de récupération de la page Wikipédia : {e}")
        sys.exit(0)  # on s'arrête proprement, sans modifier data.json

    header = list(df.columns)
    # Colonnes attendues : "Polling firm", "Fieldwork date", "Sample size", puis un candidat par colonne
    candidate_cols = [c for c in header if c not in ("Polling firm", "Fieldwork date", "Sample size")]

    unknown_names = set()
    new_polls = []
    current_institut, current_date, current_sample = None, None, None

    for _, row in df.iterrows():
        firm = str(row.get("Polling firm", "")).strip()
        field = str(row.get("Fieldwork date", "")).strip()
        sample_raw = str(row.get("Sample size", "")).strip()

        # Ligne d'annonce/événement (peu de colonnes remplies) : on l'ignore
        candidate_values = [row.get(c) for c in candidate_cols]
        non_empty = [v for v in candidate_values if str(v).strip() not in ("", "nan", "–", "-")]
        if len(non_empty) < 2 and not firm:
            continue

        if firm and firm.lower() != "nan":
            current_institut = firm
            current_date = parse_date_range(field)
            current_sample = parse_sample(sample_raw)
        if not current_institut or not current_date:
            continue

        scores = {}
        for c in known_names:
            scores[c] = None
        any_score = False
        for col in candidate_cols:
            mapped = NAME_MAP.get(col.strip())
            if not mapped:
                unknown_names.add(col.strip())
                continue
            val = parse_percent(str(row.get(col, "")))
            if val is not None:
                scores[mapped] = val
                any_score = True
        if not any_score:
            continue

        # Un sondage déjà connu (même institut+date+mêmes candidats testés) ? on saute.
        sig = candidate_signature(scores)
        already_exists = any(
            p["institut"].lower() == current_institut.lower()
            and p["date"] == current_date
            and candidate_signature(p["scores"]) == sig
            for p in (data["polls"] + new_polls)
        )
        if already_exists:
            continue

        bloc_sums = {"BLOC GAUCHE": 0, "CENTRE": 0, "BLOC DROITE": 0}
        bloc_has = {"BLOC GAUCHE": False, "CENTRE": False, "BLOC DROITE": False}
        for c in known_candidates:
            v = scores.get(c["name"])
            if v is not None and c["bloc"] in bloc_sums:
                bloc_sums[c["bloc"]] += v
                bloc_has[c["bloc"]] = True
        final_bloc_sums = {b: round(v, 2) for b, v in bloc_sums.items() if bloc_has[b]}

        hyp = find_matching_hypothesis(scores, data["polls"] + new_polls)
        if not hyp:
            hyp = generate_hypothesis_label(scores, known_candidates, data["polls"] + new_polls)

        new_polls.append({
            "cle": "",
            "hypothese": hyp,
            "institut": current_institut,
            "date": current_date,
            "isReal": True,
            "scores": scores,
            "blocSums": final_bloc_sums,
            "sample": current_sample,
            "registered": None,
            "source": "wikipedia-auto",
        })

    if unknown_names:
        print("::warning::Candidats non reconnus, ignorés (à ajouter dans NAME_MAP si besoin) : "
              + ", ".join(sorted(unknown_names)))

    if not new_polls:
        print("Aucun nouveau sondage détecté.")
        return

    data["polls"].extend(new_polls)
    save_data(data)

    print(f"{len(new_polls)} nouveau(x) sondage(s) ajouté(s) :")
    for p in new_polls:
        print(f"  - {p['institut']} ({p['date']}) — {p['hypothese']}")


if __name__ == "__main__":
    main()
