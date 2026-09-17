"""Extractor and cross-validator for exact 2016/17 TUMexchange admission percentages.

Extracts numerical admission rates from
`data/source/downloaded/Nach_UNIs_Auswahlquote_TUMexchange.pdf`, matches
them against canonical university profiles from `bars.sqlite`, and cross-validates
the results against the visual color tiers in
`data/source/downloaded/Auswahlquote_TUMexchange_nach_Unis_eng.pdf`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pdfplumber

from tumexchange.storage import write_records_json, write_rows_csv

LOGGER = logging.getLogger(__name__)

DEFAULT_SOURCE_PDF = Path("data/source/downloaded/Nach_UNIs_Auswahlquote_TUMexchange.pdf")
DEFAULT_VALIDATION_PDF = Path("data/source/downloaded/Auswahlquote_TUMexchange_nach_Unis_eng.pdf")
DEFAULT_CANONICAL_DB = Path("data/output/combined/bars.sqlite")
DEFAULT_OUTPUT_DIR = Path("data/output/exact_2016")

EXTRACTION_VERSION = "0.2.0-exact-percentage"
RELEASE_YEAR = 2017
OBSERVATION_YEAR = 2017

GERMAN_COUNTRY_MAP: dict[str, str] = {
    "Argentinien": "Argentina",
    "Australien": "Australia",
    "Brasilien": "Brazil",
    "Chile": "Chile",
    "China": "China",
    "Costa Rica": "Costa Rica",
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
    "Philippinen": "Philippines",
    "Russland": "Russia",
    "Singapur": "Singapore",
    "Südafrika": "South Africa",
    "Taiwan": "Taiwan",
    "Thailand": "Thailand",
    "USA": "USA",
}

# Mapping normalized, accent-stripped 2016 PDF names to target names/keys in bars.sqlite
EXPLICIT_2016_ALIASES: dict[str, str] = {
    "instituto tecnologico de buenos aires": "instituto tecnologico de buenos aires (itba)",
    "pontifica universidad catolica de chile": "pontificia universidad catolica de chile",
    "instituto technologico de costa rica": "instituto tecnologico de costa rica",
    "indian institute of science bangalor (iisc)": "indian institute of science (iisc)",
    "indian institute of technology mandy": "indian institute of technology mandi (iit mandi)",
    "technion": "israel institute of technology (technion)",
    "osaka university": "the university of osaka",
    "the university of tokyo - graduate school of information science": (
        "the university of tokyo (graduate school of information, science and technology)"
    ),
    "the university of tokyo - graduate school of engineering": (
        "the university of tokyo (graduate school of engineering)"
    ),
    "toyko institute of technology": "institute of science tokyo (formerly tokyo tech)",
    "universidad nacional de columbia": "universidad nacional de colombia",
    "universidad de antioguia": "universidad de antioquia",
    "postech": "pohang university of science and technology",
    "instituto technologico y de estudios superiores de monterrey (itesm)": (
        "instituto tecnologico y de estudios superiores de monterrey"
    ),
    "national taiwan university of science and technology": (
        "national taiwan university of science and technology (taiwan tech)"
    ),
    "georgia tech": "georgia institute of technology (georgia tech)",
    "university of texas at austin": "university of texas at austin (ut austin)",
    "university of minnesota": "university of minnesota, twin cities",
    "oklahoma state university": "oklahoma state university - stillwater",
    "university of illinois at urbana- champaign": "university of illinois at urbana-champaign",
    "university of illinois at urbana-champaign": "university of illinois at urbana-champaign",
    "rmit vietnam": "rmit international university vietnam",
}

# Cross-validation color mapping from PDF non-stroking tuple to semantic tier
COLOR_TIER_TUPLES: list[tuple[tuple[float, float, float], str]] = [
    ((1.0, 0.0, 0.0), "Red"),
    ((1.0, 1.0, 0.0), "Yellow"),
    ((0.573, 0.816, 0.314), "Light Green"),
    ((0.0, 0.69, 0.314), "Dark Green"),
]


def clean_text(text: Any) -> str:
    """Normalize unicode spaces and characters."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(normalized.split()).strip()


