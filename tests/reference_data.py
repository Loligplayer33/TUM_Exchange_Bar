"""Independent expected values read from the supplied 2027 source PDF.

This module is deliberately kept separate from the extractor.  The expected
values come from an independent inspection of the PDF text and vector
geometry, so integration tests can catch a parser that reproduces its own
assumptions.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PDF = PROJECT_ROOT / "data" / "source" / "TUMexchange_Chances_2027.pdf"

YEARS = (2024, 2025, 2026)

# The bottom border of each table row is drawn as a cyan vector rectangle.
# Distinct border y positions therefore give a source-independent row count.
EXPECTED_PAGE_ROW_COUNTS = {1: 32, 2: 45, 3: 38, 4: 10}
EXPECTED_UNIVERSITY_COUNT = sum(EXPECTED_PAGE_ROW_COUNTS.values())
EXPECTED_SLOT_COUNT = EXPECTED_UNIVERSITY_COUNT * len(YEARS)

# The explicit user model assigns "no applications" a 100% score while
# retaining the source status and leaving raw_width_pt empty.  Other textual
# statuses remain N/A.
EXPECTED_STATUS_COUNTS = {
    "no applications": 17,
    "no places": 5,
    "no cooperation": 26,
}
EXPECTED_STATUS_SLOT_COUNT = sum(EXPECTED_STATUS_COUNTS.values())
EXPECTED_NUMERIC_BAR_COUNT = EXPECTED_SLOT_COUNT - EXPECTED_STATUS_SLOT_COUNT
EXPECTED_ASSIGNED_NO_APPLICATIONS_COUNT = EXPECTED_STATUS_COUNTS["no applications"]
EXPECTED_NA_STATUS_COUNT = EXPECTED_STATUS_COUNTS["no places"] + EXPECTED_STATUS_COUNTS[
    "no cooperation"
]
EXPECTED_SCORED_SLOT_COUNT = EXPECTED_NUMERIC_BAR_COUNT + EXPECTED_ASSIGNED_NO_APPLICATIONS_COUNT

# The three Brazil 2025 bars are the calibration reference.  pdfplumber
# reports these vector widths in PDF points; the value is kept with enough
# precision to make later changed-size synthetic fixtures reproducible.
BRAZIL_2025_FULL_WIDTH_PT = 48.1200255

# Exact text labels and status years independently observed in the PDF.  A
# tuple is (university label, year, source status text).  The parser may add a
# stable ID, but these source labels must remain recoverable in provenance.
EXPECTED_STATUS_ROWS = (
    # Page 1
    ("Universidad de Buenos Aires (UBA)", 2024, "no cooperation"),
    ("Burkina Institute of Technology (BIT)", 2024, "no cooperation"),
    ("Burkina Institute of Technology (BIT)", 2025, "no cooperation"),
    ("Burkina Institute of Technology (BIT)", 2026, "no applications"),
    ("Concordia University", 2024, "no places"),
    # Page 2
    ("Huazhong University of Science and Technology", 2024, "no applications"),
    ("Huazhong University of Science and Technology", 2025, "no applications"),
    ("Universidad Nacional de Colombia (UNAL)", 2026, "no places"),
    ("Universidad San Francisco de Quito (USFQ)", 2024, "no cooperation"),
    ("Universidad San Francisco de Quito (USFQ)", 2025, "no cooperation"),
    ("Universidad San Francisco de Quito (USFQ)", 2026, "no cooperation"),
    ("Indian Institute of Technology Guwahati (IIT Guwahati)", 2024, "no applications"),
    ("Indian Institute of Technology Guwahati (IIT Guwahati)", 2026, "no applications"),
    ("Indian Institute of Technology Kanpur (IIT Kanpur)", 2024, "no applications"),
    ("Indian Institute of Technology Kharagpur (IIT Kharagpur)", 2025, "no applications"),
    ("Indian Institute of Technology Kharagpur (IIT Kharagpur)", 2026, "no applications"),
    ("Indian Institute of Technology Mandi (IIT Mandi)", 2024, "no applications"),
    ("Indian Institute of Technology Mandi (IIT Mandi)", 2025, "no applications"),
    ("Indian Institute of Technology Mandi (IIT Mandi)", 2026, "no applications"),
    ("Indian Institute of Technology Roorkee (IIT Roorkee)", 2025, "no applications"),
    ("Indian Institute of Technology Roorkee (IIT Roorkee)", 2026, "no applications"),
    ("Universitas Indonesia", 2025, "no applications"),
    ("Hebrew University of Jerusalem", 2025, "no applications"),
    ("Tel Aviv University", 2025, "no applications"),
    # Page 3
    ("Victoria University of Wellington", 2024, "no cooperation"),
    ("Victoria University of Wellington", 2025, "no cooperation"),
    ("Universidad Tecnológica de Panamá (UTP)", 2024, "no cooperation"),
    ("Universidad Tecnológica de Panamá (UTP)", 2025, "no cooperation"),
    ("Universidad Tecnológica de Panamá (UTP)", 2026, "no cooperation"),
    ("Singapore University of Technology and Design (SUTD)", 2024, "no cooperation"),
    ("Singapore University of Technology and Design (SUTD)", 2025, "no cooperation"),
    ("Tshwane University of Technology (TUT)", 2024, "no cooperation"),
    ("Tshwane University of Technology (TUT)", 2025, "no cooperation"),
    ("Tshwane University of Technology (TUT)", 2026, "no cooperation"),
    ("University of Johannesburg (UJ)", 2024, "no cooperation"),
    ("University of Johannesburg (UJ)", 2025, "no cooperation"),
    ("University of Johannesburg (UJ)", 2026, "no cooperation"),
    ("University of Venda", 2024, "no cooperation"),
    ("University of Venda", 2025, "no cooperation"),
    ("University of Venda", 2026, "no cooperation"),
    ("University of the Witwatersrand", 2024, "no cooperation"),
    ("University of the Witwatersrand", 2025, "no cooperation"),
    ("University of the Witwatersrand", 2026, "no cooperation"),
    ("Makerere University", 2024, "no cooperation"),
    # Page 4
    ("University of Illinois at Urbana-Champaign (UIUC)", 2025, "no places"),
    ("University of Minnesota, Twin Cities", 2026, "no places"),
    ("Virginia Polytechnic Institute and State University (Virginia Tech)", 2024, "no places"),
    ("Vietnam National University of Hanoi - University of Science", 2025, "no applications"),
)

EXPECTED_NAMED_WIDTHS_PT = {
    # Full reference bars, all three Brazil 2025 rows.
    ("Universidade Estadual de Campinas (UNICAMP)", 2025): BRAZIL_2025_FULL_WIDTH_PT,
    ("Universidade Federal de Santa Catarina (UFSC)", 2025): BRAZIL_2025_FULL_WIDTH_PT,
    ("Universidade Federal do Rio de Janeiro (UFRJ)", 2025): BRAZIL_2025_FULL_WIDTH_PT,
    # A partial, a sub-pixel-size tiny bar, and the deliberate N/A example.
    ("Instituto Tecnológico de Buenos Aires (ITBA)", 2024): 28.32,
    ("University of Sydney", 2024): 0.84,
}
