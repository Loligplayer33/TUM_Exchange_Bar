"""CSV, JSON, and SQLite persistence for extracted TUMexchange records."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "0.2"

CSV_FIELDS = [
    # Stable row API, in the order used by the project brief.
    "country",
    "university",
    "year",
    "page",
    "row_index",
    "status",
    "score_percent",
    "bar_fraction",
    "raw_width_pt",
    "full_width_pt",
    "calibration_id",
    "bar_bbox",
    "notes",
    # Provenance and additional queryable fields.
    "row_id",
    "source_document_id",
    "source_sha256",
    "source_filename",
    "source_release_year",
    "is_latest_roster",
    "extraction_version",
    "score_basis",
    "status_box_bbox",
    "status_box_width_pt",
    "raw_label",
    "raw_university_text",
    "column_label",
    "units",
    "geometry",
]


class StorageError(RuntimeError):
    """Raised when an output would silently replace another document."""


_OBSERVATION_COLUMNS = (
    "row_id",
    "source_document_id",
    "calibration_id",
    "university_key",
    "country",
    "university",
    "year",
    "page",
    "row_index",
    "status",
    "score_percent",
    "score_basis",
    "bar_fraction",
    "raw_width_pt",
    "full_width_pt",
    "bar_bbox_json",
    "status_box_bbox_json",
    "status_box_width_pt",
    "notes",
    "raw_label",
    "raw_university_text",
    "column_label",
    "units",
    "geometry_json",
    "source_sha256",
    "source_filename",
    "source_release_year",
    "extraction_version",
)

_OBSERVATION_JSON_COLUMNS = {
    "bar_bbox_json",
    "status_box_bbox_json",
    "geometry_json",
}

_DOCUMENT_COLUMNS = (
    "source_document_id",
    "source_sha256",
    "source_filename",
    "source_release_year",
    "extraction_version",
    "record_count",
)


def _canonical_json(value: Any) -> str:
    """Serialize JSON values for semantic, order-independent comparison."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            # A malformed value must remain distinguishable from a valid JSON
            # value.  The caller will reject it when it compares a DB payload.
            return value
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _university_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[„“”\"\'‘’]", "", text)
    text = re.sub(r"\s*[—–].*$", "", text)
    text = re.sub(r"^(the|m\.v\.)\s+", "", text, flags=re.IGNORECASE)
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if match is None:
            break
        qualifier = match.group(1).strip()
        if qualifier.casefold().startswith("planned transfer"):
            text = text[: match.start()].rstrip()
            continue
        if any(
            qualifier.casefold().startswith(prefix)
            for prefix in [
                "school",
                "faculty",
                "graduate school",
                "department",
                "college",
                "campus",
            ]
        ):
            break
        if re.fullmatch(r"[A-Za-z0-9&.+ -]{2,24}", qualifier):
            text = text[: match.start()].rstrip()
        else:
            break
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[,/]", " - ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = text.casefold()
    return " ".join(text.split())


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return _json_value(value)
    return value


def _source_from_records(records: Sequence[Mapping[str, Any]], calibration: Mapping[str, Any]) -> dict[str, Any]:
    first = records[0] if records else {}
    source = {
        "source_document_id": first.get("source_document_id") or calibration.get("source_document_id"),
        "source_sha256": first.get("source_sha256") or calibration.get("source_sha256"),
        "source_filename": first.get("source_filename") or calibration.get("source_filename"),
        "source_release_year": first.get("source_release_year") or calibration.get("source_release_year"),
        "extraction_version": first.get("extraction_version") or calibration.get("extraction_version"),
    }
    for index, record in enumerate(records):
        for field in (
            "source_document_id",
            "source_sha256",
            "source_filename",
            "source_release_year",
            "extraction_version",
        ):
            value = record.get(field)
            if value is not None and source.get(field) is not None and value != source[field]:
                raise StorageError(
                    f"record {index} has {field}={value!r}, but the document source is "
                    f"{source[field]!r}"
                )
    return source


