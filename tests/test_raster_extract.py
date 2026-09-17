"""Unit tests for the 2025 TUMexchange raster bar extractor and validator."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tumexchange.raster_extract import (
    CALIBRATION_SPEC,
    DEFAULT_CANONICAL_DB,
    DEFAULT_OUTPUT_DIR,
    cross_validate_against_2026,
    load_canonical_universities,
    match_university,
    measure_cell_bar,
    normalize_text,
)


class TestRasterExtract(unittest.TestCase):
    """Test raster chart extraction, calibration geometry, and cross-validation."""

    def test_calibration_spec(self) -> None:
        """Verify calibration anchor uses Brazil 2022 full reference width."""
        self.assertEqual(CALIBRATION_SPEC["source_release_year"], 2025)
        self.assertEqual(CALIBRATION_SPEC["units"], "px")
        self.assertEqual(CALIBRATION_SPEC["full_width_px"], 292.0)
        self.assertEqual(CALIBRATION_SPEC["year_geometry"][2022]["full_width"], 292.0)
        self.assertEqual(CALIBRATION_SPEC["year_geometry"][2023]["full_width"], 291.0)
        self.assertEqual(CALIBRATION_SPEC["year_geometry"][2024]["full_width"], 291.0)

    def test_normalize_text(self) -> None:
        """Verify accent stripping and lowercase normalization."""
        self.assertEqual(normalize_text("Université de Montréal"), "universite de montreal")
        self.assertEqual(normalize_text("São Paulo"), "sao paulo")

    def test_canonical_matching(self) -> None:
        """Verify university resolution to canonical profiles in bars.sqlite."""
        canon_map = load_canonical_universities(DEFAULT_CANONICAL_DB)
        self.assertGreater(len(canon_map), 100)

        uni, country = match_university(
            "Instituto Tecnoldgico de Buenos Aires (ITBA", "Argentina", canon_map
        )
        self.assertEqual(uni, "Instituto Tecnológico de Buenos Aires (ITBA)")
        self.assertEqual(country, "Argentina")

        uni, country = match_university(
            "Universidade Estadual de Campinas (UNICAMP.", "Brazil", canon_map
        )
        self.assertEqual(uni, "Universidade Estadual de Campinas (UNICAMP)")
        self.assertEqual(country, "Brazil")

        uni, country = match_university(
            "The Hong Kong University of Science and Technology (School of Engineering",
            "China",
            canon_map,
        )
        self.assertEqual(
            uni,
            "The Hong Kong University of Science and Technology (School of Engineering)",
        )

        uni, country = match_university(
            "The Hong Kong University of Science and Technology (School of Science",
            "China",
            canon_map,
        )
        self.assertEqual(
            uni,
            "The Hong Kong University of Science and Technology (School of Science)",
        )

    def test_measure_cell_bar_synthetic(self) -> None:
        """Verify synthetic blue bar measurement with exact fraction."""
        fw = 292.0
        # 50% bar = 146 pixels
        bar_len = 146
        img = Image.new("RGB", (300, 45), (255, 255, 255))
        # Draw TUM blue bar: (0, 113, 193)
        for x in range(bar_len):
            for y in range(5, 40):
                img.putpixel((x, y), (0, 113, 193))

        status, pct, basis = measure_cell_bar(img, fw)
        self.assertEqual(status, "measured")
        self.assertEqual(basis, "visual_bar")
        self.assertAlmostEqual(pct, 50.0, delta=0.5)

    def test_cross_validation_accuracy(self) -> None:
        """Verify mean difference against 2026 ground truth is < 2.0%."""
        rec_path = DEFAULT_OUTPUT_DIR / "records.json"
        if not rec_path.is_file():
            self.skipTest("2025 records.json not generated yet")

        with open(rec_path, encoding="utf-8") as f:
            data = json.load(f)

        records = data.get("records", [])
        self.assertEqual(len(records), 384)

        cv = cross_validate_against_2026(records)
        self.assertIn("mean_diff_pct", cv)
        self.assertLess(cv["mean_diff_pct"], 2.0)
        self.assertGreater(cv["close_matches_within_2_5pct"], 90)


if __name__ == "__main__":
    unittest.main()
