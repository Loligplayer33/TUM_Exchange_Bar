"""Unit tests for the tabular traffic-light PDF extractor."""

import json
import unittest
from pathlib import Path

from tumexchange.tabular_extract import (
    CONFIG_2018_EN,
    CONFIG_2022_DE,
    TIER_SPECS,
    classify_traffic_light_color,
    detect_document_config,
    load_canonical_map,
    resolve_university,
)


class TestTabularExtract(unittest.TestCase):
    """Test classification of 4-tier traffic light curves and tabular extraction."""

    def test_classify_traffic_light_color(self) -> None:
        """Verify RGB tuple mapping to 4 discrete quartile tiers."""
        # Tier 1 (Red): (1.0, 0.0, 0.0) -> <= 25%
        self.assertEqual(classify_traffic_light_color((1.0, 0.0, 0.0)), 1)
        # Tier 2 (Yellow): (1.0, 1.0, 0.0) -> 26-50%
        self.assertEqual(classify_traffic_light_color((1.0, 1.0, 0.0)), 2)
        # Tier 3 (Light Green): (0.573, 0.816, 0.314) -> 51-75%
        self.assertEqual(classify_traffic_light_color((0.573, 0.816, 0.314)), 3)
        # Tier 4 (Dark Green): (0.0, 0.69, 0.314) -> >= 76%
        self.assertEqual(classify_traffic_light_color((0.0, 0.69, 0.314)), 4)
        # Non-matching colors
        self.assertIsNone(classify_traffic_light_color(None))
        self.assertIsNone(classify_traffic_light_color((0.0, 0.396, 0.741)))

    def test_tier_specs(self) -> None:
        """Verify representative scores and score ranges for all 4 tiers."""
        self.assertEqual(TIER_SPECS[1]["score_percent"], 12.5)
        self.assertEqual(TIER_SPECS[1]["score_range"], "≤ 25%")
        self.assertEqual(TIER_SPECS[2]["score_percent"], 38.0)
        self.assertEqual(TIER_SPECS[2]["score_range"], "26-50%")
        self.assertEqual(TIER_SPECS[3]["score_percent"], 63.0)
        self.assertEqual(TIER_SPECS[3]["score_range"], "51-75%")
        self.assertEqual(TIER_SPECS[4]["score_percent"], 88.0)
        self.assertEqual(TIER_SPECS[4]["score_range"], "≥ 76%")

    def test_detect_document_config(self) -> None:
        """Verify automatic detection of German vs English document configurations."""
        cfg_de = detect_document_config("Auswahlquote_TUMexchange_2021_22_GESAMT_deut.pdf")
        self.assertEqual(cfg_de.source_release_year, 2022)
        self.assertEqual(cfg_de.language, "de")
        self.assertEqual(len(cfg_de.year_cols), 4)

        cfg_en = detect_document_config("Auswahlquote_TUMexchange_nach_Unis_eng.pdf")
        self.assertEqual(cfg_en.source_release_year, 2018)
        self.assertEqual(cfg_en.language, "en")
        self.assertEqual(len(cfg_en.year_cols), 2)

    def test_extracted_dataset_integrity(self) -> None:
        """Verify the generated tabular dataset contains all tiers and clean records."""
        rec_path = Path("data/output/tabular/records.json")
        if not rec_path.is_file():
            self.skipTest("tabular records.json not generated yet")

        with open(rec_path, encoding="utf-8") as f:
            data = json.load(f)

        records = data.get("records", [])
        self.assertGreaterEqual(len(records), 800)
        tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for r in records:
            if r.get("tier"):
                tier_counts[r["tier"]] += 1

        for tier in (1, 2, 3, 4):
            self.assertGreater(tier_counts[tier], 100, f"Tier {tier} should have >100 entries")


if __name__ == "__main__":
    unittest.main()
