"""Independent regression checks for the preserved and combined artifacts."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import sqlite3
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from tumexchange.storage import CSV_FIELDS as STORAGE_CSV_FIELDS
from tumexchange.storage import _university_key, read_current_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_JSON = PROJECT_ROOT / "data" / "output" / "TUMexchange_Chances_2027" / "records.json"
ORIGINAL_HTML = PROJECT_ROOT / "data" / "output" / "TUMexchange_Chances_2027" / "report.html"
COMBINED_HTML = PROJECT_ROOT / "data" / "output" / "combined" / "combined-report.html"
COMBINED_DB = PROJECT_ROOT / "data" / "output" / "combined" / "bars.sqlite"
COMBINED_CSV = PROJECT_ROOT / "data" / "output" / "combined" / "rows.csv"

EXPECTED_ORIGINAL_SHA256 = (
    "8223a69416480025529b7800025625395d7603375a3511f04f3e6e5fccbad1d8"
)

EXACT_FIELDS = (
    "source_document_id",
    "status",
    "score_percent",
    "score_basis",
    "raw_width_pt",
    "full_width_pt",
    "calibration_id",
)

PROVENANCE_FIELDS = (
    "source_document_id",
    "source_sha256",
    "source_filename",
    "source_release_year",
    "calibration_id",
)

CSV_INTEGER_FIELDS = {"year", "page", "row_index", "source_release_year"}
CSV_FLOAT_FIELDS = {
    "score_percent",
    "bar_fraction",
    "raw_width_pt",
    "full_width_pt",
    "status_box_width_pt",
}
CSV_JSON_FIELDS = {"bar_bbox", "status_box_bbox", "geometry"}
CSV_BOOLEAN_FIELDS = {"is_latest_roster"}


def _record_key(record: dict[str, Any]) -> tuple[str, int]:
    return _university_key(record["university"]), int(record["year"])


def _index_records(records: list[dict[str, Any]], label: str) -> dict[tuple[str, int], dict[str, Any]]:
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if key in indexed:
            raise AssertionError(f"{label} contains duplicate normalized key/year {key!r}")
        indexed[key] = record
    return indexed


class _ReportParser(HTMLParser):
    """Capture embedded JSON and raw record details without third-party HTML packages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str], str]] = []
        self.raw_records: list[str] = []
        self._script_attrs: dict[str, str] | None = None
        self._script_parts: list[str] | None = None
        self._raw_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag.lower() == "script":
            self._script_attrs = attributes
            self._script_parts = []
        elif (
            tag.lower() == "pre"
            and "raw-record" in attributes.get("class", "").split()
        ):
            self._raw_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)
        if self._raw_parts is not None:
            self._raw_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._script_parts is not None:
            self.scripts.append(
                (self._script_attrs or {}, "".join(self._script_parts))
            )
            self._script_attrs = None
            self._script_parts = None
        elif tag == "pre" and self._raw_parts is not None:
            self.raw_records.append("".join(self._raw_parts))
            self._raw_parts = None


def _read_html(path: Path) -> tuple[bytes, str, _ReportParser]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    parser = _ReportParser()
    parser.feed(text)
    parser.close()
    return raw, text, parser


def _report_data(parser: _ReportParser) -> Any:
    payloads = [
        body
        for attributes, body in parser.scripts
        if attributes.get("id") == "report-data"
    ]
    if len(payloads) != 1:
        raise AssertionError(f"expected one report-data script, got {len(payloads)}")
    return json.loads(payloads[0].strip())


def _raw_records(parser: _ReportParser) -> list[dict[str, Any]]:
    return [
        json.loads(html.unescape(record).strip())
        for record in parser.raw_records
    ]