def strip_accents(text: str) -> str:
    """Fold accents and diacritics for robust matching."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def compute_sha256(path: Path | str) -> str:
    """Compute standard SHA-256 checksum for a local file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_canonical_universities(db_path: Path | str) -> dict[str, dict[str, str]]:
    """Load canonical university representations from SQLite database."""
    path = Path(db_path)
    if not path.is_file():
        LOGGER.warning("Canonical DB not found at %s", path)
        return {}

    conn = sqlite3.connect(path)
    try:
        cursor = conn.cursor()
        query = (
            "SELECT country, university, university_key, source_release_year, year "
            "FROM observations"
        )
        rows = cursor.execute(query).fetchall()
    finally:
        conn.close()

    groups: dict[str, list[tuple[int, int, str, str]]] = {}
    for c, u, k, ry, y in rows:
        if k not in groups:
            groups[k] = []
        groups[k].append((ry or 0, y or 0, c, u))

    canonical_map: dict[str, dict[str, str]] = {}
    for k, items in groups.items():
        items.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best = items[0]
        canonical_map[k] = {
            "country": best[2],
            "university": best[3],
            "university_key": k,
        }

    return canonical_map


def match_university_canonical(
    raw_name: str,
    canonical_map: Mapping[str, Mapping[str, str]],
    raw_country: str | None = None,
) -> tuple[str, str]:
    """Match a raw extracted name to canonical country and university."""
    clean_n = clean_text(raw_name)
    folded = strip_accents(clean_n.lower())

    # 1. Check explicit alias lookup
    target = EXPLICIT_2016_ALIASES.get(folded)
    if target:
        target_folded = strip_accents(target.lower())
        for k, v in canonical_map.items():
            if strip_accents(k) == target_folded or strip_accents(v["university"].lower()) == target_folded:
                return v["country"], v["university"]

    # 2. Check canonical keys and names
    for k, v in canonical_map.items():
        if strip_accents(k) == folded:
            return v["country"], v["university"]
        if strip_accents(v["university"].lower()) == folded:
            return v["country"], v["university"]

    # 3. Check stripped acronyms e.g. (ITB), (IISc), (UFPR)
    no_acronym = re.sub(r"\s*\([A-Za-z0-9&.+-]{2,16}\)\s*$", "", clean_n).strip()
    no_acronym_folded = strip_accents(no_acronym.lower())
    for k, v in canonical_map.items():
        k_clean = re.sub(r"\s*\([A-Za-z0-9&.+-]{2,16}\)\s*$", "", k).strip()
        if strip_accents(k_clean) == no_acronym_folded:
            return v["country"], v["university"]
        v_clean = re.sub(r"\s*\([A-Za-z0-9&.+-]{2,16}\)\s*$", "", v["university"]).strip()
        if strip_accents(v_clean.lower()) == no_acronym_folded:
            return v["country"], v["university"]

    # Fallback to cleaned original name and mapped English country
    fallback_country = GERMAN_COUNTRY_MAP.get(raw_country or "", raw_country or "")
    return fallback_country, clean_n


