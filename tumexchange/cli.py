"""Command-line interface for extraction, import, and read-only queries."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import EXTRACTION_VERSION
from .extract import ExtractionError, extract_pdf
from .report import write_report
from .storage import (
    CSV_FIELDS,
    StorageError,
    import_records_json,
    query_sqlite,
    read_calibration_manifest,
    read_current_records,
    write_calibration_json,
    write_outputs,
    write_rows_csv,
)


def _write_report(records: list[dict[str, Any]], calibration: Mapping[str, Any], path: Path) -> None:
    write_report(records, calibration, path)


def _cmd_extract(args: argparse.Namespace) -> int:
    records, calibration = extract_pdf(
        args.pdf,
        calibration_override=args.calibration,
        reference_country=args.reference_country,
        reference_year=args.reference_year,
        reference_university=args.reference_university,
        tolerance_pt=args.tolerance_pt,
        allow_incomplete=args.allow_incomplete,
        allow_cross_source_calibration=args.allow_cross_source_calibration,
        source_release_year=args.source_release_year,
    )
    output_dir = Path(args.output).expanduser()
    paths = write_outputs(records, calibration, output_dir, allow_overwrite=args.force)
    if not args.no_report:
        paths["report"] = output_dir / "report.html"
        _write_report(records, calibration, paths["report"])
    summary = {
        "source": calibration.get("source_filename"),
        "source_sha256": calibration.get("source_sha256"),
        "source_release_year": calibration.get("source_release_year"),
        "output": str(output_dir.resolve()),
        "records": len(records),
        "universities": calibration.get("university_count"),
        "measured": calibration.get("measured_count"),
        "no_applications": calibration.get("no_applications_count"),
        "unavailable": calibration.get("unavailable_count"),
        "calibration_id": calibration.get("calibration_id"),
        "full_width_pt_by_year": calibration.get("full_width_pt_by_year"),
        "files": {key: str(value) for key, value in paths.items()},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _filter_query(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if args.country:
        clauses.append("country = ?")
        parameters.append(args.country)
    if args.university:
        clauses.append("university LIKE ?")
        parameters.append(f"%{args.university}%")
    if args.year is not None:
        clauses.append("year = ?")
        parameters.append(args.year)
    if args.status:
        clauses.append("status = ?")
        parameters.append(args.status)
    if args.min_score is not None:
        clauses.append("score_percent >= ?")
        parameters.append(args.min_score)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT country, university, year, status, score_percent, score_basis, "
        "raw_width_pt, full_width_pt, source_release_year, source_document_id, row_id "
        "FROM observations"
        + where
        + " ORDER BY country, university, year"
    )
    if args.limit is not None:
        sql += " LIMIT ?"
        parameters.append(args.limit)
    return sql, parameters


def _print_query(rows: list[dict[str, Any]], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if output_format == "csv":
        if not rows:
            return
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    if not rows:
        print("No rows")
        return
    fields = list(rows[0])
    widths = {field: max(len(field), *(len(str(row.get(field, ""))) for row in rows)) for field in fields}
    print("  ".join(field.ljust(widths[field]) for field in fields))
    print("  ".join("-" * widths[field] for field in fields))
    for row in rows:
        print("  ".join(str(row.get(field, "")).ljust(widths[field]) for field in fields))


def _cmd_query(args: argparse.Namespace) -> int:
    if args.sql and any(
        value is not None
        for value in (args.country, args.university, args.year, args.status, args.min_score, args.limit)
    ):
        raise StorageError("use either --sql or filter options, not both")
    if args.sql:
        rows = query_sqlite(args.database, args.sql)
    else:
        sql, parameters = _filter_query(args)
        rows = query_sqlite(args.database, sql, parameters)
    _print_query(rows, args.format)
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    database = args.database or str(Path(args.records).with_name("bars.sqlite"))
    path = import_records_json(args.records, database)
    print(json.dumps({"database": str(path.resolve()), "records_json": str(Path(args.records).resolve())}, indent=2))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    profiles = None
    if args.profiles:
        from .profiles import load_profiles

        profiles = load_profiles(Path(args.profiles))
        if args.profile_matches:
            with Path(args.profile_matches).open(encoding="utf-8") as handle:
                profiles["matches"] = json.load(handle)
    elif args.profile_matches:
        raise ValueError("--profile-matches requires --profiles")
    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    records = read_current_records(args.database)
    calibration = read_calibration_manifest(args.database)
    write_rows_csv(records, output / "rows.csv")
    write_calibration_json(calibration, output / "calibration.json")
    report_path = output / "combined-report.html"
    if profiles is None:
        _write_report(records, calibration, report_path)
    else:
        write_report(records, calibration, report_path, profiles=profiles)
    print(
        json.dumps(
            {
                "database": str(Path(args.database).resolve()),
                "output": str(output.resolve()),
                "current_observations": len(records),
                "source_count": calibration["source_count"],
                "report": str(report_path.resolve()),
            },
            indent=2,
        )
    )
    return 0


def _cmd_fetch_profiles(args: argparse.Namespace) -> int:
    from .moveon import fetch_catalog, write_catalog

    catalog = fetch_catalog()
    output = Path(args.output)
    previous = None
    if output.exists():
        with output.open(encoding="utf-8") as handle:
            previous = json.load(handle)
    changes = _profile_changes(previous, catalog)
    if not args.check:
        write_catalog(catalog, output)
    print(json.dumps({
        "output": str(output.resolve()),
        "written": not args.check,
        "profiles": len(catalog["profiles"]),
        "retrieved_at": catalog["retrieved_at"],
        "source_url": catalog["source_url"],
        "changes": changes,
    }, indent=2))
    return 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    from .consolidate import build_combined_database

    summary = build_combined_database(target_db_path=args.database)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _profile_changes(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    """Compare source content by stable relation ID, ignoring retrieval timestamps."""
    def index(catalog: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        if catalog is None:
            return {}
        return {
            str(profile["relation_id"]): {
                key: value for key, value in profile.items() if key != "retrieved_at"
            }
            for profile in catalog["profiles"]
        }

    old, new = index(previous), index(current)
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(key for key in old.keys() & new.keys() if old[key] != new[key]),
        "unchanged": sum(old[key] == new[key] for key in old.keys() & new.keys()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tumexchange",
        description="Extract calibrated vector chance bars from TUMexchange PDFs.",
    )
    parser.add_argument("--version", action="version", version=EXTRACTION_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="extract one PDF and write CSV, JSON, SQLite, and HTML")
    extract.add_argument("pdf", type=Path, help="source vector PDF")
    extract.add_argument("--output", "-o", required=True, help="output directory")
    extract.add_argument("--calibration", type=Path, help="calibration JSON override for another same-layout PDF")
    extract.add_argument("--reference-country", default="Brazil", help="country used for explicit full-bar references (default: Brazil)")
    extract.add_argument("--reference-year", type=int, default=2025, help="year used for explicit full-bar references (default: 2025)")
    extract.add_argument(
        "--reference-university",
        default="Universidade Federal do Rio de Janeiro (UFRJ)",
        help="known full-bar university (default: UFRJ)",
    )
    extract.add_argument(
        "--source-release-year",
        type=int,
        help="document release year used for overlap precedence (default: infer from filename)",
    )
    extract.add_argument("--tolerance-pt", type=float, default=0.001, help="full-reference equality tolerance in PDF points (default: 0.001)")
    extract.add_argument("--allow-incomplete", action="store_true", help="write data despite ambiguous or missing bar cells after reviewing the layout")
    extract.add_argument("--allow-cross-source-calibration", action="store_true", help="allow a calibration JSON carrying another PDF hash after independently checking identical geometry")
    extract.add_argument("--force", action="store_true", help="allow replacing a different document's human-readable exports")
    extract.add_argument("--no-report", action="store_true", help="skip report.html")
    extract.set_defaults(handler=_cmd_extract)

    query = commands.add_parser("query", help="read-only SQL or filter query against bars.sqlite")
    query.add_argument("database", type=Path, help="SQLite database")
    query.add_argument("--sql", help="read-only SELECT/WITH/PRAGMA statement")
    query.add_argument("--country")
    query.add_argument("--university")
    query.add_argument("--year", type=int)
    query.add_argument("--status", choices=["measured", "no_cooperation", "no_places", "no_applications", "bar_absent", "ambiguous"])
    query.add_argument("--min-score", type=float)
    query.add_argument("--limit", type=int)
    query.add_argument("--format", choices=["table", "json", "csv"], default="table")
    query.set_defaults(handler=_cmd_query)

    import_parser = commands.add_parser("import", help="append an exported records.json to a SQLite database")
    import_parser.add_argument("records", type=Path, help="records.json created by extract")
    import_parser.add_argument("--database", type=Path, help="target database (default: sibling bars.sqlite)")
    import_parser.set_defaults(handler=_cmd_import)

    report_parser = commands.add_parser(
        "report",
        help="build CSV and HTML from a combined database's current observations",
    )
    report_parser.add_argument("database", type=Path)
    report_parser.add_argument("--output", "-o", required=True, help="output directory")
    report_parser.add_argument("--profiles", type=Path, help="saved MoveON university profile catalog")
    report_parser.add_argument("--profile-matches", type=Path, help="reviewed university-to-relation matching overrides")
    report_parser.set_defaults(handler=_cmd_report)

    profiles_parser = commands.add_parser(
        "fetch-profiles", help="save all public MoveON exchange profiles with a coverage check",
    )
    profiles_parser.add_argument("--output", "-o", type=Path, required=True, help="destination profile catalog JSON")
    profiles_parser.add_argument("--check", action="store_true", help="show source changes without replacing the saved catalog")
    profiles_parser.set_defaults(handler=_cmd_fetch_profiles)

    consolidate_parser = commands.add_parser(
        "consolidate",
        help="rebuild combined bars.sqlite and exports from all historical sources (2016-2026)",
    )
    consolidate_parser.add_argument(
        "--database",
        "-d",
        type=Path,
        default=Path("data/output/combined/bars.sqlite"),
        help="target combined database (default: data/output/combined/bars.sqlite)",
    )
    consolidate_parser.set_defaults(handler=_cmd_consolidate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ExtractionError, StorageError, OSError, ValueError) as exc:
        print(f"tumexchange: error: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