def _records_payload(
    records: Sequence[Mapping[str, Any]], calibration: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the exact payload written to ``records.json``."""

    source = _source_from_records(records, calibration)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "calibration_id": calibration.get("calibration_id"),
        "record_count": len(records),
        "records": list(records),
    }


def _validate_record_ids(records: Sequence[Mapping[str, Any]]) -> None:
    row_ids = [record.get("row_id") for record in records]
    if any(row_id is None or row_id == "" for row_id in row_ids):
        raise StorageError("every observation must have a non-empty row_id")
    if len(set(row_ids)) != len(row_ids):
        raise StorageError("records contain duplicate row_id values")


def write_rows_csv(records: Sequence[Mapping[str, Any]], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({field: _csv_value(record.get(field)) for field in CSV_FIELDS})
    return path


def write_records_json(
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _records_payload(records, calibration)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_calibration_json(calibration: Mapping[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(calibration), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS documents (
            source_document_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL UNIQUE,
            source_filename TEXT NOT NULL,
            source_release_year INTEGER NOT NULL,
            extraction_version TEXT,
            record_count INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calibrations (
            calibration_id TEXT PRIMARY KEY,
            source_document_id TEXT,
            source_sha256 TEXT,
            method TEXT,
            formula TEXT,
            units TEXT,
            full_width_pt REAL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(source_document_id) REFERENCES documents(source_document_id)
        );
        CREATE TABLE IF NOT EXISTS source_observations (
            row_id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL,
            calibration_id TEXT,
            university_key TEXT NOT NULL,
            country TEXT NOT NULL,
            university TEXT NOT NULL,
            year INTEGER NOT NULL,
            page INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            score_percent REAL,
            score_basis TEXT,
            bar_fraction REAL,
            raw_width_pt REAL,
            full_width_pt REAL,
            bar_bbox_json TEXT,
            status_box_bbox_json TEXT,
            status_box_width_pt REAL,
            notes TEXT,
            raw_label TEXT,
            raw_university_text TEXT,
            column_label TEXT,
            units TEXT,
            geometry_json TEXT,
            source_sha256 TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_release_year INTEGER NOT NULL,
            extraction_version TEXT,
            FOREIGN KEY(source_document_id) REFERENCES documents(source_document_id),
            FOREIGN KEY(calibration_id) REFERENCES calibrations(calibration_id)
        );
        CREATE TABLE IF NOT EXISTS observations (
            row_id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL,
            calibration_id TEXT,
            university_key TEXT NOT NULL,
            country TEXT NOT NULL,
            university TEXT NOT NULL,
            year INTEGER NOT NULL,
            page INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            score_percent REAL,
            score_basis TEXT,
            bar_fraction REAL,
            raw_width_pt REAL,
            full_width_pt REAL,
            bar_bbox_json TEXT,
            status_box_bbox_json TEXT,
            status_box_width_pt REAL,
            notes TEXT,
            raw_label TEXT,
            raw_university_text TEXT,
            column_label TEXT,
            units TEXT,
            geometry_json TEXT,
            source_sha256 TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_release_year INTEGER NOT NULL,
            extraction_version TEXT,
            UNIQUE(university_key, year),
            FOREIGN KEY(source_document_id) REFERENCES documents(source_document_id),
            FOREIGN KEY(calibration_id) REFERENCES calibrations(calibration_id)
        );
        CREATE INDEX IF NOT EXISTS idx_observations_university_year
            ON observations(university, year);
        CREATE INDEX IF NOT EXISTS idx_observations_country_year
            ON observations(country, year);
        CREATE INDEX IF NOT EXISTS idx_observations_status
            ON observations(status);
        CREATE INDEX IF NOT EXISTS idx_observations_source
            ON observations(source_document_id);
        CREATE INDEX IF NOT EXISTS idx_observations_score
            ON observations(score_percent);
        CREATE INDEX IF NOT EXISTS idx_source_observations_source
            ON source_observations(source_document_id);
        CREATE INDEX IF NOT EXISTS idx_source_observations_key_year
            ON source_observations(university_key, year);
        CREATE INDEX IF NOT EXISTS idx_calibrations_source
            ON calibrations(source_document_id);
        """
    )


