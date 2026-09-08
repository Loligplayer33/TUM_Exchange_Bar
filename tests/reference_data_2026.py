"""Independent expected facts for the differently scaled 2026 release."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PDF_2026 = PROJECT_ROOT / "data" / "source" / "TUMexchange_Chances_2026.pdf"

YEARS_2026 = (2023, 2024, 2025)
EXPECTED_PAGE_ROW_COUNTS_2026 = {1: 30, 2: 50, 3: 46}
EXPECTED_UNIVERSITY_COUNT_2026 = 126
EXPECTED_STATUS_COUNTS_2026 = {
    "no applications": 20,
    "no places": 8,
    "no cooperation": 14,
}
EXPECTED_NUMERIC_BAR_COUNT_2026 = 336

# UFRJ 2025 is the explicit full-width reference in this release. Its 2024
# observation is partial in both releases and therefore checks normalization
# independently of each document's absolute bar scale.
UFRJ_2025_FULL_WIDTH_PT_2026 = 60.11997975
UFRJ_2024_WIDTH_PT_2026 = 40.0800015
UFRJ_2024_SCORE_2026 = 66.666691617