def extract_exact_percentages(
    pdf_path: Path | str = DEFAULT_SOURCE_PDF,
    canonical_db_path: Path | str | None = DEFAULT_CANONICAL_DB,
    include_unmeasured: bool = False,
) -> list[dict[str, Any]]:
    """Extract exact decimal admission rates from the 2016/17 PDF document."""
    pdf_file = Path(pdf_path)
    if not pdf_file.is_file():
        raise FileNotFoundError(f"Source PDF does not exist: {pdf_file}")

    canonical_map = load_canonical_universities(canonical_db_path) if canonical_db_path else {}
    sha256_hash = compute_sha256(pdf_file)
    source_doc_id = f"sha256:{sha256_hash[:16]}"

    records: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_file) as pdf:
        current_country: str | None = None
        for page_idx, page in enumerate(pdf.pages):
            page_number = page_idx + 1
            tables = page.extract_tables()
            row_index_on_page = 0

            for table in tables:
                for row in table:
                    if not row or not any(row):
                        continue

                    col0 = clean_text(row[0]) if len(row) > 0 else ""
                    col1 = clean_text(row[1]) if len(row) > 1 else ""
                    col2 = clean_text(row[2]) if len(row) > 2 else ""

                    # Skip table header rows
                    if "partneruniversitat" in strip_accents(col1.lower()) or "auswahlquote" in strip_accents(col2.lower()) or col0 == "Land":
                        continue

                    # Country grouping line
                    if col0 and not col1:
                        current_country = col0
                        continue

                    if not col1:
                        continue

                    row_index_on_page += 1
                    raw_uni = col1
                    raw_pct = col2

                    is_dash = (raw_pct == "-" or not raw_pct)
                    if is_dash and not include_unmeasured:
                        continue

                    canonical_country, canonical_uni = match_university_canonical(
                        raw_uni, canonical_map, raw_country=current_country
                    )

                    row_id = f"{source_doc_id}:{page_number}:{row_index_on_page}:{OBSERVATION_YEAR}"

                    if not is_dash:
                        num_str = raw_pct.replace("%", "").replace(",", ".").strip()
                        score_percent = float(num_str)
                        bar_fraction = round(score_percent / 100.0, 6)
                        status = "measured"
                        score_basis = "exact_percentage"
                        notes = "Exact numerical percentage extracted from PDF table"
                    else:
                        score_percent = None
                        bar_fraction = None
                        status = "no_cooperation"
                        score_basis = "unmeasured"
                        notes = "No active exchange reported in source table (-)"

                    record: dict[str, Any] = {
                        "row_id": row_id,
                        "source_document_id": source_doc_id,
                        "source_sha256": sha256_hash,
                        "source_filename": pdf_file.name,
                        "source_release_year": RELEASE_YEAR,
                        "extraction_version": EXTRACTION_VERSION,
                        "country": canonical_country,
                        "university": canonical_uni,
                        "year": OBSERVATION_YEAR,
                        "page": page_number,
                        "row_index": row_index_on_page,
                        "status": status,
                        "score_percent": score_percent,
                        "score_basis": score_basis,
                        "bar_fraction": bar_fraction,
                        "raw_width_pt": None,
                        "full_width_pt": None,
                        "calibration_id": None,
                        "bar_bbox": None,
                        "status_box_bbox": None,
                        "status_box_width_pt": None,
                        "notes": notes,
                        "raw_label": raw_uni,
                        "raw_university_text": raw_uni,
                        "column_label": "Auswahlquote 2016/17",
                        "units": "percent",
                        "geometry": None,
                    }
                    records.append(record)

    return records


def tier_for_percentage(score_percent: float) -> str:
    """Map admission percentage to TUM color tier category.

    Tiers:
    - <= 25%: Red
    - 26-50%: Yellow
    - 51-75%: Light Green
    - >= 76%: Dark Green
    """
    if score_percent <= 25.0:
        return "Red"
    if score_percent <= 50.0:
        return "Yellow"
    if score_percent <= 75.0:
        return "Light Green"
    return "Dark Green"


def color_to_tier(color: Any) -> str | None:
    """Map an extracted RGB color tuple to semantic tier name."""
    if not isinstance(color, tuple) or len(color) < 3:
        return None
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    for target, name in COLOR_TIER_TUPLES:
        tr, tg, tb = target
        if abs(r - tr) < 0.05 and abs(g - tg) < 0.05 and abs(b - tb) < 0.05:
            return name
    return None


