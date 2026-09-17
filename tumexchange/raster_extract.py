"""Extractor and cross-validator for TUMexchange 2025 raster bar charts.

Extracts admission chance data from `data/source/downloaded/TUMexchange_Chances_2025.pdf`
for observation years 2022, 2023, and 2024 using visual pixel bar measurement,
calibrated against Brazil reference universities for year 2022.
Cross-validates overlapping observation years (2023, 2024) against Release 2026.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

from tumexchange.storage import (
    _university_key,
    write_records_json,
    write_rows_csv,
    write_sqlite,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_SOURCE_PDF = Path("data/source/downloaded/TUMexchange_Chances_2025.pdf")
DEFAULT_IMAGES_DIR = Path("data/source/downloaded/2025_images")
DEFAULT_CANONICAL_DB = Path("data/output/combined/bars.sqlite")
DEFAULT_2026_RECORDS = Path("data/output/2026_v02/records.json")
DEFAULT_OUTPUT_DIR = Path("data/output/2025")

EXTRACTION_VERSION = "0.1.0-raster-bar-calibration"
RELEASE_YEAR = 2025

# Calibrated column horizontal bounds and full-bar reference widths
# Calibration anchor: Brazil universities 2022 (UNICAMP, UNESP, UFSC, UFRJ full width = 292.0 px)
CALIBRATION_SPEC = {
    "source_release_year": 2025,
    "method": "raster_brazil_anchor",
    "reference_university": "Universidade Estadual de Campinas (UNICAMP)",
    "units": "px",
    "full_width_px": 292.0,
    "year_geometry": {
        2022: {"x_start": 1278, "full_width": 292.0},
        2023: {"x_start": 1579, "full_width": 291.0},
        2024: {"x_start": 1879, "full_width": 291.0},
    },
}

COUNTRY_TRANSLATIONS = {
    "Argentinien": "Argentina",
    "Australien": "Australia",
    "Brasilien": "Brazil",
    "Chile": "Chile",
    "China": "China",
    "Costa Rica": "Costa Rica",
    "Ecuador": "Ecuador",
    "Egypt": "Egypt",
    "Ghana": "Ghana",
    "Indien": "India",
    "India": "India",
    "Indonesien": "Indonesia",
    "Indonesia": "Indonesia",
    "Israel": "Israel",
    "Japan": "Japan",
    "Kanada": "Canada",
    "Canada": "Canada",
    "Kenya": "Kenya",
    "Kolumbien": "Colombia",
    "Colombia": "Colombia",
    "Korea": "Korea",
    "Kuba": "Cuba",
    "Malaysia": "Malaysia",
    "Marokko": "Morocco",
    "Morocco": "Morocco",
    "Mexiko": "Mexico",
    "Mexico": "Mexico",
    "Namibia": "Namibia",
    "Neuseeland": "New Zealand",
    "New Zealand": "New Zealand",
    "Panama": "Panama",
    "Peru": "Peru",
    "Philippinen": "Philippines",
    "Russland": "Russia",
    "Singapur": "Singapore",
    "Singapore": "Singapore",
    "Südafrika": "South Africa",
    "South Africa": "South Africa",
    "Taiwan": "Taiwan",
    "Thailand": "Thailand",
    "Uganda": "Uganda",
    "UK": "United Kingdom",
    "United Kingdom": "United Kingdom",
    "USA": "USA",
    "Vietnam": "Vietnam",
}

RASTER_ALIASES = {
    "instituto tecnoldgico de buenos aires (itba": "Instituto Tecnológico de Buenos Aires (ITBA)",
    "pontificia universidad catolica argentina, santa maria de los buenos aires": "Pontificia Universidad Católica Argentina, Santa María de los Buenos Aires",
    "universidad de buenos aires (uba": "Universidad de Buenos Aires (UBA)",
    "universidad nacional del sur": "Universidad Nacional del Sur",
    "australian national universi": "Australian National University",
    "macquarie universi": "Macquarie University",
    "queensland university of technolog": "Queensland University of Technology",
    "rmit universi": "RMIT University",
    "the university of queensland (uq": "The University of Queensland (UQ)",
    "university of adelaide": "University of Adelaide",
    "university of melbourne": "University of Melbourne",
    "university of new south wales": "University of New South Wales",
    "university of sydne": "University of Sydney",
    "university of technology sydne": "University of Technology Sydney",
    "university of wollongong": "University of Wollongong",
    "universidade de sao paulo (usp": "Universidade de São Paulo (USP)",
    "universidade estadual de campinas (unicamp": "Universidade Estadual de Campinas (UNICAMP)",
    "universidade estadual de campinas (unicamp.": "Universidade Estadual de Campinas (UNICAMP)",
    "universidade estadual paulista 'julio de mesquita filho' (unesp": "Universidade Estadual Paulista Júlio de Mesquita Filho (UNESP)",
    "universidade federal de santa catarina (ufsc": "Universidade Federal de Santa Catarina (UFSC)",
    "universidade federal do rio de janeiro (ufrj": "Universidade Federal do Rio de Janeiro (UFRJ)",
    "concordia universi": "Concordia University",
    "ecole de technologie supérieure (ets": "École de Technologie Supérieure (ETS)",
    "ecole polytechnique de montréal": "École Polytechnique de Montréal",
    "mcgill universi": "McGill University",
    "université de montréal": "Université de Montréal",
    "université du québec a trois-riviéres": "Université du Québec à Trois-Rivières",
    "université laval": "Université Laval",
    "university of alberta": "University of Alberta",
    "university of british columbia": "University of British Columbia",
    "university of manitoba": "University of Manitoba",
    "university of toronto": "University of Toronto",
    "university of waterloo": "University of Waterloo",
    "pontificia universidad catolica de chile": "Pontificia Universidad Católica de Chile",
    "pontificia universidad catolica de valparaiso": "Pontificia Universidad Católica de Valparaíso",
    "universidad de chile": "Universidad de Chile",
    "universidad de concepcidn": "Universidad de Concepción",
    "universidad técnica federico santa maria": "Universidad Técnica Federico Santa María",
    "city university of hong kong": "City University of Hong Kong",
    "huazhong university of science and technolog)": "Huazhong University of Science and Technology",
    "peking universi": "Peking University",
    "shanghai jiao tong universi": "Shanghai Jiao Tong University",
    "the chinese university of hong kong - shenzhen": "The Chinese University of Hong Kong, Shenzhen (CUHK-Shenzhen)",
    "the hong kong polytechnic universi": "The Hong Kong Polytechnic University",
    "the hong kong university of science and technology (school of science": "The Hong Kong University of Science and Technology (School of Science)",
    "the hong kong university of science and technology (school of engineering": "The Hong Kong University of Science and Technology (School of Engineering)",
    "the university of hong kong": "The University of Hong Kong",
    "tongji universi": "Tongji University",
    "tsinghua shenzhen international graduate school (tsinghua sigs": "Tsinghua Shenzhen International Graduate School (Tsinghua SIGS)",
    "tsinghua universi": "Tsinghua University",
    "zhejiang universi": "Zhejiang University",
    "universidad de antioquia": "Universidad de Antioquia",
    "universidad de los andes": "Universidad de Los Andes",
    "universidad nacional de colombia": "Universidad Nacional de Colombia",
    "instituto tecnologico de costa rica": "Instituto Tecnológico de Costa Rica",
    "american university in cairo": "The American University in Cairo",
    "kwame nkrumah university of science and technology (knust": "Kwame Nkrumah University of Science and Technology (KNUST)",
    "indian institute of science (iisc": "Indian Institute of Science (IISc)",
    "indian institute of technology bombay (iit bomba": "Indian Institute of Technology Bombay (IIT Bombay)",
    "indian institute of technology delhi (iit delhi": "Indian Institute of Technology Delhi (IIT Delhi)",
    "indian institute of technology guwahati (iit guwahati": "Indian Institute of Technology Guwahati (IIT Guwahati)",
    "indian institute of technology kanpur (iit kanpur": "Indian Institute of Technology Kanpur (IIT Kanpur)",
    "indian institute of technology kharagpur (iit kharagpur": "Indian Institute of Technology Kharagpur (IIT Kharagpur)",
    "indian institute of technology madras (iit madras": "Indian Institute of Technology Madras (IIT Madras)",
    "indian institute of technology mandi (iit mandi": "Indian Institute of Technology Mandi (IIT Mandi)",
    "indian institute of technology roorkee (iit roorkee": "Indian Institute of Technology Roorkee (IIT Roorkee)",
    "institut teknologi bandung (itb": "Institut Teknologi Bandung (ITB)",
    "universitas indonesia": "Universitas Indonesia",
    "hebrew university of jerusalem": "The Hebrew University of Jerusalem",
    "israel institute of technology (technion": "Israel Institute of Technology (Technion)",
    "tel aviv universit": "Tel Aviv University",
    "hokkaido universit": "Hokkaido University",
    "keio universit": "Keio University",
    "kobe universit": "Kobe University",
    "kyoto universi": "Kyoto University",
    "kyushu universit": "Kyushu University",
    "nagoya universit": "Nagoya University",
    "osaka universit": "The University of Osaka",
    "the university of tokyo": "The University of Tokyo",
    "tohoku universit": "Tohoku University",
    "tokyo institute of technolog)": "Tokyo Institute of Technology",
    "tokyo university of agriculture and technolog)": "Tokyo University of Agriculture and Technology",
    "waseda universit": "Waseda University",
    "jomo kenyatta university of agriculture and technology (jkua": "Jomo Kenyatta University of Agriculture and Technology (JKUAT)",
    "korea advanced institute of science and technolog)": "Korea Advanced Institute of Science and Technology (KAIST)",
    "korea universit": "Korea University",
    "pohang university of science and technolog)": "Pohang University of Science and Technology (POSTECH)",
    "pusan national universit": "Pusan National University",
    "seoul national universit": "Seoul National University",
    "yonsei universit": "Yonsei University",
    "universiti malaya (um": "Universiti Malaya (UM)",
    "instituto tecnoldgico y de estudios superiores de monterrey (itesm": "Instituto Tecnológico y de Estudios Superiores de Monterrey (ITESM)",
    "universidad de las américas puebla (udlap’": "Universidad de las Américas Puebla (UDLAP)",
    "universidad nacional aut6noma de méxico": "Universidad Nacional Autónoma de México (UNAM)",
    "al akhawayn universi": "Al Akhawayn University",
    "namibia university of science and technolog)": "Namibia University of Science and Technology",
    "the university of waikato": "The University of Waikato",
    "pontificia universidad catolica del pert": "Pontificia Universidad Católica del Perú",
    "nanyang technological universi": "Nanyang Technological University",
    "national university of singapore": "National University of Singapore",
    "stellenbosch universi": "Stellenbosch University",
    "national cheng kung universi": "National Cheng Kung University",
    "national taiwan universi": "National Taiwan University",
    "national taiwan university of science and technolog aiwan tech": "National Taiwan University of Science and Technology (Taiwan Tech)",
    "chulalongkorn universi": "Chulalongkorn University",
    "mahidol universi": "Mahidol University",
    "sirindhorn international institute of technolog)": "Sirindhorn International Institute of Technology (SIIT)",
    "thammasat university —“‘“‘“cs;s*™sc‘idzd": "Thammasat University",
    "makerere universi": "Makerere University",
    "aston universi": "Aston University",
    "cardiff universi": "Cardiff University",
    "queen's university belfast": "Queen's University Belfast",
    "the university of edinburgh": "The University of Edinburgh",
    "university college london": "University College London",
    "university of bath": "University of Bath",
    "university of bristol": "University of Bristol",
    "university of exeter": "University of Exeter",
    "university of newcastle upon tyne": "Newcastle University",
    "university of strathclyde": "University of Strathclyde",
    "georgia institute of technology (georgia tech": "Georgia Institute of Technology (Georgia Tech)",
    "oklahoma state university - stillwater": "Oklahoma State University - Stillwater",
    "university of california, davis": "University of California, Davis",
    "university of california, irvine": "University of California, Irvine",
    "university of california, san diego": "University of California, San Diego",
    "university of minnesota, twin cities": "University of Minnesota, Twin Cities",
    "virginia polytechnic institute and state university (virginia tech": "Virginia Polytechnic Institute and State University (Virginia Tech)",
    "hanoi university of science and technolog)": "Hanoi University of Science and Technology",
    "rmit international university vietnam": "RMIT International University Vietnam",
    "vietnam national university of hanoi - university of science": "Vietnam National University of Hanoi - University of Science",
}


def normalize_text(text: str) -> str:
    """Normalize text for consistent comparison."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def load_canonical_universities(db_path: Path) -> dict[str, tuple[str, str]]:
    """Load canonical university names and countries keyed by normalized name."""
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT university, country FROM observations"
        ).fetchall()
        canon_map = {}
        for uni, country in rows:
            canon_map[normalize_text(uni)] = (uni, country)
            canon_map[_university_key(uni)] = (uni, country)
        return canon_map
    finally:
        conn.close()


