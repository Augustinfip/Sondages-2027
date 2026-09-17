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
  ce dictionnaire déclenche désormais la création automatique du candidat (nom court
  dérivé + bloc politique deviné via ses voisins de colonne).
- Wikipédia ne donne pas de nom à chaque hypothèse (contrairement à l'Excel d'origine).
  Ce script en génère un automatiquement, avec la même logique que le bouton "Ajouter
  un sondage" de l'outil (reconnaissance par signature de candidats testés).
- Ce script ne gère PAS le second tour (duels), uniquement le premier tour.
- Il ne modifie et ne supprime jamais les sondages existants : il ajoute des lignes
  qui n'existent pas encore (comparaison par institut + date + candidats testés).

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

PARTICLES = {"de", "du", "des", "le", "la", "van", "von"}


def derive_short_name(full_name):
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name.strip()
    surname_parts = [parts[-1]]
    i = len(parts) - 2
    while i >= 0 and parts[i].lower() in PARTICLES:
        surname_parts.insert(0, parts[i])
        i -= 1
    return " ".join(surname_parts)


def infer_bloc_from_neighbors(col_index, ordered_names, candidates_by_name):
    left_bloc = None
    for j in range(col_index - 1, -1, -1):
        nm = ordered_names[j]
        if nm in candidates_by_name:
            left_bloc = candidates_by_name[nm]["bloc"]
            break
    right_bloc = None
    for j in range(col_index + 1, len(ordered_names)):
        nm = ordered_names[j]
        if nm in candidates_by_name:
            right_bloc = candidates_by_name[nm]["bloc"]
            break
    if left_bloc and left_bloc == right_bloc:
        return left_bloc, True
    if left_bloc and not right_bloc:
        return left_bloc, True
    if right_bloc and not left_bloc:
        return right_bloc, True
    if left_bloc and right_bloc:
        return left_bloc, False
    return "CENTRE", False


BLOC_OF = {}


def load_data():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        f.write(text)


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
    if cell.lower() in ("", "–", "-", "—", "nan", "none"):
        return None
    cell = cell.replace("%", "").replace("<", "").strip()
    try:
        val = float(cell)
    except ValueError:
        return None
    if val != val:
        return None
    return val


def parse_sample(cell):
    cell = (cell or "").strip().replace(",", "").replace(".", "")
    if not cell or not cell.isdigit():
        return None
    return int(cell)


def parse_date_range(cell, ref_year_hint=None):
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

    tables = pd.read_html(io.StringIO(html))
    if not tables:
        raise RuntimeError("Aucun tableau trouvé sur la page.")

    for t in tables:
        if isinstance(t.columns, pd.MultiIndex):
            t = t.copy()
            t.columns = [c[0] if isinstance(c, tuple) else c for c in t.columns]
        cols_lower = [str(c).lower() for c in t.columns]
        has_firm = any("firm" in c for c in cols_lower)
        has_date = any("date" in c or "fieldwork" in c for c in cols_lower)
        if has_firm and has_date:
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
        sys.exit(0)

    print(f"[diag] Tableau trouvé : {len(df)} lignes.")
    print(f"[diag] Colonnes : {list(df.columns)}")
    if "Fieldwork date" in df.columns:
        print(f"[diag] 5 premières dates de terrain brutes : {df['Fieldwork date'].head(5).tolist()}")
        parsed = [parse_date_range(str(d)) for d in df["Fieldwork date"].head(5)]
        print(f"[diag] 5 premières dates converties : {parsed}")
    if "Polling firm" in df.columns:
        print(f"[diag] 5 premiers instituts : {df['Polling firm'].head(5).tolist()}")

    header = list(df.columns)
    candidate_cols = [c for c in header if c not in ("Polling firm", "Fieldwork date", "Sample size")]

    candidates_by_name = {c["name"]: c for c in known_candidates}
    resolved_names = []
    newly_created = []
    for col in candidate_cols:
        col_clean = col.strip()
        if col_clean in NAME_MAP and NAME_MAP[col_clean] in candidates_by_name:
            resolved_names.append(NAME_MAP[col_clean])
            continue
        derived = derive_short_name(col_clean)
        if derived in candidates_by_name:
            resolved_names.append(derived)
            continue
        resolved_names.append(derived)

    for idx, col in enumerate(candidate_cols):
        name = resolved_names[idx]
        if name in candidates_by_name:
            continue
        bloc, confident = infer_bloc_from_neighbors(idx, resolved_names, candidates_by_name)
        new_candidate = {"name": name, "bloc": bloc}
        known_candidates.append(new_candidate)
        candidates_by_name[name] = new_candidate
        newly_created.append((col.strip(), name, bloc, confident))

    known_names = {c["name"] for c in known_candidates}

    if newly_created:
        print(f"[info] {len(newly_created)} nouveau(x) candidat(s) créé(s) automatiquement :")
        for wiki_name, name, bloc, confident in newly_created:
            flag = "" if confident else " ⚠️ bloc incertain, à vérifier/reclasser dans l'outil"
            print(f"  - {wiki_name} -> {name} ({bloc}){flag}")

    new_polls = []
    current_institut, current_date, current_sample = None, None, None

    for _, row in df.iterrows():
        firm = str(row.get("Polling firm", "")).strip()
        field = str(row.get("Fieldwork date", "")).strip()
        sample_raw = str(row.get("Sample size", "")).strip()

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
        for idx, col in enumerate(candidate_cols):
            mapped = resolved_names[idx]
            val = parse_percent(str(row.get(col, "")))
            if val is not None:
                scores[mapped] = val
                any_score = True
        if not any_score:
            continue

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

    if not new_polls and not newly_created:
        print("Aucun nouveau sondage détecté.")
        return

    data["polls"].extend(new_polls)
    save_data(data)

    print(f"{len(new_polls)} nouveau(x) sondage(s) ajouté(s) :")
    for p in new_polls:
        print(f"  - {p['institut']} ({p['date']}) — {p['hypothese']}")


if __name__ == "__main__":
    main()