def _db_public_record(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    for stored, public in (
        ("bar_bbox_json", "bar_bbox"),
        ("status_box_bbox_json", "status_box_bbox"),
        ("geometry_json", "geometry"),
    ):
        value = record.pop(stored)
        record[public] = json.loads(value) if value is not None else None
    return record


def _parse_csv_record(row: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for field, value in row.items():
        if field in CSV_JSON_FIELDS:
            parsed[field] = json.loads(value) if value else None
        elif field in CSV_INTEGER_FIELDS:
            parsed[field] = int(value) if value else None
        elif field in CSV_FLOAT_FIELDS:
            parsed[field] = float(value) if value else None
        elif field in CSV_BOOLEAN_FIELDS:
            parsed[field] = value in {"True", "true", "1"}
        else:
            parsed[field] = value if value else None
    return parsed


class CombinedArtifactsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_payload = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
        cls.source_records = cls.source_payload["records"]
        cls.source_by_key = _index_records(cls.source_records, "2027 source JSON")

        uri = f"file:{COMBINED_DB.resolve()}?mode=ro"
        cls.connection = sqlite3.connect(uri, uri=True)
        cls.connection.row_factory = sqlite3.Row
        cls.current_records = read_current_records(COMBINED_DB)
        cls.current_by_key = _index_records(
            cls.current_records, "combined current observations"
        )
        cls.source_2027_records = [
            _db_public_record(dict(row))
            for row in cls.connection.execute(
                "SELECT * FROM source_observations "
                "WHERE source_release_year = 2027 "
                "ORDER BY university_key, year"
            )
        ]
        cls.source_2027_by_key = _index_records(
            cls.source_2027_records, "combined 2027 source observations"
        )
        cls.source_2026_records = [
            dict(row)
            for row in cls.connection.execute(
                "SELECT * FROM source_observations "
                "WHERE source_release_year = 2026 "
                "ORDER BY university_key, year"
            )
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    @classmethod
    def _query(cls, sql: str) -> list[dict[str, Any]]:
        return [dict(row) for row in cls.connection.execute(sql)]

    def _assert_fields(
        self,
        expected: dict[str, Any],
        actual: dict[str, Any],
        fields: tuple[str, ...],
        context: str,
    ) -> None:
        for field in fields:
            self.assertIn(field, expected, f"{context} is missing {field}")
            self.assertIn(field, actual, f"{context} is missing {field}")
            self.assertEqual(
                actual[field],
                expected[field],
                f"{context} differs in {field}",
            )

    def test_preserved_original_html_is_readable_unchanged_and_matches_source(self) -> None:
        raw, text, parser = _read_html(ORIGINAL_HTML)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), EXPECTED_ORIGINAL_SHA256)
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn("<title>TUMexchange bar register</title>", text)
        self.assertEqual(
            len(re.findall(r'<tr\b[^>]*class="data-row"[^>]*>', text)),
            375,
        )

        embedded = _report_data(parser)
        self.assertIsInstance(embedded, list)
        self.assertEqual(len(embedded), 375)
        self.assertEqual(len(parser.raw_records), 375)
        raw_records = _raw_records(parser)
        embedded_by_key = _index_records(embedded, "original HTML report-data")
        raw_by_key = _index_records(raw_records, "original HTML raw details")
        self.assertEqual(set(embedded_by_key), set(self.source_by_key))
        self.assertEqual(embedded_by_key, raw_by_key)
        for key, source_record in self.source_by_key.items():
            self._assert_fields(
                source_record,
                embedded_by_key[key],
                EXACT_FIELDS,
                f"original HTML record {key!r}",
            )

    def test_combined_current_preserves_all_2027_fields_and_precedence(self) -> None:
        self.assertEqual(self.source_payload["record_count"], 375)
        self.assertEqual(len(self.source_records), 375)
        self.assertEqual(
            Counter(record["status"] for record in self.source_records),
            Counter(
                measured=327,
                no_applications=17,
                no_cooperation=26,
                no_places=5,
            ),
        )
        self.assertEqual(self.source_payload["source"]["source_release_year"], 2027)
        self.assertTrue(
            all(record["source_release_year"] == 2027 for record in self.source_records)
        )

        self.assertEqual(len(self.source_2027_records), 375)
        self.assertEqual(len(self.source_2026_records), 378)
        self.assertEqual(len(self.current_records), 517)
        self.assertEqual(
            self._query(
                "SELECT source_release_year, COUNT(*) AS count "
                "FROM source_observations GROUP BY source_release_year "
                "ORDER BY source_release_year"
            ),
            [
                {"source_release_year": 2026, "count": 378},
                {"source_release_year": 2027, "count": 375},
            ],
        )
        self.assertEqual(
            self._query(
                "SELECT source_release_year, COUNT(*) AS count "
                "FROM observations GROUP BY source_release_year "
                "ORDER BY source_release_year"
            ),
            [
                {"source_release_year": 2026, "count": 142},
                {"source_release_year": 2027, "count": 375},
            ],
        )

        duplicate_current = self._query(
            "SELECT university_key, year, COUNT(*) AS count "
            "FROM observations "
            "GROUP BY university_key, year "
            "HAVING COUNT(*) > 1"
        )
        self.assertEqual(duplicate_current, [])

        for key, source_record in self.source_by_key.items():
            self.assertIn(key, self.source_2027_by_key)
            self.assertIn(key, self.current_by_key)
            self._assert_fields(
                source_record,
                self.source_2027_by_key[key],
                EXACT_FIELDS,
                f"SQLite 2027 history record {key!r}",
            )
            self._assert_fields(
                source_record,
                self.current_by_key[key],
                EXACT_FIELDS,
                f"SQLite current record {key!r}",
            )

        keys_2027 = set(self.source_by_key)
        keys_2026 = {
            (_university_key(record["university"]), int(record["year"]))
            for record in self.source_2026_records
        }
        overlap = keys_2027 & keys_2026
        old_only = keys_2026 - keys_2027
        self.assertEqual(len(overlap), 236)
        self.assertEqual(len(old_only), 142)
        self.assertEqual(set(self.source_2027_by_key), keys_2027)
        self.assertEqual(set(self.current_by_key), keys_2027 | keys_2026)
        self.assertTrue(
            all(self.current_by_key[key]["source_release_year"] == 2027 for key in overlap)
        )
        self.assertTrue(
            all(self.current_by_key[key]["source_release_year"] == 2026 for key in old_only)
        )

        current_2023 = [
            record for record in self.current_records if record["year"] == 2023
        ]
        self.assertEqual(len(current_2023), 126)
        self.assertTrue(
            all(record["source_release_year"] == 2026 for record in current_2023)
        )
        self.assertEqual(
            {_university_key(record["university"]) for record in current_2023},
            {key[0] for key in old_only if key[1] == 2023},
        )
        universities_2027 = {key[0] for key in keys_2027}
        universities_2026 = {key[0] for key in keys_2026}
        self.assertEqual(len(universities_2026 - universities_2027), 8)
        self.assertEqual(len({key[0] for key in self.current_by_key}), 133)
        historical_only = {
            record["university"]
            for record in self.current_records
            if not record["is_latest_roster"]
        }
        self.assertEqual(len(historical_only), 8)
        self.assertIn("Universidade de São Paulo (USP)", historical_only)

    def test_combined_rows_csv_matches_current_sqlite_snapshot(self) -> None:
        with COMBINED_CSV.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames or ()), tuple(STORAGE_CSV_FIELDS))
            rows = [_parse_csv_record(row) for row in reader]

        self.assertEqual(len(rows), 517)
        csv_by_key = _index_records(rows, "combined rows.csv")
        self.assertEqual(set(csv_by_key), set(self.current_by_key))
        for key, current_record in self.current_by_key.items():
            csv_record = csv_by_key[key]
            for field in STORAGE_CSV_FIELDS:
                expected = current_record.get(field)
                if expected == "":
                    expected = None
                self.assertEqual(
                    csv_record.get(field),
                    expected,
                    f"rows.csv record {key!r} differs in {field}",
                )

    def test_combined_html_embeds_grouped_current_annual_provenance(self) -> None:
        _, text, parser = _read_html(COMBINED_HTML)
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn("<title>TUMexchange Acceptance Chances</title>", text)
        self.assertNotIn("@@UNIVERSITIES@@", text)
        self.assertNotIn("@@ANNUAL@@", text)
        payload = _report_data(parser)
        groups = payload["groups"]
        historical_groups = [group for group in groups if group.get("records")]
        self.assertEqual(len(historical_groups), 133)
        for group in groups:
            if not group.get("records"):
                self.assertTrue(group.get("profiles"))
                self.assertIsNone(group.get("mean_score"))
                self.assertEqual(group.get("scored_count"), 0)
        self.assertEqual(
            text.count('class="data-row university-row"'),
            len(groups),
        )
        self.assertEqual(text.count('class="university-detail-row"'), len(groups))
        self.assertEqual(text.count('class="annual-data-row"'), 517)
        self.assertNotIn('class="annual-detail-row"', text)
        self.assertIn('class="annual-table"', text)
        self.assertIn("Acceptance rate", text)
        self.assertIn("Release 2026", text)
        self.assertIn("Release 2027", text)
        self.assertIn("Not listed in release 2027", text)
        self.assertEqual(text.count("Not listed in release 2027"), 8)
        self.assertIn('data-sort-column="university"', text)
        self.assertIn('data-sort-column="score"', text)
        self.assertIn("const compareGroups", text)
        self.assertIn("const cycleSort", text)
        self.assertNotIn('id="score-sort"', text)

        payload = _report_data(parser)
        self.assertIsInstance(payload, dict)
        groups = payload.get("groups")
        self.assertIsInstance(groups, list)
        self.assertGreaterEqual(len(groups), 133)

        annual_records: list[dict[str, Any]] = []
        for group in groups:
            self.assertIsInstance(group.get("university"), str)
            group_key = _university_key(group["university"])
            records = group.get("records")
            self.assertIsInstance(records, list)
            for record in records:
                self.assertEqual(
                    _university_key(record["university"]),
                    group_key,
                    f"HTML group {group['university']!r} contains another university",
                )
                annual_records.append(record)

        self.assertEqual(len(annual_records), 517)
        html_by_key = _index_records(annual_records, "combined HTML annual payload")
        self.assertEqual(set(html_by_key), set(self.current_by_key))
        html_compare_fields = PROVENANCE_FIELDS + (
            "status",
            "score_percent",
            "score_basis",
            "raw_width_pt",
        )
        for key, current_record in self.current_by_key.items():
            html_record = html_by_key[key]
            self._assert_fields(
                current_record,
                html_record,
                html_compare_fields,
                f"combined HTML annual record {key!r}",
            )
            if current_record.get("full_width_pt") is not None:
                self.assertEqual(
                    html_record.get("full_width_pt"),
                    current_record["full_width_pt"],
                    f"combined HTML annual record {key!r} differs in full_width_pt",
                )

        self.assertEqual(len(parser.raw_records), 0)
        self.assertNotIn('class="raw-record"', text)


if __name__ == "__main__":
    unittest.main()