def extract_validation_tiers(eng_pdf_path: Path | str = DEFAULT_VALIDATION_PDF) -> list[dict[str, Any]]:
    """Extract partner universities and their 2016/17 color tiers from the English PDF."""
    pdf_file = Path(eng_pdf_path)
    if not pdf_file.is_file():
        raise FileNotFoundError(f"Validation PDF does not exist: {pdf_file}")

    eng_rows: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_file) as pdf:
        current_country: str | None = None
        for page_idx, page in enumerate(pdf.pages):
            page_number = page_idx + 1
            tables = page.find_tables()
            if not tables:
                continue

            for row in tables[0].rows:
                c0 = clean_text(page.crop(row.cells[0]).extract_text())
                c1 = clean_text(page.crop(row.cells[1]).extract_text())

                if "partner university" in strip_accents(c1.lower()) or "country" in strip_accents(c0.lower()):
                    continue
                if c0 and not c1:
                    current_country = c0
                    continue
                if not c1:
                    continue

                cell3_crop = page.crop(row.cells[3])
                c3_text = clean_text(cell3_crop.extract_text())
                filled_curves = [c for c in cell3_crop.curves if c.get("fill")]

                tier = None
                if filled_curves:
                    tier = color_to_tier(filled_curves[0].get("non_stroking_color"))
                elif "--" in c3_text or "-" in c3_text:
                    tier = "Dash"
                else:
                    tier = "Empty"

                eng_rows.append({
                    "country": current_country,
                    "university": c1,
                    "tier_2016": tier or "Unknown",
                    "page": page_number,
                })

    return eng_rows


@dataclass
class CrossValidationSummary:
    total_exact_records: int
    matched_in_english: int
    concordant_count: int
    concordance_rate_pct: float
    discordant_count: int
    not_in_english_count: int
    discordant_records: list[dict[str, Any]]
    not_in_english_records: list[dict[str, Any]]


def cross_validate(
    records: Sequence[Mapping[str, Any]],
    eng_pdf_path: Path | str = DEFAULT_VALIDATION_PDF,
) -> CrossValidationSummary:
    """Cross-validate exact numerical percentages against visual tiers in the English PDF."""
    eng_rows = extract_validation_tiers(eng_pdf_path)

    # Index English rows by normalized key
    eng_by_key: dict[str, dict[str, Any]] = {}
    for item in eng_rows:
        key = strip_accents(item["university"].lower())
        eng_by_key[key] = item

    # Specific DE to EN mappings for minor spelling or grouping differences
    DE_TO_EN_NAME_MAP = {
        "toyko institute of technology": "tokyo institute of technology",
        "universidad nacional de columbia": "universidad nacional de colombia",
        "universidad de antioguia": "universidad de antioquia",
        "the university of tokyo - graduate school of information science": "the university of tokyo",
        "the university of tokyo - graduate school of engineering": "the university of tokyo",
        "universidade estadual paulista „julio de mesquita filho": "universidade estadual paulista „julio de mesquita filho”",
        "universite du quebec a trois- rivieres": "universite du quebec a trois-rivieres",
        "university of california": "university of california, berkley",
    }

    concordant: list[dict[str, Any]] = []
    discordant: list[dict[str, Any]] = []
    not_in_eng: list[dict[str, Any]] = []

    for rec in records:
        raw_name = rec.get("raw_label") or rec.get("university") or ""
        clean_n = clean_text(raw_name)
        key = strip_accents(clean_n.lower())

        target_key = DE_TO_EN_NAME_MAP.get(key, key)
        eng_item = eng_by_key.get(target_key)

        if not eng_item:
            # Try matching without parenthetical acronyms
            no_parens = re.sub(r"\s*\([^)]*\)", "", key).strip()
            for ek, ev in eng_by_key.items():
                ev_no_p = re.sub(r"\s*\([^)]*\)", "", ek).strip()
                if no_parens == ev_no_p:
                    eng_item = ev
                    break

        if not eng_item:
            not_in_eng.append({
                "university": clean_n,
                "score_percent": rec.get("score_percent"),
                "country": rec.get("country"),
                "page": rec.get("page"),
            })
            continue

        score = rec.get("score_percent")
        if score is None:
            continue

        expected_tier = tier_for_percentage(float(score))
        actual_tier = eng_item["tier_2016"]

        if expected_tier == actual_tier:
            concordant.append({
                "university": clean_n,
                "score_percent": score,
                "expected_tier": expected_tier,
                "actual_tier": actual_tier,
            })
        else:
            discordant.append({
                "university_de": clean_n,
                "university_en": eng_item["university"],
                "score_percent": score,
                "expected_tier": expected_tier,
                "actual_tier": actual_tier,
            })

    matched = len(concordant) + len(discordant)
    rate = round((len(concordant) / matched) * 100.0, 2) if matched > 0 else 0.0

    return CrossValidationSummary(
        total_exact_records=len(records),
        matched_in_english=matched,
        concordant_count=len(concordant),
        concordance_rate_pct=rate,
        discordant_count=len(discordant),
        not_in_english_count=len(not_in_eng),
        discordant_records=discordant,
        not_in_english_records=not_in_eng,
    )


