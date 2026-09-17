"""Extraction of historical TUMexchange acceptance ratings from tabular traffic-light PDFs.

Extracts ratings from:
1. Auswahlquote_TUMexchange_2021_22_GESAMT_deut.pdf (observation years 2019-2022)
2. Auswahlquote_TUMexchange_nach_Unis_eng.pdf (observation years 2017-2018)
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pdfplumber

from .storage import CSV_FIELDS, _university_key

logger = logging.getLogger(__name__)

EXTRACTION_VERSION = "0.1.0-tabular-traffic-light"

# Color definition for 4 traffic-light tiers
# Red: (1.0, 0.0, 0.0) -> tier 1: <=25% (score_percent = 12.5, score_range = '≤ 25%')
# Yellow: (1.0, 1.0, 0.0) -> tier 2: 26-50% (score_percent = 38.0, score_range = '26-50%')
# Light Green: (0.573, 0.816, 0.314) -> tier 3: 51-75% (score_percent = 63.0, score_range = '51-75%')
# Dark Green: (0.0, 0.69, 0.314) -> tier 4: >=76% (score_percent = 88.0, score_range = '≥ 76%')
TIER_SPECS = {
    1: {
        "tier": 1,
        "score_percent": 12.5,
        "score_range": "≤ 25%",
        "bar_fraction": 0.125,
        "notes": "Traffic light quartile tier 1: ≤ 25%",
        "rgb": (1.0, 0.0, 0.0),
    },
    2: {
        "tier": 2,
        "score_percent": 38.0,
        "score_range": "26-50%",
        "bar_fraction": 0.38,
        "notes": "Traffic light quartile tier 2: 26-50%",
        "rgb": (1.0, 1.0, 0.0),
    },
    3: {
        "tier": 3,
        "score_percent": 63.0,
        "score_range": "51-75%",
        "bar_fraction": 0.63,
        "notes": "Traffic light quartile tier 3: 51-75%",
        "rgb": (0.573, 0.816, 0.314),
    },
    4: {
        "tier": 4,
        "score_percent": 88.0,
        "score_range": "≥ 76%",
        "bar_fraction": 0.88,
        "notes": "Traffic light quartile tier 4: ≥ 76%",
        "rgb": (0.0, 0.69, 0.314),
    },
}

COUNTRY_DE_TO_EN = {
    "Argentinien": "Argentina",
    "Australien": "Australia",
    "Brasilien": "Brazil",
    "Chile": "Chile",
    "China": "China",
    "Costa Rica": "Costa Rica",
    "Ecuador": "Ecuador",
    "Ghana": "Ghana",
    "Indien": "India",
    "Indonesien": "Indonesia",
    "Israel": "Israel",
    "Japan": "Japan",
    "Kanada": "Canada",
    "Kolumbien": "Colombia",
    "Korea": "Korea",
    "Kuba": "Cuba",
    "Malaysia": "Malaysia",
    "Marokko": "Morocco",
    "Mexiko": "Mexico",
    "Neuseeland": "New Zealand",
    "Panama": "Panama",
    "Peru": "Peru",
    "Philippinen": "Philippines",
    "Russland": "Russia",
    "Singapur": "Singapore",
    "Südafrika": "South Africa",
    "Südkorea": "Korea",
    "Taiwan": "Taiwan",
    "Thailand": "Thailand",
    "Uganda": "Uganda",
    "USA": "USA",
    "Vietnam": "Vietnam",
    "Ägypten": "Egypt",
}

# Alias resolution mapping normalized raw keys to canonical database keys
ALIASES: dict[str, str] = {
    "pontificia universidad católica argentina": "pontificia universidad católica argentina, santa maría de los buenos aires",
    "instituto technológico de costa rica": "instituto tecnológico de costa rica",
    "instituto technológico y de estudios superiores de monterrey (itesm)": "instituto tecnológico y de estudios superiores de monterrey (itesm)",
    "indian institute of science bangalor (iisc)": "indian institute of science (iisc)",
    "indian institute of technology mandy": "indian institute of technology mandi",
    "technion": "israel institute of technology (technion)",
    "technion - israel institute of technology": "israel institute of technology (technion)",
    "tokyo institute of technology": "institute of science tokyo (formerly tokyo tech)",
    "osaka university": "the university of osaka",
    "postech": "pohang university of science and technology (postech)",
    "national taiwan university of science and technology": "national taiwan university of science and technology (taiwan tech)",
    "georgia tech": "georgia institute of technology (georgia tech)",
    "oklahoma state university": "oklahoma state university - stillwater",
    "university of illinois at urbana- champaign": "university of illinois at urbana-champaign (uiuc)",
    "university of illinois at urbana-champaign": "university of illinois at urbana-champaign (uiuc)",
    "university of minnesota": "university of minnesota, twin cities",
    "university of texas at austin": "university of texas at austin (ut austin)",
    "université du québec à trois-riwières": "université du québec à trois-rivières",
    "université du québec à trois-riviéres": "université du québec à trois-rivières",
    "vietnam national university of hanoi – university of science": "vietnam national university of hanoi - university of science",
    "the chinese university of hong kong – shenzhen": "the chinese university of hong kong - shenzhen",
    "pontifica universidad católica de chile": "pontificia universidad católica de chile",
    "universidade estadual paulista „júlio de mesquita filho”": "universidade estadual paulista 'júlio de mesquita filho' (unesp)",
    "universidade estadual paulista júlio de mesquita filho (unesp)": "universidade estadual paulista 'júlio de mesquita filho' (unesp)",
    "universidade federal de santa catarina": "universidade federal de santa catarina (ufsc)",
    "universidade de são paulo": "universidade de são paulo (usp)",
    # Historical universities harmonized between German and English releases
    "m.v. lomonosov moscow state university": "lomonosov moscow state university",
    "moscow aviation institute (mai)": "moscow aviation institute (state university of aerospace technologies)",
    "moscow state technical university n.e. bauman (mstu)": "bauman moscow state technical university",
    "st. petersburg state polytechnic university": "peter the great st. petersburg polytechnic university",
    "state university st. petersburg": "saint-petersburg state university",
    "rutgers university": "rutgers, the state university of new jersey",
    "university of california, berkley": "university of california, berkeley",
}

# Canonical names for historical partner universities discontinued before 2026/2027 releases
HISTORICAL_CANONICAL: dict[str, tuple[str, str]] = {
    "lomonosov moscow state university": ("Lomonosov Moscow State University", "Russia"),
    "moscow aviation institute (state university of aerospace technologies)": (
        "Moscow Aviation Institute (State University of Aerospace Technologies)",
        "Russia",
    ),
    "bauman moscow state technical university": ("Bauman Moscow State Technical University", "Russia"),
    "peter the great st. petersburg polytechnic university": (
        "Peter the Great St. Petersburg Polytechnic University",
        "Russia",
    ),
    "saint-petersburg state university": ("Saint-Petersburg State University", "Russia"),
    "rutgers, the state university of new jersey": ("Rutgers, The State University of New Jersey", "USA"),
    "university of california, berkeley": ("University of California, Berkeley", "USA"),
}

TABULAR_CSV_FIELDS = CSV_FIELDS + ["tier", "score_range", "raw_country_text"]


def classify_traffic_light_color(color: Sequence[float] | None) -> int | None:
    """Classify RGB color tuple to traffic-light quartile tier (1 to 4)."""
    if not color or len(color) < 3:
        return None
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    # Tier 1 (Red): (1.0, 0.0, 0.0)
    if abs(r - 1.0) < 0.15 and abs(g - 0.0) < 0.15 and abs(b - 0.0) < 0.15:
        return 1
    # Tier 2 (Yellow): (1.0, 1.0, 0.0)
    if abs(r - 1.0) < 0.15 and abs(g - 1.0) < 0.15 and abs(b - 0.0) < 0.15:
        return 2
    # Tier 3 (Light Green): (0.573, 0.816, 0.314)
    if abs(r - 0.573) < 0.15 and abs(g - 0.816) < 0.15 and abs(b - 0.314) < 0.15:
        return 3
    # Tier 4 (Dark Green): (0.0, 0.69, 0.314)
    if abs(r - 0.0) < 0.15 and abs(g - 0.69) < 0.15 and abs(b - 0.314) < 0.15:
        return 4
    return None


def load_canonical_map(sqlite_path: Path | str | None = None) -> dict[str, tuple[str, str]]:
    """Load canonical university names and countries from combined bars.sqlite."""
    if sqlite_path is None:
        sqlite_path = Path("data/output/combined/bars.sqlite")
    path = Path(sqlite_path)
    if not path.is_file():
        logger.warning("Combined sqlite database not found at %s", path)
        return {}

    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """
            SELECT university_key, university, country, source_release_year
            FROM observations
            ORDER BY source_release_year DESC
            """
        ).fetchall()
        canon_map: dict[str, tuple[str, str]] = {}
        for key, university, country, _ in rows:
            if key not in canon_map:
                canon_map[str(key)] = (str(university), str(country))
        return canon_map
    finally:
        connection.close()


def resolve_university(
    raw_uni: str,
    raw_country: str,
    canon_map: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Resolve university name and country to canonical form."""
    k = _university_key(raw_uni)
    if k in ALIASES:
        k = _university_key(ALIASES[k])

    if canon_map and k in canon_map:
        return canon_map[k]

    if k in HISTORICAL_CANONICAL:
        return HISTORICAL_CANONICAL[k]

    # Clean punctuation quotes for fuzzy key match
    clean_k = k.replace("„", "").replace("”", "").replace('"', "").replace("'", "")
    if canon_map:
        for db_k, (db_u, db_c) in canon_map.items():
            db_clean = db_k.replace("„", "").replace("”", "").replace('"', "").replace("'", "")
            if clean_k == db_clean:
                return (db_u, db_c)

    country_en = COUNTRY_DE_TO_EN.get(raw_country, raw_country)
    return (raw_uni, country_en)


