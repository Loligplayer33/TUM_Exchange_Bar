"""Consolidate and combine all historical TUMexchange admission data (2016–2026).

Combines 6 primary source releases into a unified `bars.sqlite` database:
1. Release 2027 (vector bar charts, observation years 2024–2026)
2. Release 2026 v02 (vector bar charts, observation years 2023–2025)
3. Release 2025 (raster bar charts, observation years 2022–2024, Brazil calibrated)
4. Release 2022 (tabular traffic-light quartiles, observation years 2019–2022)
5. Release 2018 (tabular traffic-light quartiles, observation years 2017–2018)
6. Release 2017 (exact decimal percentages, observation year 2017)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from tumexchange.storage import (
    read_calibration_manifest,
    read_current_records,
    write_calibration_json,
    write_records_json,
    write_rows_csv,
    write_sqlite,
)

LOGGER = logging.getLogger(__name__)


def build_combined_database(
    target_db_path: Path = Path("data/output/combined/bars.sqlite"),
    backup_existing: bool = True,
) -> dict[str, Any]:
    """Rebuild the combined database from all historical sources."""
    target_db_path = Path(target_db_path)
    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    if backup_existing and target_db_path.is_file():
        backup_path = target_db_path.with_name("bars_backup.sqlite")
        with sqlite3.connect(target_db_path) as src, sqlite3.connect(backup_path) as dst:
            src.backup(dst)

    # Clean target DB before fresh consolidation
    if target_db_path.is_file():
        target_db_path.unlink()

    # Define the 6 source layers in chronological order
    # Precedence in _refresh_current_observations handles continuous vs categorical and release year
    source_layers = [
        # 1. Exact 2016/17 (Release 2017)
        {
            "name": "exact_2016",
            "records_path": Path("data/output/exact_2016/records.json"),
            "calibration": {
                "calibration_id": "exact_percentage_2017",
                "method": "exact_published_percentage",
                "source_release_year": 2017,
                "units": "percent",
                "full_width_pt": 100.0,
            },
        },
        # 2. English Tabular 2016-2018 (Release 2018)
        {
            "name": "tabular_2018_en",
            "records_path": Path("data/output/tabular/records.json"),
            "doc_filename": "Auswahlquote_TUMexchange_nach_Unis_eng.pdf",
            "calibration": {
                "calibration_id": "tabular_traffic_light_2018",
                "method": "tabular_traffic_light_4tier",
                "source_release_year": 2018,
                "units": "quartile",
                "full_width_pt": 100.0,
            },
        },
        # 3. German Tabular 2018-2022 (Release 2022)
        {
            "name": "tabular_2022_de",
            "records_path": Path("data/output/tabular/records.json"),
            "doc_filename": "Auswahlquote_TUMexchange_2021_22_GESAMT_deut.pdf",
            "calibration": {
                "calibration_id": "tabular_traffic_light_2022",
                "method": "tabular_traffic_light_4tier",
                "source_release_year": 2022,
                "units": "quartile",
                "full_width_pt": 100.0,
            },
        },
        # 4. Raster 2025 (Release 2025, Brazil calibrated)
        {
            "name": "raster_2025",
            "records_path": Path("data/output/2025/records.json"),
            "calibration": {
                "calibration_id": "raster_brazil_2025",
                "reference_university": "Universidade Estadual de Campinas (UNICAMP)",
                "source_release_year": 2025,
                "full_width_pt": 292.0,
                "units": "px",
                "method": "raster_brazil_anchor",
            },
        },
        # 5. Vector 2026 v02 (Release 2026)
        {
            "name": "vector_2026",
            "records_path": Path("data/output/2026_v02/records.json"),
            "calibration_path": Path("data/output/2026_v02/calibration.json"),
        },
        # 6. Vector 2027 (Release 2027)
        {
            "name": "vector_2027",
            "records_path": Path("data/output/TUMexchange_Chances_2027/records.json"),
            "calibration_path": Path("data/output/TUMexchange_Chances_2027/calibration.json"),
        },
    ]

    imported_docs = []
    for layer in source_layers:
        rec_path = layer["records_path"]
        if not rec_path.is_file():
            LOGGER.warning("Layer %s records not found at %s, skipping", layer["name"], rec_path)
            continue

        with open(rec_path, encoding="utf-8") as f:
            data = json.load(f)

        records = data.get("records", [])
        if "doc_filename" in layer:
            # Filter records for the specific document in tabular
            records = [r for r in records if r.get("source_filename") == layer["doc_filename"]]

        if not records:
            continue

        if "calibration" in layer:
            cal = layer["calibration"]
        else:
            with open(layer["calibration_path"], encoding="utf-8") as f:
                cal = json.load(f)

        write_sqlite(records, cal, target_db_path)
        imported_docs.append({
            "name": layer["name"],
            "records_count": len(records),
            "release_year": cal.get("source_release_year"),
        })

    # Read consolidated snapshot and write exports
    current_records = read_current_records(target_db_path)
    cal_manifest = read_calibration_manifest(target_db_path)

    out_dir = target_db_path.parent
    write_rows_csv(current_records, out_dir / "rows.csv")
    out_dir.joinpath("records.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "calibration": cal_manifest,
                "record_count": len(current_records),
                "records": current_records,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    write_calibration_json(cal_manifest, out_dir / "calibration.json")

    # Group counts
    years_count = {}
    basis_count = {}
    for r in current_records:
        y = r["year"]
        years_count[y] = years_count.get(y, 0) + 1
        b = r.get("score_basis") or "none"
        basis_count[b] = basis_count.get(b, 0) + 1

    summary = {
        "database": str(target_db_path.resolve()),
        "imported_documents": len(imported_docs),
        "total_active_observations": len(current_records),
        "distinct_universities": len(set(r["university"] for r in current_records)),
        "observation_years": sorted(years_count.keys()),
        "observations_per_year": years_count,
        "score_basis_counts": basis_count,
    }
    return summary


def main() -> int:
    summary = build_combined_database()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main()
