"""Unit tests for the exact 2016/17 TUMexchange percentage extractor and cross-validator."""

import json
import tempfile
import unittest
from pathlib import Path

from tumexchange.exact_2016_extract import (
    DEFAULT_CANONICAL_DB,
    DEFAULT_SOURCE_PDF,
    DEFAULT_VALIDATION_PDF,
    color_to_tier,
    cross_validate,
    extract_exact_percentages,
    extract_validation_tiers,
    load_canonical_universities,
    match_university_canonical,
    tier_for_percentage,
    write_exact_2016_outputs,
)


class TestExact2016Extract(unittest.TestCase):
    """Test extraction of numerical admission rates from the 2016/17 PDF."""

    def test_tier_for_percentage(self) -> None:
        """Verify standard 4-tier threshold classifications."""
        self.assertEqual(tier_for_percentage(0.0), "Red")
        self.assertEqual(tier_for_percentage(15.5), "Red")
        self.assertEqual(tier_for_percentage(25.0), "Red")
        self.assertEqual(tier_for_percentage(25.1), "Yellow")
        self.assertEqual(tier_for_percentage(50.0), "Yellow")
        self.assertEqual(tier_for_percentage(50.1), "Light Green")
        self.assertEqual(tier_for_percentage(75.0), "Light Green")
        self.assertEqual(tier_for_percentage(75.1), "Dark Green")
        self.assertEqual(tier_for_percentage(100.0), "Dark Green")

    def test_color_to_tier(self) -> None:
        """Verify RGB tuple mapping to semantic tier names."""
        self.assertEqual(color_to_tier((1.0, 0.0, 0.0)), "Red")
        self.assertEqual(color_to_tier((1.0, 1.0, 0.0)), "Yellow")
        self.assertEqual(color_to_tier((0.573, 0.816, 0.314)), "Light Green")
        self.assertEqual(color_to_tier((0.0, 0.69, 0.314)), "Dark Green")
        self.assertIsNone(color_to_tier(0.0))
        self.assertIsNone(color_to_tier((0.1, 0.2, 0.3)))

    def test_canonical_matching(self) -> None:
        """Verify canonical university name resolution against SQLite database."""
        canonical_map = load_canonical_universities(DEFAULT_CANONICAL_DB)
        self.assertGreater(len(canonical_map), 100)

        # Exact alias mappings
        country, uni = match_university_canonical(
            "Instituto Tecnológico de Buenos Aires", canonical_map, "Argentinien"
        )
        self.assertEqual(country, "Argentina")
        self.assertEqual(uni, "Instituto Tecnológico de Buenos Aires (ITBA)")

        country, uni = match_university_canonical("Technion", canonical_map, "Israel")
        self.assertEqual(country, "Israel")
        self.assertEqual(uni, "Israel Institute of Technology (Technion)")

        country, uni = match_university_canonical("Osaka University", canonical_map, "Japan")
        self.assertEqual(country, "Japan")
        self.assertEqual(uni, "The University of Osaka")

        country, uni = match_university_canonical("Georgia Tech", canonical_map, "USA")
        self.assertEqual(country, "USA")
        self.assertEqual(uni, "Georgia Institute of Technology (Georgia Tech)")

        # Historical university not in bars.sqlite
        country, uni = match_university_canonical(
            "Deakin University", canonical_map, "Australien"
        )
        self.assertEqual(country, "Australia")
        self.assertEqual(uni, "Deakin University")

    def test_extract_exact_percentages(self) -> None:
        """Verify end-to-end extraction from the 2016/17 source PDF."""
        records = extract_exact_percentages(
            pdf_path=DEFAULT_SOURCE_PDF,
            canonical_db_path=DEFAULT_CANONICAL_DB,
            include_unmeasured=False,
        )
        self.assertEqual(len(records), 110)

        # Check invariant fields across all records
        for r in records:
            self.assertEqual(r["year"], 2017)
            self.assertEqual(r["source_release_year"], 2017)
            self.assertEqual(r["status"], "measured")
            self.assertEqual(r["score_basis"], "exact_percentage")
            self.assertIsInstance(r["score_percent"], float)
            self.assertGreaterEqual(r["score_percent"], 0.0)
            self.assertLessEqual(r["score_percent"], 100.0)
            self.assertAlmostEqual(r["bar_fraction"], r["score_percent"] / 100.0, places=4)
            self.assertTrue(r["row_id"].startswith("sha256:"))

        # Verify key spot-checked institutions
        by_raw = {r["raw_label"]: r for r in records}
        self.assertAlmostEqual(by_raw["Queensland University of Technology"]["score_percent"], 17.9)
        self.assertAlmostEqual(by_raw["University of Adelaide"]["score_percent"], 55.0)
        self.assertAlmostEqual(by_raw["The University of Queensland"]["score_percent"], 63.0)
        self.assertAlmostEqual(by_raw["Australian National University"]["score_percent"], 66.7)
        self.assertAlmostEqual(by_raw["Universidad Nacional del Sur"]["score_percent"], 80.0)
        self.assertAlmostEqual(by_raw["Swinburne University of Technology"]["score_percent"], 100.0)
        self.assertAlmostEqual(by_raw["University of Melbourne"]["score_percent"], 24.1)

    def test_extract_all_rows_including_unmeasured(self) -> None:
        """Verify inclusion of unmeasured (-) rows when requested."""
        all_records = extract_exact_percentages(
            pdf_path=DEFAULT_SOURCE_PDF,
            canonical_db_path=DEFAULT_CANONICAL_DB,
            include_unmeasured=True,
        )
        self.assertEqual(len(all_records), 126)
        unmeasured = [r for r in all_records if r["status"] == "no_cooperation"]
        self.assertEqual(len(unmeasured), 16)
        for u in unmeasured:
            self.assertIsNone(u["score_percent"])
            self.assertEqual(u["score_basis"], "unmeasured")

    def test_cross_validation(self) -> None:
        """Verify cross-validation against the English PDF visual tiers."""
        records = extract_exact_percentages(DEFAULT_SOURCE_PDF, DEFAULT_CANONICAL_DB)
        summary = cross_validate(records, DEFAULT_VALIDATION_PDF)

        self.assertEqual(summary.total_exact_records, 110)
        self.assertEqual(summary.matched_in_english, 110)
        self.assertEqual(summary.not_in_english_count, 0)
        self.assertGreaterEqual(summary.concordance_rate_pct, 85.0)
        self.assertEqual(summary.concordant_count, 97)
        self.assertEqual(summary.discordant_count, 13)

        # Check that specific known border/discrepancy cases are recorded
        discordant_map = {d["university_de"]: d for d in summary.discordant_records}
        self.assertIn("Instituto Tecnológico de Buenos Aires", discordant_map)
        self.assertEqual(discordant_map["Instituto Tecnológico de Buenos Aires"]["expected_tier"], "Yellow")
        self.assertEqual(discordant_map["Instituto Tecnológico de Buenos Aires"]["actual_tier"], "Red")

        self.assertIn("Tohoku University", discordant_map)
        self.assertEqual(discordant_map["Tohoku University"]["expected_tier"], "Yellow")
        self.assertEqual(discordant_map["Tohoku University"]["actual_tier"], "Light Green")

    def test_write_outputs(self) -> None:
        """Verify serialization to records.json and rows.csv."""
        records = extract_exact_percentages(DEFAULT_SOURCE_PDF, DEFAULT_CANONICAL_DB)
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path, csv_path = write_exact_2016_outputs(records, tmp_dir)
            self.assertTrue(json_path.is_file())
            self.assertTrue(csv_path.is_file())

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "0.2")
            self.assertEqual(payload["record_count"], 110)
            self.assertEqual(len(payload["records"]), 110)

            csv_text = csv_path.read_text(encoding="utf-8")
            lines = csv_text.strip().splitlines()
            self.assertEqual(len(lines), 111)  # header + 110 rows


if __name__ == "__main__":
    unittest.main()