@dataclass(frozen=True)
class DocumentConfig:
    source_filename: str
    source_release_year: int
    language: str
    country_col: tuple[float, float]
    uni_col: tuple[float, float]
    year_cols: list[tuple[float, float, str, int]]  # (x0, x1, column_label, year)


CONFIG_2022_DE = DocumentConfig(
    source_filename="Auswahlquote_TUMexchange_2021_22_GESAMT_deut.pdf",
    source_release_year=2022,
    language="de",
    country_col=(27.4, 98.0),
    uni_col=(98.0, 238.5),
    year_cols=[
        (238.5, 320.6, "2018/19", 2019),
        (320.6, 403.1, "2019/20", 2020),
        (403.1, 485.2, "2020/21", 2021),
        (485.2, 567.3, "2021/22", 2022),
    ],
)

CONFIG_2018_EN = DocumentConfig(
    source_filename="Auswahlquote_TUMexchange_nach_Unis_eng.pdf",
    source_release_year=2018,
    language="en",
    country_col=(70.9, 148.5),
    uni_col=(148.5, 353.7),
    year_cols=[
        (353.7, 438.8, "2017/18", 2018),
        (438.8, 524.4, "2016/17", 2017),
    ],
)


def detect_document_config(pdf_path: Path | str) -> DocumentConfig:
    """Detect whether PDF is the 2021/22 German or the older English tabular document."""
    path = Path(pdf_path)
    name = path.name.lower()
    if "2021_22" in name or "deut" in name:
        return CONFIG_2022_DE
    if "nach_unis_eng" in name or "eng" in name:
        return CONFIG_2018_EN

    # Fallback to inspecting geometry of first page
    with pdfplumber.open(path) as pdf:
        v_lines = [r for r in pdf.pages[0].rects if r["width"] < 1.0]
        x_positions = [r["x0"] for r in v_lines]
        if any(x < 50.0 for x in x_positions):
            return CONFIG_2022_DE
        return CONFIG_2018_EN