def _require_schema(connection: sqlite3.Connection) -> None:
    """Fail closed for databases from an incompatible earlier schema."""

    required = {
        "documents": {
            "source_document_id",
            "source_sha256",
            "source_filename",
            "source_release_year",
            "extraction_version",
            "record_count",
            "imported_at",
        },
        "calibrations": {
            "calibration_id",
            "source_document_id",
            "source_sha256",
            "method",
            "formula",
            "units",
            "full_width_pt",
            "payload_json",
        },
        "source_observations": set(_OBSERVATION_COLUMNS),
        "observations": set(_OBSERVATION_COLUMNS),
    }
    for table, expected in required.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = {str(row[1]) for row in rows}
        missing = sorted(expected - actual)
        if missing:
            raise StorageError(
                f"SQLite table {table!r} is missing columns required for exact source checks: "
                + ", ".join(missing)
            )


def _record_observation_values(
    record: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the exact values persisted for one observation row."""

    source_document_id = record.get("source_document_id") or source.get("source_document_id")
    source_sha256 = record.get("source_sha256") or source.get("source_sha256")
    source_filename = record.get("source_filename") or source.get("source_filename") or "unknown.pdf"
    extraction_version = record.get("extraction_version") or source.get("extraction_version")
    return {
        "row_id": record.get("row_id"),
        "source_document_id": source_document_id,
        "calibration_id": record.get("calibration_id"),
        "university_key": _university_key(record.get("university")),
        "country": record.get("country", ""),
        "university": record.get("university", ""),
        "year": record.get("year"),
        "page": record.get("page"),
        "row_index": record.get("row_index"),
        "status": record.get("status"),
        "score_percent": record.get("score_percent"),
        "score_basis": record.get("score_basis"),
        "bar_fraction": record.get("bar_fraction"),
        "raw_width_pt": record.get("raw_width_pt"),
        "full_width_pt": record.get("full_width_pt"),
        "bar_bbox_json": (
            _json_value(record.get("bar_bbox")) if record.get("bar_bbox") is not None else None
        ),
        "status_box_bbox_json": (
            _json_value(record.get("status_box_bbox"))
            if record.get("status_box_bbox") is not None
            else None
        ),
        "status_box_width_pt": record.get("status_box_width_pt"),
        "notes": record.get("notes"),
        "raw_label": record.get("raw_label"),
        "raw_university_text": record.get("raw_university_text"),
        "column_label": record.get("column_label"),
        "units": record.get("units"),
        "geometry_json": (
            _json_value(record.get("geometry")) if record.get("geometry") is not None else None
        ),
        "source_sha256": source_sha256,
        "source_filename": source_filename,
        "source_release_year": (
            record.get("source_release_year") or source.get("source_release_year")
        ),
        "extraction_version": extraction_version,
    }


def _values_equal(field: str, expected: Any, actual: Any) -> bool:
    if field in _OBSERVATION_JSON_COLUMNS:
        if expected is None or actual is None:
            return expected is None and actual is None
        return _canonical_json(expected) == _canonical_json(actual)
    return expected == actual


def _calibration_mapping(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    """The numeric contract used when a calibration ID is shared by documents."""

    return (
        payload.get("full_width_pt"),
        payload.get("full_width_pt_by_year"),
        payload.get("formula"),
        payload.get("units"),
        payload.get("method"),
        payload.get("tolerance_pt"),
    )


def _validate_existing_calibration(
    connection: sqlite3.Connection,
    calibration: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    calibration_id = calibration.get("calibration_id")
    if not calibration_id:
        return
    existing = connection.execute(
        """
        SELECT source_document_id, source_sha256, payload_json
        FROM calibrations
        WHERE calibration_id = ?
        """,
        (calibration_id,),
    ).fetchone()
    if existing is None:
        return
    try:
        old_payload = json.loads(existing[2])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageError(
            f"calibration_id {calibration_id!r} has invalid payload_json; refusing to append"
        ) from exc
    if not isinstance(old_payload, Mapping):
        raise StorageError(
            f"calibration_id {calibration_id!r} has a non-object payload_json; refusing to append"
        )
    same_document = (
        existing[0] == source.get("source_document_id")
        or existing[1] == source.get("source_sha256")
    )
    if same_document:
        if _canonical_json(old_payload) != _canonical_json(dict(calibration)):
            raise StorageError(
                f"source {source.get('source_document_id')} already has calibration "
                f"{calibration_id!r} with a different payload"
            )
    elif _calibration_mapping(old_payload) != _calibration_mapping(calibration):
        raise StorageError(
            f"calibration_id {calibration_id!r} already exists with a different numeric mapping"
        )


def _preflight_existing_source(
    connection: sqlite3.Connection,
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    source: Mapping[str, Any],
) -> bool:
    """Validate an existing source and return whether it is an exact rerun."""

    document_id = source["source_document_id"]
    source_sha256 = source["source_sha256"]
    existing_by_hash = connection.execute(
        "SELECT source_document_id FROM documents WHERE source_sha256 = ?",
        (source_sha256,),
    ).fetchone()
    if existing_by_hash and existing_by_hash[0] != document_id:
        raise StorageError(
            f"source hash {source_sha256} already belongs to document {existing_by_hash[0]}"
        )

    existing_document = connection.execute(
        """
        SELECT source_document_id, source_sha256, source_filename,
               source_release_year, extraction_version, record_count
        FROM documents
        WHERE source_document_id = ?
        """,
        (document_id,),
    ).fetchone()
    existing_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM source_observations WHERE source_document_id = ?",
            (document_id,),
        ).fetchone()[0]
    )
    if existing_document is None:
        if existing_count:
            raise StorageError(
                f"SQLite has {existing_count} observations for {document_id} but no document row"
            )
        _validate_existing_calibration(connection, calibration, source)
        return False

    expected_document = (
        document_id,
        source_sha256,
        source.get("source_filename") or "unknown.pdf",
        source.get("source_release_year"),
        source.get("extraction_version"),
        len(records),
    )
    for field, expected, actual in zip(_DOCUMENT_COLUMNS, expected_document, existing_document):
        if expected != actual:
            raise StorageError(
                f"source {document_id} already exists with different document field {field!r}"
            )
    if existing_count != len(records):
        raise StorageError(
            f"source {document_id} already has {existing_count} observations, "
            f"but the incoming extraction has {len(records)}"
        )

    existing_rows = connection.execute(
        "SELECT " + ", ".join(_OBSERVATION_COLUMNS) + " "
        "FROM source_observations WHERE source_document_id = ? ORDER BY row_id",
        (document_id,),
    ).fetchall()
    expected_rows = sorted(
        (_record_observation_values(record, source) for record in records),
        key=lambda row: str(row["row_id"]),
    )
    for expected, actual in zip(expected_rows, existing_rows):
        for index, field in enumerate(_OBSERVATION_COLUMNS):
            if not _values_equal(field, expected[field], actual[index]):
                raise StorageError(
                    f"source {document_id} already has a different observation payload "
                    f"for row_id {expected['row_id']!r} (field {field!r})"
                )
    _validate_existing_calibration(connection, calibration, source)
    return True


def _insert_document(
    connection: sqlite3.Connection,
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
) -> dict[str, Any]:
    source = _source_from_records(records, calibration)
    _validate_record_ids(records)
    document_id = source.get("source_document_id")
    source_sha256 = source.get("source_sha256")
    filename = source.get("source_filename") or "unknown.pdf"
    release_year = source.get("source_release_year")
    if not document_id or not source_sha256 or release_year is None:
        raise StorageError(
            "records/calibration do not contain source document and release-year provenance"
        )
    source = {**source, "source_filename": filename}
    already_present = _preflight_existing_source(connection, records, calibration, source)
    calibration_id = calibration.get("calibration_id")
    if already_present:
        return {**source, "calibration_id": calibration_id, "_already_present": True}

    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO documents
          (source_document_id, source_sha256, source_filename, source_release_year,
           extraction_version, record_count, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            source_sha256,
            filename,
            int(release_year),
            source.get("extraction_version"),
            len(records),
            now,
        ),
    )
    if calibration_id:
        payload = _json_value(calibration)
        existing = connection.execute(
            "SELECT payload_json FROM calibrations WHERE calibration_id = ?",
            (calibration_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO calibrations
                  (calibration_id, source_document_id, source_sha256, method, formula, units, full_width_pt, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    calibration_id,
                    document_id,
                    source_sha256,
                    calibration.get("method"),
                    calibration.get("formula"),
                    calibration.get("units"),
                    calibration.get("full_width_pt"),
                    payload,
                ),
            )
    return {**source, "calibration_id": calibration_id, "_already_present": False}


def _reject_same_release_conflicts(
    connection: sqlite3.Connection,
    values: Sequence[Mapping[str, Any]],
) -> None:
    """Refuse ambiguous precedence between two documents from one release."""

    for row in values:
        conflict = connection.execute(
            """
            SELECT row_id, source_document_id
            FROM source_observations
            WHERE university_key = ? AND year = ? AND source_release_year = ?
              AND source_document_id <> ?
            LIMIT 1
            """,
            (
                row["university_key"],
                row["year"],
                row["source_release_year"],
                row["source_document_id"],
            ),
        ).fetchone()
        if conflict is not None:
            raise StorageError(
                "two different source documents claim the same university/year "
                f"in release {row['source_release_year']}: {row['university']!r} "
                f"{row['year']} ({conflict[1]} and {row['source_document_id']})"
            )


def _refresh_current_observations(connection: sqlite3.Connection) -> None:
    """Rebuild the one-row-per-university/year queryable snapshot."""

    connection.execute("DELETE FROM observations")
    columns = ", ".join(_OBSERVATION_COLUMNS)
    connection.execute(
        f"""
        INSERT INTO observations ({columns})
        SELECT {", ".join("candidate." + column for column in _OBSERVATION_COLUMNS)}
        FROM source_observations AS candidate
        WHERE NOT EXISTS (
            SELECT 1
            FROM source_observations AS newer
            WHERE newer.university_key = candidate.university_key
              AND newer.year = candidate.year
              AND (
                  (CASE WHEN newer.score_basis IN ('measured_bar', 'visual_bar', 'exact_percentage', 'no_applications', 'no_applications_assumed_full') THEN 3 WHEN newer.score_basis = 'traffic_light' THEN 2 ELSE 1 END)
                  > (CASE WHEN candidate.score_basis IN ('measured_bar', 'visual_bar', 'exact_percentage', 'no_applications', 'no_applications_assumed_full') THEN 3 WHEN candidate.score_basis = 'traffic_light' THEN 2 ELSE 1 END)
                  OR (
                      (CASE WHEN newer.score_basis IN ('measured_bar', 'visual_bar', 'exact_percentage', 'no_applications', 'no_applications_assumed_full') THEN 3 WHEN newer.score_basis = 'traffic_light' THEN 2 ELSE 1 END)
                      = (CASE WHEN candidate.score_basis IN ('measured_bar', 'visual_bar', 'exact_percentage', 'no_applications', 'no_applications_assumed_full') THEN 3 WHEN candidate.score_basis = 'traffic_light' THEN 2 ELSE 1 END)
                      AND newer.source_release_year > candidate.source_release_year
                  )
              )
        )
        """
    )


def write_sqlite(
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    output_path: str | Path,
) -> Path:
    """Append a document to SQLite without replacing prior documents."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        _init_db(connection)
        _require_schema(connection)
        source = _insert_document(connection, records, calibration)
        if not source.get("_already_present"):
            rows = [_record_observation_values(record, source) for record in records]
            _reject_same_release_conflicts(connection, rows)
            for record in records:
                values = _record_observation_values(record, source)
                connection.execute(
                    "INSERT INTO source_observations ("
                    + ", ".join(_OBSERVATION_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in _OBSERVATION_COLUMNS)
                    + ")",
                    tuple(values[field] for field in _OBSERVATION_COLUMNS),
                )
            _refresh_current_observations(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return path


def write_outputs(
    records: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    output_dir: str | Path,
    *,
    allow_overwrite: bool = False,
) -> dict[str, Path]:
    """Write the standard exports and append the document to SQLite.

    The human-readable exports describe the most recent extraction in the
    directory.  SQLite is the durable multi-document store and is append-only
    by document hash.
    """

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    source = _source_from_records(records, calibration)
    names = {"rows_csv": "rows.csv", "records_json": "records.json",
             "calibration_json": "calibration.json", "sqlite": "bars.sqlite"}
    paths = {key: directory / name for key, name in names.items()}
    existing = any(path.exists() for path in paths.values())
    if existing and not allow_overwrite:
        try:
            old = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            old_cal = json.loads(paths["calibration_json"].read_text(encoding="utf-8"))
            if not all(path.exists() for path in paths.values()):
                raise ValueError("incomplete output set")
            if old.get("source", {}).get("source_sha256") != source.get("source_sha256"):
                raise ValueError("different source document")
            if old_cal.get("source_sha256") != source.get("source_sha256"):
                raise ValueError("different calibration source")
        except (OSError, ValueError, AttributeError) as exc:
            raise StorageError(f"Existing exports cannot be verified: {exc}. Choose a new output directory or use --force.") from exc
    # Validate and write everything in a staging directory before touching exports.
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=directory) as scratch:
        stage = Path(scratch)
        if paths["sqlite"].exists():
            with sqlite3.connect(paths["sqlite"]) as src, sqlite3.connect(stage / "bars.sqlite") as dst:
                src.backup(dst)
        write_sqlite(records, calibration, stage / "bars.sqlite")
        write_rows_csv(records, stage / "rows.csv")
        write_records_json(records, calibration, stage / "records.json")
        write_calibration_json(calibration, stage / "calibration.json")
        for key, name in names.items():
            os.replace(stage / name, paths[key])
    return paths


def import_records_json(records_json_path: str | Path, sqlite_path: str | Path) -> Path:
    """Import a previously exported records JSON into an existing database."""

    payload_path = Path(records_json_path)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise StorageError("records JSON must contain a list under 'records'")
    calibration_path = payload_path.with_name("calibration.json")
    if not calibration_path.exists():
        raise StorageError(f"matching calibration.json not found beside {payload_path}")
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    return write_sqlite(records, calibration, sqlite_path)


def query_sqlite(
    sqlite_path: str | Path,
    sql: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """Run a read-only SELECT query and return rows as dictionaries."""

    statement = sql.strip().lower()
    if not (statement.startswith("select") or statement.startswith("with") or statement.startswith("pragma")):
        raise StorageError("query accepts read-only SELECT, WITH, or PRAGMA statements")
    uri = f"file:{Path(sqlite_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(sql, tuple(parameters))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def read_current_records(sqlite_path: str | Path) -> list[dict[str, Any]]:
    """Return the active one-row-per-university/year snapshot as record dicts."""

    uri = f"file:{Path(sqlite_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        latest_release = connection.execute(
            "SELECT MAX(source_release_year) FROM documents"
        ).fetchone()[0]
        latest_roster_keys = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT university_key
                FROM source_observations
                WHERE source_release_year = ?
                """,
                (latest_release,),
            ).fetchall()
        }
        rows = connection.execute(
            "SELECT " + ", ".join(_OBSERVATION_COLUMNS) + " FROM observations "
            "ORDER BY country, university, year"
        ).fetchall()
    finally:
        connection.close()
    records: list[dict[str, Any]] = []
    for source in rows:
        record = dict(source)
        university_key = str(record.pop("university_key", ""))
        record["is_latest_roster"] = university_key in latest_roster_keys
        for stored, public in (
            ("bar_bbox_json", "bar_bbox"),
            ("status_box_bbox_json", "status_box_bbox"),
            ("geometry_json", "geometry"),
        ):
            value = record.pop(stored, None)
            record[public] = json.loads(value) if value is not None else None
        records.append(record)
    return records


def read_calibration_manifest(sqlite_path: str | Path) -> dict[str, Any]:
    """Return all source calibrations represented by a combined database."""

    uri = f"file:{Path(sqlite_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """
            SELECT d.source_document_id, d.source_filename, d.source_release_year,
                   c.payload_json
            FROM documents AS d
            LEFT JOIN calibrations AS c
              ON c.source_document_id = d.source_document_id
            ORDER BY d.source_release_year, d.source_filename
            """
        ).fetchall()
    finally:
        connection.close()
    sources = []
    for document_id, filename, release_year, payload in rows:
        sources.append(
            {
                "source_document_id": document_id,
                "source_filename": filename,
                "source_release_year": release_year,
                "calibration": json.loads(payload) if payload else None,
            }
        )
    return {
        "method": "per-source calibration manifest",
        "overlap_policy": (
            "one current observation per normalized university/year; "
            "the highest source_release_year supersedes older overlaps"
        ),
        "source_count": len(sources),
        "sources": sources,
        "calibrations": [
            source["calibration"]
            for source in sources
            if source["calibration"] is not None
        ],
    }


__all__ = [
    "CSV_FIELDS",
    "SCHEMA_VERSION",
    "StorageError",
    "import_records_json",
    "query_sqlite",
    "read_calibration_manifest",
    "read_current_records",
    "write_calibration_json",
    "write_outputs",
    "write_records_json",
    "write_rows_csv",
    "write_sqlite",
]
