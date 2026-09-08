"""Regression checks for the alternate 2026 PDF template and scale."""

from __future__ import annotations

import collections
import math
import unittest

import pdfplumber

from tumexchange.extract import extract_pdf

try:
    from .reference_data import SOURCE_PDF
    from .reference_data_2026 import (
        EXPECTED_NUMERIC_BAR_COUNT_2026,
        EXPECTED_STATUS_COUNTS_2026,
        EXPECTED_UNIVERSITY_COUNT_2026,
        SOURCE_PDF_2026,
        UFRJ_2024_SCORE_2026,
        UFRJ_2024_WIDTH_PT_2026,
        UFRJ_2025_FULL_WIDTH_PT_2026,
        YEARS_2026,
    )
except ImportError:  # pragma: no cover - unittest discovery
    from reference_data import SOURCE_PDF
    from reference_data_2026 import (
        EXPECTED_NUMERIC_BAR_COUNT_2026,
        EXPECTED_STATUS_COUNTS_2026,
        EXPECTED_UNIVERSITY_COUNT_2026,
        SOURCE_PDF_2026,
        UFRJ_2024_SCORE_2026,
        UFRJ_2024_WIDTH_PT_2026,
        UFRJ_2025_FULL_WIDTH_PT_2026,
        YEARS_2026,
    )


UFRJ = "Universidade Federal do Rio de Janeiro (UFRJ)"


class AlternateLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records, cls.calibration = extract_pdf(SOURCE_PDF_2026)
        cls.newer_records, _ = extract_pdf(SOURCE_PDF)

    def test_alternate_layout_counts_and_year_mapping(self) -> None:
        self.assertEqual(len(self.records), EXPECTED_UNIVERSITY_COUNT_2026 * 3)
        self.assertEqual(self.calibration["university_count"], EXPECTED_UNIVERSITY_COUNT_2026)
        self.assertEqual(self.calibration["measured_count"], EXPECTED_NUMERIC_BAR_COUNT_2026)
        self.assertEqual(tuple(sorted(map(int, self.calibration["year_columns"]))), YEARS_2026)
        self.assertEqual(self.calibration["source_release_year"], 2026)
        self.assertEqual(len({row["country"] for row in self.records}), 30)
        self.assertEqual(
            {
                row["university"]
                for row in self.records
                if row["country"] == "Israel" and row["year"] == 2023
            },
            {
                "Hebrew University of Jerusalem",
                "Israel Institute of Technology (Technion)",
                "Tel Aviv University",
            },
        )
        self.assertEqual(
            collections.Counter(row["status"] for row in self.records),
            collections.Counter(
                measured=EXPECTED_NUMERIC_BAR_COUNT_2026,
                no_applications=EXPECTED_STATUS_COUNTS_2026["no applications"],
                no_places=EXPECTED_STATUS_COUNTS_2026["no places"],
                no_cooperation=EXPECTED_STATUS_COUNTS_2026["no cooperation"],
            ),
        )

    def test_vector_geometry_has_distinct_scale_and_height(self) -> None:
        with pdfplumber.open(SOURCE_PDF_2026) as pdf:
            bars = [
                rect
                for page in pdf.pages
                for rect in page.rects
                if math.isclose(rect["x0"], 486.48, abs_tol=0.01)
                and math.isclose(rect["top"], 607.08, abs_tol=0.01)
            ]
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0]["height"], 5.52, places=2)
        self.assertAlmostEqual(bars[0]["width"], UFRJ_2025_FULL_WIDTH_PT_2026, places=5)

    def test_ufrj_reference_and_partial_bar_normalize_across_releases(self) -> None:
        current = {
            row["year"]: row
            for row in self.records
            if row["university"] == UFRJ
        }
        newer = {
            row["year"]: row
            for row in self.newer_records
            if row["university"] == UFRJ
        }
        self.assertAlmostEqual(current[2025]["full_width_pt"], UFRJ_2025_FULL_WIDTH_PT_2026, places=5)
        self.assertEqual(current[2025]["score_percent"], 100.0)
        self.assertAlmostEqual(current[2023]["full_width_pt"], 59.2799835, places=5)
        self.assertEqual(current[2023]["score_percent"], 100.0)
        self.assertAlmostEqual(current[2024]["raw_width_pt"], UFRJ_2024_WIDTH_PT_2026, places=5)
        self.assertAlmostEqual(current[2024]["score_percent"], UFRJ_2024_SCORE_2026, places=8)
        self.assertAlmostEqual(current[2024]["score_percent"], newer[2024]["score_percent"], delta=0.1)

    def test_all_common_numeric_overlaps_agree_after_per_source_calibration(self) -> None:
        old = {(row["university"], row["year"]): row for row in self.records}
        new = {(row["university"], row["year"]): row for row in self.newer_records}
        comparable = [
            (old[key]["score_percent"], new[key]["score_percent"])
            for key in old.keys() & new.keys()
            if old[key]["score_percent"] is not None and new[key]["score_percent"] is not None
        ]
        self.assertGreater(len(comparable), 150)
        self.assertLess(max(abs(left - right) for left, right in comparable), 0.2)


if __name__ == "__main__":
    unittest.main()