def extract_tabular_pdf(
    pdf_path: Path | str,
    canon_map: Mapping[str, tuple[str, str]] | None = None,
    config: DocumentConfig | None = None,
) -> list[dict[str, Any]]:
    """Extract all historical observations from a single tabular traffic-light PDF."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF file not found: {path}")

    if config is None:
        config = detect_document_config(path)

    data = path.read_bytes()
    source_sha256 = hashlib.sha256(data).hexdigest()
    source_document_id = f"sha256:{source_sha256[:16]}"

    records: list[dict[str, Any]] = []
    current_country = ""
    global_row_index = 0

    with pdfplumber.open(path) as pdf:
        for p_idx, page in enumerate(pdf.pages):
            page_num = p_idx + 1
            # Horizontal boundaries of table rows
            h_lines = [r for r in page.rects if r["height"] < 1.0 and r["width"] > 50]
            y_tops = sorted(list(set(round(r["top"], 1) for r in h_lines)))
            if len(y_tops) < 2:
                continue

            words = page.extract_words()
            curves = page.curves

            for i in range(len(y_tops) - 1):
                y0, y1 = y_tops[i], y_tops[i + 1]

                # Words inside country column
                c0_words = [
                    w["text"]
                    for w in words
                    if config.country_col[0] <= (w["x0"] + w["x1"]) / 2 < config.country_col[1]
                    and y0 <= (w["top"] + w["bottom"]) / 2 < y1
                ]
                # Words inside university column
                c1_words = [
                    w["text"]
                    for w in words
                    if config.uni_col[0] <= (w["x0"] + w["x1"]) / 2 < config.uni_col[1]
                    and y0 <= (w["top"] + w["bottom"]) / 2 < y1
                ]

                c0 = " ".join(c0_words).strip()
                c1 = " ".join(c1_words).strip()

                # Header rows check
                c1_lower = c1.lower()
                if "partneruniversit" in c1_lower or "partner universit" in c1_lower or "selection" in c0.lower():
                    continue

                # Country header section
                if c0 and not c1:
                    if "Land" not in c0 and "Country" not in c0:
                        current_country = c0
                    continue

                if not c1:
                    continue

                # If country is present on the same row, update current_country
                if c0 and "Land" not in c0 and "Country" not in c0:
                    current_country = c0

                global_row_index += 1
                canonical_uni, canonical_country = resolve_university(c1, current_country, canon_map)

                # Process each year column
                for cx0, cx1, column_label, year in config.year_cols:
                    col_words = [
                        w["text"]
                        for w in words
                        if cx0 <= (w["x0"] + w["x1"]) / 2 < cx1 and y0 <= (w["top"] + w["bottom"]) / 2 < y1
                    ]
                    cell_text = " ".join(col_words).strip()

                    # Find filled curves in this cell
                    cell_curves = [
                        c
                        for c in curves
                        if cx0 <= (c["x0"] + c["x1"]) / 2 < cx1
                        and y0 <= (c["top"] + c["bottom"]) / 2 < y1
                        and c.get("fill")
                    ]

                    tier = None
                    active_curve = None
                    if cell_curves:
                        for curve in cell_curves:
                            color = curve.get("non_stroking_color") or curve.get("fill_color")
                            classified = classify_traffic_light_color(color)
                            if classified is not None:
                                tier = classified
                                active_curve = curve
                                break

                    row_id = f"{source_document_id}:{global_row_index}:{year}"

                    if tier is not None:
                        spec = TIER_SPECS[tier]
                        bar_bbox = [
                            round(active_curve["x0"], 2),
                            round(active_curve["top"], 2),
                            round(active_curve["x1"], 2),
                            round(active_curve["bottom"], 2),
                        ]
                        record = {
                            "row_id": row_id,
                            "source_document_id": source_document_id,
                            "source_sha256": source_sha256,
                            "source_filename": config.source_filename,
                            "source_release_year": config.source_release_year,
                            "country": canonical_country,
                            "university": canonical_uni,
                            "year": year,
                            "page": page_num,
                            "row_index": global_row_index,
                            "status": "measured",
                            "score_percent": spec["score_percent"],
                            "tier": tier,
                            "score_range": spec["score_range"],
                            "bar_fraction": spec["bar_fraction"],
                            "raw_width_pt": None,
                            "full_width_pt": None,
                            "calibration_id": None,
                            "bar_bbox": bar_bbox,
                            "notes": spec["notes"],
                            "is_latest_roster": False,
                            "extraction_version": EXTRACTION_VERSION,
                            "score_basis": "traffic_light",
                            "status_box_bbox": None,
                            "status_box_width_pt": None,
                            "raw_label": c1,
                            "raw_university_text": c1,
                            "raw_country_text": current_country,
                            "column_label": column_label,
                            "units": "pt",
                            "geometry": {
                                "coordinate_system": "pdfplumber top-left page coordinates",
                                "tier": tier,
                                "circle_bbox": bar_bbox,
                                "fill_color": list(spec["rgb"]),
                            },
                        }
                    else:
                        notes_label = f"No active exchange: {cell_text}" if cell_text else "No active exchange"
                        record = {
                            "row_id": row_id,
                            "source_document_id": source_document_id,
                            "source_sha256": source_sha256,
                            "source_filename": config.source_filename,
                            "source_release_year": config.source_release_year,
                            "country": canonical_country,
                            "university": canonical_uni,
                            "year": year,
                            "page": page_num,
                            "row_index": global_row_index,
                            "status": "no_places",
                            "score_percent": None,
                            "tier": None,
                            "score_range": None,
                            "bar_fraction": None,
                            "raw_width_pt": None,
                            "full_width_pt": None,
                            "calibration_id": None,
                            "bar_bbox": None,
                            "notes": notes_label,
                            "is_latest_roster": False,
                            "extraction_version": EXTRACTION_VERSION,
                            "score_basis": "not_available",
                            "status_box_bbox": None,
                            "status_box_width_pt": None,
                            "raw_label": c1,
                            "raw_university_text": c1,
                            "raw_country_text": current_country,
                            "column_label": column_label,
                            "units": "pt",
                            "geometry": None,
                        }

                    records.append(record)

    return records


def write_tabular_outputs(
    records: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> dict[str, Path]:
    """Write records.json and rows.csv to destination directory."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "records.json"
    csv_path = out_dir / "rows.csv"

    # Write records.json
    payload = {
        "schema_version": "0.2",
        "extraction_version": EXTRACTION_VERSION,
        "documents": list(documents),
        "record_count": len(records),
        "records": list(records),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Write rows.csv
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TABULAR_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row_dict = {}
            for field in TABULAR_CSV_FIELDS:
                val = record.get(field)
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
                row_dict[field] = val
            writer.writerow(row_dict)

    return {"json": json_path, "csv": csv_path}


def extract_all_tabular(
    pdf_paths: Sequence[Path | str] | None = None,
    output_dir: Path | str = "data/output/tabular",
    sqlite_path: Path | str = "data/output/combined/bars.sqlite",
) -> dict[str, Any]:
    """Extract from all tabular PDFs and produce combined records.json and rows.csv."""
    if pdf_paths is None:
        pdf_paths = [
            Path("data/source/downloaded/Auswahlquote_TUMexchange_2021_22_GESAMT_deut.pdf"),
            Path("data/source/downloaded/Auswahlquote_TUMexchange_nach_Unis_eng.pdf"),
        ]

    canon_map = load_canonical_map(sqlite_path)
    all_records: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []

    for pdf_path in pdf_paths:
        path = Path(pdf_path)
        doc_records = extract_tabular_pdf(path, canon_map=canon_map)
        all_records.extend(doc_records)
        first = doc_records[0] if doc_records else {}
        documents.append(
            {
                "source_document_id": first.get("source_document_id"),
                "source_sha256": first.get("source_sha256"),
                "source_filename": first.get("source_filename"),
                "source_release_year": first.get("source_release_year"),
                "record_count": len(doc_records),
            }
        )

    out_paths = write_tabular_outputs(all_records, documents, output_dir)

    # Summary statistics
    unis = set(r["university"] for r in all_records)
    years = sorted(set(r["year"] for r in all_records))
    measured = sum(1 for r in all_records if r["status"] == "measured")
    no_places = sum(1 for r in all_records if r["status"] == "no_places")
    tier_counts = {t: sum(1 for r in all_records if r.get("tier") == t) for t in (1, 2, 3, 4)}

    summary = {
        "output_dir": str(Path(output_dir).resolve()),
        "files": {k: str(v.resolve()) for k, v in out_paths.items()},
        "total_records": len(all_records),
        "total_distinct_universities": len(unis),
        "years": years,
        "measured_count": measured,
        "no_places_count": no_places,
        "tier_counts": tier_counts,
        "documents": documents,
    }
    return summary


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Extract historical TUMexchange ratings from tabular PDFs")
    parser.add_argument(
        "--output",
        "-o",
        default="data/output/tabular",
        help="output directory for records.json and rows.csv (default: data/output/tabular)",
    )
    parser.add_argument(
        "--database",
        "-d",
        default="data/output/combined/bars.sqlite",
        help="path to canonical combined bars.sqlite",
    )
    parser.add_argument(
        "pdfs",
        nargs="*",
        help="PDF paths to extract (default: standard downloaded tabular PDFs)",
    )
    args = parser.parse_args()

    pdf_paths = [Path(p) for p in args.pdfs] if args.pdfs else None
    summary = extract_all_tabular(
        pdf_paths=pdf_paths,
        output_dir=args.output,
        sqlite_path=args.database,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main()