def match_university(
    raw_uni: str,
    raw_country: str,
    canon_map: Mapping[str, tuple[str, str]],
) -> tuple[str, str]:
    """Match OCR-extracted university text to canonical name and country."""
    norm = normalize_text(raw_uni)
    clean = raw_uni.strip()

    # 1. Direct explicit raster alias (check longer keys first)
    for alias_key in sorted(RASTER_ALIASES.keys(), key=len, reverse=True):
        target_uni = RASTER_ALIASES[alias_key]
        if alias_key in norm:
            country = COUNTRY_TRANSLATIONS.get(raw_country, raw_country)
            t_norm = normalize_text(target_uni)
            if t_norm in canon_map:
                return canon_map[t_norm]
            return (target_uni, country)

    # 2. Keyed match in canon_map
    k = _university_key(clean)
    if k in canon_map:
        return canon_map[k]

    if norm in canon_map:
        return canon_map[norm]

    # 3. Substring match
    for c_norm, (c_uni, c_country) in canon_map.items():
        if len(norm) > 10 and (norm in c_norm or c_norm in norm):
            return (c_uni, c_country)

    country = COUNTRY_TRANSLATIONS.get(raw_country, raw_country)
    return (clean, country)


def measure_cell_bar(
    crop: Image.Image,
    full_width: float,
) -> tuple[str, float | None, str]:
    """Measure blue vector bar inside a cell crop or detect badge status."""
    fw_int = int(full_width)
    blue_cols = []
    for x in range(0, min(crop.width, fw_int)):
        cnt = sum(
            1
            for y in range(crop.height)
            if abs(crop.getpixel((x, y))[0] - 0) < 25
            and abs(crop.getpixel((x, y))[1] - 113) < 25
            and abs(crop.getpixel((x, y))[2] - 193) < 25
        )
        if cnt >= 6:
            blue_cols.append(x)

    # Bar must start near left edge (<= 10)
    if blue_cols and min(blue_cols) <= 10:
        end = blue_cols[0]
        for c in blue_cols[1:]:
            if c - end <= 25:
                end = c
            else:
                break
        run_len = end + 1
        if run_len >= 3:
            pct = min(100.0, round((run_len / full_width) * 100.0, 2))
            return "measured", pct, "visual_bar"

    # If no valid blue bar starting on the left, perform OCR on cell
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        crop.save(f.name)
        tmp = f.name
    try:
        cmd = ["/opt/homebrew/bin/tesseract", tmp, "stdout", "--psm", "6"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = res.stdout.decode("utf-8", errors="replace").lower().strip()
    finally:
        Path(tmp).unlink(missing_ok=True)

    if "application" in text or "appication" in text or "no app" in text:
        return "no_applications", 100.0, "no_applications"
    if "place" in text or "platze" in text:
        return "no_places", None, "none"
    if "cooperation" in text or "koop" in text or "coop" in text:
        return "no_cooperation", None, "none"

    return "no_places", None, "none"


def extract_2025_records(
    pdf_path: Path = DEFAULT_SOURCE_PDF,
    images_dir: Path = DEFAULT_IMAGES_DIR,
    canonical_db: Path = DEFAULT_CANONICAL_DB,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract all records from the 2025 document across all observation years."""
    canon_map = load_canonical_universities(canonical_db)
    source_bytes = pdf_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    doc_id = f"sha256:{source_sha256[:16]}"

    # Page image definitions
    page_specs = [
        (1, ["page_1_2_Im3.jpg"]),
        (2, ["page_2_0_Im4.jpg", "page_2_1_Im5.jpg"]),
        (3, ["page_3_0_Im6.jpg"]),
        (4, ["page_4_0_Im7.jpg", "page_4_1_Im8.jpg"]),
    ]

    records: list[dict[str, Any]] = []
    current_country = "Argentina"
    global_row_index = 0

    for p_num, img_files in page_specs:
        if len(img_files) == 1:
            im = Image.open(images_dir / img_files[0])
        else:
            im_a = Image.open(images_dir / img_files[0])
            im_b = Image.open(images_dir / img_files[1])
            im = Image.new("RGB", (im_a.width, im_a.height + im_b.height))
            im.paste(im_a, (0, 0))
            im.paste(im_b, (0, im_a.height))

        # OCR text layout on left side
        crop = im.crop((0, 0, 1270, im.height))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            crop.save(f.name)
            tmp = f.name
        try:
            cmd = ["/opt/homebrew/bin/tesseract", tmp, "stdout", "--psm", "6", "tsv"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            Path(tmp).unlink(missing_ok=True)

        by_line: dict[tuple[int, int, int], list[tuple[int, int, int, int, str]]] = {}
        for row in res.stdout.decode("utf-8", errors="replace").split("\n"):
            parts = row.split("\t")
            if len(parts) >= 12 and parts[11].strip():
                try:
                    key = (int(parts[2]), int(parts[3]), int(parts[4]))
                    by_line.setdefault(key, []).append(
                        (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]), parts[11])
                    )
                except ValueError:
                    continue

        lines = []
        for k, words in by_line.items():
            text = " ".join(w[4] for w in words).strip()
            top = min(w[1] for w in words)
            lines.append((top, text))

        # Insert Makerere & Aston on Page 4 if missed by coarse TSV
        if p_num == 4:
            has_makerere = any("makerere" in t.lower() for _, t in lines)
            if not has_makerere:
                lines.append((438, "Makerere University"))
            has_aston = any("aston" in t.lower() for _, t in lines)
            if not has_aston:
                lines.append((585, "Aston University"))

        lines.sort(key=lambda item: item[0])

        for top_y, text in lines:
            if "partner university" in text.lower() or "seat changes" in text.lower():
                continue

            # Check if this line is a Country header
            matched_country = None
            clean_text = text.replace("—", "").strip()
            for c_de, c_en in COUNTRY_TRANSLATIONS.items():
                if clean_text.lower() == c_de.lower() or clean_text.lower() == c_en.lower():
                    matched_country = c_en
                    break

            if matched_country:
                current_country = matched_country
                continue

            # University line
            global_row_index += 1
            canonical_uni, country = match_university(text, current_country, canon_map)

            # Process observation years: 2022, 2023, 2024
            for year in [2022, 2023, 2024]:
                geom = CALIBRATION_SPEC["year_geometry"][year]
                cx0 = geom["x_start"]
                fw = geom["full_width"]

                # Extract cell crop around row top_y
                cell_crop = im.crop((cx0, top_y - 8, cx0 + int(fw) + 12, top_y + 40))
                status, score_pct, score_basis = measure_cell_bar(cell_crop, fw)

                bar_frac = (score_pct / 100.0) if score_pct is not None else None
                row_id = f"{doc_id}:{global_row_index}:{year}"

                rec = {
                    "row_id": row_id,
                    "source_document_id": doc_id,
                    "source_sha256": source_sha256,
                    "source_filename": pdf_path.name,
                    "source_release_year": RELEASE_YEAR,
                    "country": country,
                    "university": canonical_uni,
                    "year": year,
                    "page": p_num,
                    "row_index": global_row_index,
                    "status": status,
                    "score_percent": score_pct,
                    "score_basis": score_basis,
                    "bar_fraction": bar_frac,
                    "raw_width_pt": round(bar_frac * fw, 2) if bar_frac is not None else None,
                    "full_width_pt": fw,
                    "calibration_id": f"raster_brazil_{RELEASE_YEAR}",
                    "bar_bbox": [cx0, top_y, cx0 + int(fw), top_y + 38],
                    "status_box_bbox": None,
                    "status_box_width_pt": None,
                    "notes": (
                        "Extracted via pixel bar measurement calibrated on Brazil 2022 full bars"
                        if status == "measured"
                        else f"Cell status: {status}"
                    ),
                    "raw_label": text,
                    "raw_university_text": text,
                    "column_label": str(year),
                    "units": "px",
                    "geometry": {
                        "x_start": cx0,
                        "y_top": top_y,
                        "full_width_px": fw,
                    },
                    "extraction_version": EXTRACTION_VERSION,
                }
                records.append(rec)

    calibration = {
        "calibration_id": f"raster_brazil_{RELEASE_YEAR}",
        "reference_university": CALIBRATION_SPEC["reference_university"],
        "source_release_year": RELEASE_YEAR,
        "full_width_pt": CALIBRATION_SPEC["full_width_px"],
        "units": "px",
        "method": CALIBRATION_SPEC["method"],
    }
    return records, calibration


def cross_validate_against_2026(
    records_2025: Sequence[Mapping[str, Any]],
    records_2026_path: Path = DEFAULT_2026_RECORDS,
) -> dict[str, Any]:
    """Cross-validate overlapping 2023 and 2024 admission rates against Release 2026."""
    if not records_2026_path.is_file():
        return {"error": f"2026 records not found at {records_2026_path}"}

    with open(records_2026_path, encoding="utf-8") as f:
        data_2026 = json.load(f)

    gt_map = {
        (r["university"], r["year"]): r
        for r in data_2026.get("records", [])
    }

    compared_count = 0
    close_matches = 0
    exact_matches = 0
    status_matches = 0
    diffs = []

    for r25 in records_2025:
        year = r25["year"]
        if year not in (2023, 2024):
            continue

        gt = gt_map.get((r25["university"], year))
        if not gt:
            continue

        compared_count += 1
        if r25["status"] == gt["status"]:
            status_matches += 1

        if r25["status"] == "measured" and gt["status"] == "measured":
            m25 = r25["score_percent"]
            m26 = gt["score_percent"]
            diff = abs(m25 - m26)
            diffs.append(diff)
            if diff < 0.05:
                exact_matches += 1
            if diff <= 2.5:
                close_matches += 1

    mean_diff = (sum(diffs) / len(diffs)) if diffs else 0.0
    max_diff = max(diffs) if diffs else 0.0

    return {
        "compared_records": compared_count,
        "measured_comparisons": len(diffs),
        "status_matches": status_matches,
        "close_matches_within_2_5pct": close_matches,
        "mean_diff_pct": round(mean_diff, 2),
        "max_diff_pct": round(max_diff, 2),
    }


def write_2025_outputs(
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Path]:
    """Write records.json, rows.csv, and bars.sqlite for Release 2025."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "records.json"
    csv_path = output_dir / "rows.csv"
    sqlite_path = output_dir / "bars.sqlite"

    if sqlite_path.is_file():
        sqlite_path.unlink()

    write_records_json(records, calibration, json_path)
    write_rows_csv(records, csv_path)
    write_sqlite(records, calibration, sqlite_path)

    return {"json": json_path, "csv": csv_path, "sqlite": sqlite_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract TUMexchange 2025 raster bars")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_SOURCE_PDF, help="source PDF path")
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES_DIR, help="images directory")
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    parser.add_argument("--database", "-d", type=Path, default=DEFAULT_CANONICAL_DB, help="canonical SQLite DB")
    parser.add_argument("--cross-validate", action="store_true", default=True, help="cross-validate against 2026")
    args = parser.parse_args()

    records, calibration = extract_2025_records(
        pdf_path=args.pdf,
        images_dir=args.images,
        canonical_db=args.database,
    )
    outputs = write_2025_outputs(records, calibration, args.output)

    unis = sorted(set(r["university"] for r in records))
    years = sorted(set(r["year"] for r in records))
    measured = sum(1 for r in records if r["status"] == "measured")
    no_apps = sum(1 for r in records if r["status"] == "no_applications")

    summary = {
        "output_dir": str(args.output),
        "files": {k: str(v) for k, v in outputs.items()},
        "total_records": len(records),
        "distinct_universities": len(unis),
        "years": years,
        "measured_count": measured,
        "no_applications_count": no_apps,
    }

    if args.cross_validate:
        cv = cross_validate_against_2026(records)
        summary["cross_validation_2026"] = cv

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main()