def write_exact_2016_outputs(
    records: Sequence[Mapping[str, Any]],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    """Write extracted records to records.json and rows.csv in the specified directory."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records_file = out_dir / "records.json"
    rows_file = out_dir / "rows.csv"

    first = records[0] if records else {}
    calibration = {
        "calibration_id": "exact_percentage:source_text",
        "source_document_id": first.get("source_document_id"),
        "source_sha256": first.get("source_sha256"),
        "source_filename": first.get("source_filename"),
        "source_release_year": first.get("source_release_year", RELEASE_YEAR),
        "extraction_version": first.get("extraction_version", EXTRACTION_VERSION),
        "method": "exact_table_text",
        "formula": "percentage",
        "units": "percent",
    }

    write_records_json(records, calibration, records_file)
    write_rows_csv(records, rows_file)

    return records_file, rows_file


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Extract exact 2016/17 admission percentages.")
    parser.add_argument(
        "--source-pdf",
        type=Path,
        default=DEFAULT_SOURCE_PDF,
        help="Path to Nach_UNIs_Auswahlquote_TUMexchange.pdf",
    )
    parser.add_argument(
        "--validation-pdf",
        type=Path,
        default=DEFAULT_VALIDATION_PDF,
        help="Path to Auswahlquote_TUMexchange_nach_Unis_eng.pdf",
    )
    parser.add_argument(
        "--canonical-db",
        type=Path,
        default=DEFAULT_CANONICAL_DB,
        help="Path to bars.sqlite for canonical university matching",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for records.json and rows.csv",
    )

    args = parser.parse_args()

    LOGGER.info("Extracting exact numerical rates from %s...", args.source_pdf)
    records = extract_exact_percentages(
        pdf_path=args.source_pdf,
        canonical_db_path=args.canonical_db,
    )
    LOGGER.info("Extracted %d exact percentage records.", len(records))

    LOGGER.info("Writing outputs to %s...", args.output_dir)
    json_path, csv_path = write_exact_2016_outputs(records, args.output_dir)
    LOGGER.info("Wrote %s and %s", json_path, csv_path)

    LOGGER.info("Cross-validating against visual tiers in %s...", args.validation_pdf)
    summary = cross_validate(records, args.validation_pdf)

    print("\n" + "=" * 60)
    print("EXACT 2016/17 EXTRACTION & CROSS-VALIDATION SUMMARY")
    print("=" * 60)
    print(f"Total extracted exact records: {summary.total_exact_records}")
    print(f"Matched in English validation PDF: {summary.matched_in_english}")
    print(f"Concordant with visual tier: {summary.concordant_count} ({summary.concordance_rate_pct}%)")
    print(f"Discordant with visual tier: {summary.discordant_count}")
    print(f"Not found in English PDF: {summary.not_in_english_count}")
    print("\nDiscordant details:")
    for d in summary.discordant_records:
        print(f"  * {d['university_de']} ({d['score_percent']}%): expected {d['expected_tier']} vs actual {d['actual_tier']}")
    print("=" * 60 + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
