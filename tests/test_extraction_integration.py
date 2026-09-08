"""Integration tests for extraction, calibration, and SQLite persistence."""

from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, FloatObject, NameObject

import tumexchange.extract as extract_module
from tumexchange.extract import CalibrationError, UnsupportedLayoutError, extract_pdf
from tumexchange.storage import (
    StorageError,
    import_records_json,
    query_sqlite,
    read_current_records,
    write_calibration_json,
    write_records_json,
    write_outputs,
    write_sqlite,
)

try:
    from .reference_data_2026 import SOURCE_PDF_2026
except ImportError:  # pragma: no cover - unittest discovery.
    from reference_data_2026 import SOURCE_PDF_2026

try:
    from .reference_data import (
        BRAZIL_2025_FULL_WIDTH_PT,
        EXPECTED_NUMERIC_BAR_COUNT,
        EXPECTED_SCORED_SLOT_COUNT,
        EXPECTED_STATUS_COUNTS,
        EXPECTED_STATUS_ROWS,
        EXPECTED_UNIVERSITY_COUNT,
        SOURCE_PDF,
        YEARS,
    )
except ImportError:  # pragma: no cover - exercised by unittest discovery.
    from reference_data import (
        BRAZIL_2025_FULL_WIDTH_PT,
        EXPECTED_NUMERIC_BAR_COUNT,
        EXPECTED_SCORED_SLOT_COUNT,
        EXPECTED_STATUS_COUNTS,
        EXPECTED_STATUS_ROWS,
        EXPECTED_UNIVERSITY_COUNT,
        SOURCE_PDF,
        YEARS,
    )


def _record(records: list[dict], university: str, year: int) -> dict:
    matches = [row for row in records if row["university"] == university and row["year"] == year]
    if len(matches) != 1:
        raise AssertionError(f"expected one record for {university!r} {year}, got {len(matches)}")
    return matches[0]


def _write_scaled_numeric_bar_pdf(source: Path, destination: Path, factor: float) -> None:
    """Scale only numeric bar paths while retaining the source page layout.

    The source PDF draws bars as four line segments rather than ``re``
    operators.  ``pypdf`` lets this fixture widen the 8.4pt numeric paths
    without changing labels, year columns, status boxes, or page coordinates.
    """

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    changed = 0
    for page in writer.pages:
        content = ContentStream(page.get_contents(), writer)
        operations = content.operations
        for index, (operands, operator) in enumerate(operations):
            if operator != b"m" or len(operands) != 2 or index + 4 >= len(operations):
                continue
            x0, y0 = map(float, operands)
            sequence = operations[index + 1 : index + 4]
            if [op for _, op in sequence] != [b"l", b"l", b"l"]:
                continue
            first, second, third = [values for values, _ in sequence]
            if any(len(values) != 2 for values in (first, second, third)):
                continue
            x1, y1 = map(float, first)
            x2, y2 = map(float, second)
            x3, y3 = map(float, third)
            if (
                abs(x2 - x1) > 1e-3
                or abs(y1 - y0) > 1e-3
                or abs(x3 - x0) > 1e-3
                or abs(y3 - y2) > 1e-3
                or abs(abs(y2 - y0) - 11.2) >= 0.05
            ):
                continue
            new_x = x0 + factor * (x1 - x0)
            first[0] = FloatObject(new_x)
            second[0] = FloatObject(new_x)
            changed += 1
        page[NameObject("/Contents")] = writer._add_object(content)

    if changed != 327:
        raise AssertionError(f"scaled fixture changed {changed} numeric bars, expected 327")
    with destination.open("wb") as handle:
        writer.write(handle)


class ExtractionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records, cls.calibration = extract_pdf(SOURCE_PDF)

    def test_source_has_all_university_year_cells_and_status_policy(self) -> None:
        self.assertEqual(len(self.records), EXPECTED_UNIVERSITY_COUNT * len(YEARS))
        self.assertEqual(self.calibration["university_count"], EXPECTED_UNIVERSITY_COUNT)
        self.assertEqual(self.calibration["measured_count"], EXPECTED_NUMERIC_BAR_COUNT)
        self.assertEqual(self.calibration["no_applications_count"], 17)
        self.assertEqual(self.calibration["unavailable_count"], 31)
        self.assertEqual(
            Counter(row["status"] for row in self.records),
            Counter(
                measured=327,
                no_applications=EXPECTED_STATUS_COUNTS["no applications"],
                no_places=EXPECTED_STATUS_COUNTS["no places"],
                no_cooperation=EXPECTED_STATUS_COUNTS["no cooperation"],
            ),
        )
        self.assertEqual(sum(row["score_percent"] is not None for row in self.records), EXPECTED_SCORED_SLOT_COUNT)

    def test_full_bars_calibrate_across_years_and_snap(self) -> None:
        full_reference = [
            row
            for row in self.records
            if row["status"] == "measured"
            and math.isclose(row["raw_width_pt"], BRAZIL_2025_FULL_WIDTH_PT, abs_tol=0.001)
        ]
        self.assertEqual(len(full_reference), 71)
        self.assertTrue(all(row["score_percent"] == 100.0 for row in full_reference))

        brazil_2025 = [row for row in self.records if row["country"] == "Brazil" and row["year"] == 2025]
        self.assertEqual(len(brazil_2025), 3)
        self.assertTrue(all(row["score_percent"] == 100.0 for row in brazil_2025))
        self.assertTrue(all(math.isclose(row["full_width_pt"], BRAZIL_2025_FULL_WIDTH_PT, abs_tol=1e-6) for row in brazil_2025))

    def test_text_statuses_are_not_silent_zeroes(self) -> None:
        actual_statuses = {
            (row["university"], row["year"], row["status"])
            for row in self.records
            if row["status"] != "measured"
        }
        expected_statuses = {
            (university, year, status.replace(" ", "_"))
            for university, year, status in EXPECTED_STATUS_ROWS
        }
        self.assertEqual(actual_statuses, expected_statuses)

        no_applications = [row for row in self.records if row["status"] == "no_applications"]
        self.assertEqual(len(no_applications), 17)
        self.assertTrue(all(row["score_percent"] == 100.0 for row in no_applications))
        self.assertTrue(all(row["bar_fraction"] == 1.0 for row in no_applications))
        self.assertTrue(all(row["raw_width_pt"] is None for row in no_applications))
        self.assertTrue(all(row["bar_bbox"] is None for row in no_applications))
        self.assertTrue(all(row["score_basis"] == "no_applications_assumed_full" for row in no_applications))

        unavailable = [row for row in self.records if row["status"] in {"no_places", "no_cooperation"}]
        self.assertEqual(len(unavailable), 31)
        self.assertTrue(all(row["score_percent"] is None for row in unavailable))
        self.assertTrue(all(row["bar_fraction"] is None for row in unavailable))
        self.assertTrue(all(row["raw_width_pt"] is None for row in unavailable))

    def test_per_year_mapping_and_named_measurements(self) -> None:
        bit = [row for row in self.records if row["university"] == "Burkina Institute of Technology (BIT)"]
        self.assertEqual([(row["year"], row["status"]) for row in bit], [(2024, "no_cooperation"), (2025, "no_cooperation"), (2026, "no_applications")])

        uiuc = [row for row in self.records if row["university"] == "University of Illinois at Urbana-Champaign (UIUC)"]
        self.assertEqual([(row["year"], row["status"]) for row in uiuc], [(2024, "measured"), (2025, "no_places"), (2026, "measured")])

        itba = _record(self.records, "Instituto Tecnológico de Buenos Aires (ITBA)", 2024)
        sydney = _record(self.records, "University of Sydney", 2024)
        self.assertAlmostEqual(itba["raw_width_pt"], 28.32, places=2)
        self.assertAlmostEqual(
            itba["score_percent"],
            itba["raw_width_pt"] / itba["full_width_pt"] * 100,
            places=8,
        )
        self.assertAlmostEqual(sydney["raw_width_pt"], 0.84, places=2)
        self.assertGreater(sydney["score_percent"], 0.0)

        for row in self.records:
            self.assertEqual(row["column_label"], str(row["year"]))
            self.assertIn(row["year"], YEARS)

    def test_per_year_override_is_reusable_and_not_global(self) -> None:
        # Deliberately use different reference widths by year.  This catches
        # implementations that apply only one scalar to every annual column.
        override = {
            "calibration_id": "calibration:test:per-year",
            "full_width_pt_by_year": {"2024": 100.0, "2025": 200.0, "2026": 50.0},
        }
        records, calibration = extract_pdf(SOURCE_PDF, calibration_override=override)
        self.assertEqual(calibration["full_width_pt_by_year"], {"2024": 100.0, "2025": 200.0, "2026": 50.0})
        itba = _record(records, "Instituto Tecnológico de Buenos Aires (ITBA)", 2024)
        campinas = _record(records, "Universidade Estadual de Campinas (UNICAMP)", 2025)
        anu = _record(records, "Australian National University", 2026)
        self.assertAlmostEqual(itba["score_percent"], itba["raw_width_pt"] / 100 * 100, places=8)
        self.assertAlmostEqual(campinas["score_percent"], campinas["raw_width_pt"] / 200 * 100, places=8)
        self.assertAlmostEqual(anu["score_percent"], anu["raw_width_pt"] / 50 * 100, places=8)

    def test_invalid_or_unsupported_calibration_fails_loudly(self) -> None:
        with self.assertRaises(CalibrationError):
            extract_pdf(SOURCE_PDF, calibration_override={"full_width_pt_by_year": {"2024": 100.0}})
        with self.assertRaises(CalibrationError):
            extract_pdf(SOURCE_PDF, calibration_override={"full_width_pt": 0})
        with self.assertRaises(CalibrationError):
            extract_pdf(SOURCE_PDF, reference_country="Atlantis")

    def test_stale_cross_source_calibration_requires_explicit_opt_in(self) -> None:
        stale = {
            "source_sha256": "f" * 64,
            "full_width_pt_by_year": {str(year): BRAZIL_2025_FULL_WIDTH_PT for year in YEARS},
        }
        with self.assertRaises(CalibrationError):
            extract_pdf(SOURCE_PDF, calibration_override=stale)

        records, calibration = extract_pdf(
            SOURCE_PDF,
            calibration_override=stale,
            allow_cross_source_calibration=True,
        )
        self.assertEqual(len(records), 375)
        self.assertEqual(calibration["calibration_source_sha256"], "f" * 64)
        self.assertNotEqual(calibration["source_sha256"], "f" * 64)

    def test_ambiguous_cells_are_rejected_without_explicit_incomplete_opt_in(self) -> None:
        real_prepare_rows = extract_module._prepare_rows

        def inject_ambiguous_cell(pages, columns):
            rows = real_prepare_rows(pages, columns)
            rows[0]["per_year"][2024] = {
                "status": "ambiguous",
                "status_text": None,
                "bar": None,
                "status_box": None,
                "notes": "test ambiguity",
            }
            return rows

        with patch.object(extract_module, "_prepare_rows", side_effect=inject_ambiguous_cell):
            with self.assertRaises(UnsupportedLayoutError):
                extract_pdf(SOURCE_PDF)
            records, _ = extract_pdf(SOURCE_PDF, allow_incomplete=True)
        self.assertEqual(_record(records, "Instituto Tecnológico de Buenos Aires (ITBA)", 2024)["status"], "ambiguous")

    def test_numeric_bar_width_scaling_recalibrates_without_changing_scores(self) -> None:
        baseline_scores = {(row["university"], row["year"]): row["score_percent"] for row in self.records}
        with tempfile.TemporaryDirectory(prefix="tumexchange-scaled-") as tmp:
            scaled_path = Path(tmp) / "scaled-bars.pdf"
            _write_scaled_numeric_bar_pdf(SOURCE_PDF, scaled_path, 1.25)
            records, calibration = extract_pdf(scaled_path)

        self.assertEqual(len(records), len(self.records))
        self.assertEqual(calibration["measured_count"], 327)
        self.assertAlmostEqual(calibration["full_width_pt"], BRAZIL_2025_FULL_WIDTH_PT * 1.25, places=5)
        scaled_scores = {(row["university"], row["year"]): row["score_percent"] for row in records}
        self.assertEqual(set(scaled_scores), set(baseline_scores))
        for key, score in baseline_scores.items():
            if score is None:
                self.assertIsNone(scaled_scores[key])
            else:
                self.assertAlmostEqual(scaled_scores[key], score, places=5)
        self.assertAlmostEqual(
            _record(records, "Instituto Tecnológico de Buenos Aires (ITBA)", 2024)["raw_width_pt"],
            _record(self.records, "Instituto Tecnológico de Buenos Aires (ITBA)", 2024)["raw_width_pt"] * 1.25,
            places=5,
        )

    def test_sqlite_keeps_source_history_and_newest_overlap_once(self) -> None:
        older_records, older_calibration = extract_pdf(SOURCE_PDF_2026)
        with tempfile.TemporaryDirectory(prefix="tumexchange-test-") as tmp:
            root = Path(tmp)
            db_path = root / "bars.sqlite"
            write_sqlite(older_records, older_calibration, db_path)
            write_sqlite(self.records, self.calibration, db_path)
            write_sqlite(older_records, older_calibration, db_path)
            write_sqlite(self.records, self.calibration, db_path)
            counts = query_sqlite(
                db_path,
                "SELECT (SELECT COUNT(*) FROM documents) AS documents, "
                "(SELECT COUNT(*) FROM source_observations) AS source_observations, "
                "(SELECT COUNT(*) FROM observations) AS observations",
            )
            self.assertEqual(
                counts,
                [{"documents": 2, "source_observations": 753, "observations": 517}],
            )
            self.assertEqual(
                query_sqlite(
                    db_path,
                    """
                    SELECT year, source_release_year
                    FROM observations
                    WHERE university = 'Universidade Federal do Rio de Janeiro (UFRJ)'
                    ORDER BY year
                    """,
                ),
                [
                    {"year": 2023, "source_release_year": 2026},
                    {"year": 2024, "source_release_year": 2027},
                    {"year": 2025, "source_release_year": 2027},
                    {"year": 2026, "source_release_year": 2027},
                ],
            )
            current = read_current_records(db_path)
            self.assertEqual(
                len(
                    [
                        row
                        for row in current
                        if row["university"]
                        in {"National Taiwan University", "National Taiwan University (NTU)"}
                    ]
                ),
                4,
            )
            historical_only = {
                row["university"]
                for row in current
                if not row["is_latest_roster"]
            }
            self.assertEqual(len(historical_only), 8)
            self.assertIn("Universidade de São Paulo (USP)", historical_only)

            reverse_path = root / "reverse.sqlite"
            write_sqlite(self.records, self.calibration, reverse_path)
            write_sqlite(older_records, older_calibration, reverse_path)
            self.assertEqual(
                query_sqlite(
                    reverse_path,
                    "SELECT row_id FROM observations ORDER BY university_key, year",
                ),
                query_sqlite(
                    db_path,
                    "SELECT row_id FROM observations ORDER BY university_key, year",
                ),
            )

    def test_json_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tumexchange-json-test-") as tmp:
            root = Path(tmp)
            records_path = write_records_json(self.records, self.calibration, root / "records.json")
            write_calibration_json(self.calibration, root / "calibration.json")
            db_path = root / "imported.sqlite"
            import_records_json(records_path, db_path)
            import_records_json(records_path, db_path)
            self.assertEqual(query_sqlite(db_path, "SELECT COUNT(*) AS n FROM documents"), [{"n": 1}])
            self.assertEqual(query_sqlite(db_path, "SELECT COUNT(*) AS n FROM observations"), [{"n": 375}])

    def test_same_release_overlap_is_rejected_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tumexchange-conflict-") as tmp:
            db_path = Path(tmp) / "bars.sqlite"
            write_sqlite(self.records, self.calibration, db_path)
            record = copy.deepcopy(self.records[0])
            record.update(
                source_document_id="sha256:conflicting000",
                source_sha256="c" * 64,
                source_filename="TUMexchange_Chances_2027_amended.pdf",
                row_id="sha256:conflicting000:1:2024",
                calibration_id="calibration:test:same-release",
            )
            calibration = copy.deepcopy(self.calibration)
            calibration.update(
                source_document_id=record["source_document_id"],
                source_sha256=record["source_sha256"],
                source_filename=record["source_filename"],
                calibration_id=record["calibration_id"],
                record_count=1,
                university_count=1,
            )
            with self.assertRaises(StorageError):
                write_sqlite([record], calibration, db_path)
            self.assertEqual(
                query_sqlite(db_path, "SELECT COUNT(*) AS n FROM documents"),
                [{"n": 1}],
            )

    def test_changed_calibration_for_same_pdf_cannot_diverge_exports_and_sqlite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tumexchange-output-guard-") as tmp:
            output_dir = Path(tmp) / "output"
            write_outputs(self.records, self.calibration, output_dir)
            original_csv = (output_dir / "rows.csv").read_bytes()
            original_score = _record(
                query_sqlite(
                    output_dir / "bars.sqlite",
                    "SELECT * FROM observations WHERE university = ? AND year = ?",
                    ("Instituto Tecnológico de Buenos Aires (ITBA)", 2024),
                ),
                "Instituto Tecnológico de Buenos Aires (ITBA)",
                2024,
            )["score_percent"]

            changed_records, changed_calibration = extract_pdf(
                SOURCE_PDF,
                calibration_override={"full_width_pt": 60.0},
            )
            self.assertNotEqual(changed_calibration["calibration_id"], self.calibration["calibration_id"])
            with self.assertRaises(StorageError):
                write_outputs(changed_records, changed_calibration, output_dir)

            self.assertEqual((output_dir / "rows.csv").read_bytes(), original_csv)
            after = query_sqlite(
                output_dir / "bars.sqlite",
                "SELECT score_percent FROM observations WHERE university = ? AND year = ?",
                ("Instituto Tecnológico de Buenos Aires (ITBA)", 2024),
            )
            self.assertEqual(after, [{"score_percent": original_score}])


if __name__ == "__main__":
    unittest.main()
