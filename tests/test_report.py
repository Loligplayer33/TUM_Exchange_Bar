"""Report grouping and rendering checks against the extracted source data."""

from __future__ import annotations

import copy
import json
import tempfile
import unicodedata
import unittest
from functools import cmp_to_key
from pathlib import Path

import tumexchange.report as report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECORDS_JSON = PROJECT_ROOT / "data" / "output" / "TUMexchange_Chances_2027" / "records.json"
CALIBRATION_JSON = PROJECT_ROOT / "data" / "output" / "TUMexchange_Chances_2027" / "calibration.json"


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(RECORDS_JSON.read_text(encoding="utf-8"))
        cls.records = payload["records"]
        cls.calibration = json.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))
        cls.normalized = report._normalized_records(cls.records)
        cls.groups = report._group_records(cls.normalized)
        cls.by_university = {group["university"]: group for group in cls.groups}

    def test_grouping_has_one_summary_per_university_and_expected_denominators(self) -> None:
        self.assertEqual(len(self.groups), 125)
        self.assertEqual(sum(group["total_count"] for group in self.groups), 375)
        self.assertEqual(sum(group["scored_count"] for group in self.groups), 344)
        self.assertEqual(sum(group["mean_score"] is not None for group in self.groups), 119)
        self.assertEqual(sum(group["mean_score"] is None for group in self.groups), 6)

        campinas = self.by_university["Universidade Estadual de Campinas (UNICAMP)"]
        self.assertEqual(campinas["scored_count"], 3)
        self.assertAlmostEqual(campinas["mean_score"], 94.430577037, places=8)

        bit = self.by_university["Burkina Institute of Technology (BIT)"]
        self.assertEqual(bit["scored_count"], 1)
        self.assertEqual(bit["mean_score"], 100.0)

        all_na = {
            group["university"]
            for group in self.groups
            if group["mean_score"] is None
        }
        self.assertEqual(
            all_na,
            {
                "Universidad San Francisco de Quito (USFQ)",
                "Universidad Tecnológica de Panamá (UTP)",
                "Tshwane University of Technology (TUT)",
                "University of Johannesburg (UJ)",
                "University of Venda",
                "University of the Witwatersrand",
            },
        )

    def test_means_include_assigned_scores_and_exclude_na_entries(self) -> None:
        buenos_aires = self.by_university["Universidad de Buenos Aires (UBA)"]
        self.assertEqual(buenos_aires["scored_count"], 2)
        self.assertAlmostEqual(
            buenos_aires["mean_score"], (100.0 + 23.441349797) / 2, places=8
        )
        self.assertEqual(
            [record["year"] for record in buenos_aires["records"]],
            [2026, 2025, 2024],
        )

        # The category list is the data contract used by the status filter: a
        # university remains visible when any annual entry has that category.
        bit = self.by_university["Burkina Institute of Technology (BIT)"]
        self.assertIn("assigned", bit["categories"])
        self.assertIn("na", bit["categories"])
        self.assertEqual(
            [record["status"] for record in bit["records"]],
            ["no_applications", "no_cooperation", "no_cooperation"],
        )

    def test_grouped_html_has_nested_annual_rows_newest_first_and_no_year_filter(self) -> None:
        html = report._build_html(self.normalized, self.calibration)
        self.assertEqual(html.count('class="data-row university-row"'), 125)
        self.assertEqual(html.count('class="annual-data-row"'), 375)
        self.assertIn('class="annual-table"', html)
        self.assertIn("Annual statuses", html)
        self.assertNotIn('id="year-filter"', html)
        self.assertNotIn("yearFilter", html)
        self.assertNotIn("__UNIVERSITIES__", html)
        self.assertNotIn("__ANNUAL__", html)

        group = self.by_university["Instituto Tecnológico de Buenos Aires (ITBA)"]
        annual_html = report._render_university_row(group, 0)
        self.assertLess(annual_html.index(">2026<"), annual_html.index(">2025<"))
        self.assertLess(annual_html.index(">2025<"), annual_html.index(">2024<"))

    def test_status_categories_preserve_all_annual_statuses_for_filtering(self) -> None:
        html = report._build_html(self.normalized, self.calibration)
        self.assertIn('id="status-filter"', html)
        self.assertIn("Assigned 100%", html)
        self.assertIn('"status":"no_applications"', html)
        self.assertIn('"status":"no_cooperation"', html)
        self.assertIn('"status":"no_places"', html)
        # The grouped payload must keep annual records, so filtering by one
        # annual category cannot replace a university's mean or hide its other
        # years from the nested detail panel.
        self.assertIn('"records":[', html)
        bit = self.by_university["Burkina Institute of Technology (BIT)"]
        assigned_matches = [
            group
            for group in self.groups
            if "assigned" in group["categories"]
        ]
        self.assertIn(bit, assigned_matches)
        self.assertEqual(len(bit["records"]), 3)

    def test_combined_sources_keep_latest_observation_per_university_year(self) -> None:
        records = [
            {
                "country": "Example",
                "university": "Overlap University",
                "year": 2025,
                "status": "measured",
                "score_percent": 20,
                "source_release_year": 2026,
                "source_document_id": "old-document",
                "source_filename": "Chances_2026.pdf",
                "calibration_id": "old-calibration",
            },
            {
                "country": "Example",
                "university": "Overlap University",
                "year": 2025,
                "status": "measured",
                "score_percent": 80,
                "source_release_year": 2027,
                "source_document_id": "new-document",
                "source_filename": "Chances_2027.pdf",
                "calibration_id": "new-calibration",
            },
            {
                "country": "Example",
                "university": "Overlap University",
                "year": 2024,
                "status": "no_applications",
                "source_release_year": 2027,
                "source_document_id": "new-document",
                "source_filename": "Chances_2027.pdf",
                "calibration_id": "new-calibration",
            },
        ]
        groups = report._group_records(records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["total_count"], 2)
        self.assertEqual(groups[0]["scored_count"], 2)
        self.assertAlmostEqual(groups[0]["mean_score"], 90.0)
        self.assertEqual(
            [(row["year"], row["score_percent"]) for row in groups[0]["records"]],
            [(2025, 80.0), (2024, 100.0)],
        )

    def test_unavailable_status_cannot_contribute_stale_numeric_value(self) -> None:
        normalized = report._normalized_records(
            [
                {
                    "country": "Example",
                    "university": "Unavailable University",
                    "year": 2025,
                    "status": "no_places",
                    "score_percent": 99,
                },
                {
                    "country": "Example",
                    "university": "Unavailable University",
                    "year": 2024,
                    "status": "measured",
                    "score_percent": 40,
                },
            ]
        )
        self.assertIsNone(normalized[0]["score_percent"])
        group = report._group_records(normalized)[0]
        self.assertEqual(group["scored_count"], 1)
        self.assertEqual(group["mean_score"], 40.0)

    def test_roster_renames_merge_and_historical_only_entry_gets_note(self) -> None:
        records = [
            {
                "country": "Example",
                "university": "Example University",
                "year": 2023,
                "status": "measured",
                "score_percent": 40,
                "source_release_year": 2026,
                "is_latest_roster": True,
            },
            {
                "country": "Example",
                "university": "Example University (EXU)",
                "year": 2024,
                "status": "measured",
                "score_percent": 60,
                "source_release_year": 2027,
                "is_latest_roster": True,
            },
            {
                "country": "Example",
                "university": "Removed University",
                "year": 2023,
                "status": "measured",
                "score_percent": 80,
                "source_release_year": 2026,
                "is_latest_roster": False,
            },
        ]
        groups = report._group_records(records)
        self.assertEqual(len(groups), 2)
        current = next(group for group in groups if "Example University" in group["university"])
        self.assertEqual(current["university"], "Example University (EXU)")
        self.assertEqual(current["total_count"], 2)
        removed = next(group for group in groups if group["university"] == "Removed University")
        self.assertFalse(removed["is_latest_roster"])
        self.assertIn("Not listed in release 2027", report._render_university_row(removed, 0))

    def test_multi_calibration_html_labels_each_source_and_explains_scope(self) -> None:
        records = [
            {
                "country": "Example",
                "university": "Old University",
                "year": 2024,
                "status": "measured",
                "score_percent": 50,
                "raw_width_pt": 20,
                "full_width_pt": 40,
                "source_release_year": 2026,
                "source_document_id": "old-document",
                "source_filename": "Chances_2026.pdf",
                "calibration_id": "old-calibration",
            },
            {
                "country": "Example",
                "university": "Old University",
                "year": 2025,
                "status": "measured",
                "score_percent": 50,
                "raw_width_pt": 30,
                "full_width_pt": 60,
                "source_release_year": 2027,
                "source_document_id": "new-document",
                "source_filename": "Chances_2027.pdf",
                "calibration_id": "new-calibration",
            },
        ]
        calibrations = [
            {
                "calibration_id": "old-calibration",
                "source_document_id": "old-document",
                "source_filename": "Chances_2026.pdf",
                "source_release_year": 2026,
                "full_width_pt": 40,
            },
            {
                "calibration_id": "new-calibration",
                "source_document_id": "new-document",
                "source_filename": "Chances_2027.pdf",
                "source_release_year": 2027,
                "full_width_pt": 60,
            },
        ]
        rendered = report._build_html(records, calibrations)
        self.assertIn("2 calibrations", rendered)
        self.assertIn("Release 2026", rendered)
        self.assertIn("Release 2027", rendered)
        self.assertIn("Chances_2026.pdf", rendered)
        self.assertIn("Chances_2027.pdf", rendered)
        self.assertIn('href="calibration.json"', rendered)
        self.assertNotIn("Show calibration and source-release metadata", rendered)
        self.assertNotIn("class=\"calibration-panel\"", rendered)
        self.assertIn("not an individual admission probability", rendered)
        self.assertIn("not a grade-based prediction yet", rendered)
        self.assertIn("aria-describedby=\"assigned-help\"", rendered)

    def test_safe_escaping_and_report_does_not_mutate_records_or_exports(self) -> None:
        dangerous = {
            "country": "<script>alert(1)</script>",
            "university": "</script><script>alert('x')</script>",
            "year": 2026,
            "page": 1,
            "row_index": 1,
            "status": "measured",
            "score_percent": 50.0,
            "raw_width_pt": 24.0,
            "full_width_pt": 48.0,
            "calibration_id": "qa",
            "bar_bbox": [1, 2, 3, 4],
            "notes": "<b>unsafe</b>",
        }
        rendered = report._build_html(report._normalized_records([dangerous]), self.calibration)
        self.assertNotIn("</script><script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        escaped_json = report._safe_json({"value": "</script>&"})
        self.assertNotIn("</script>", escaped_json)
        self.assertIn(r"\u003c/script\u003e\u0026", escaped_json)

        before = copy.deepcopy(self.records)
        with tempfile.TemporaryDirectory(prefix="tumexchange-report-test-") as tmp:
            output = Path(tmp) / "report.html"
            report.write_report(self.records, self.calibration, output)
            self.assertTrue(output.exists())
            self.assertEqual([path.name for path in Path(tmp).iterdir()], ["report.html"])
        self.assertEqual(self.records, before)

    def test_top_level_sort_options_and_annual_rows_stay_newest_first(self) -> None:
        html = report._build_html(self.normalized, self.calibration)
        self.assertNotIn('id="score-sort"', html)
        self.assertNotIn(">Sort universities</label>", html)
        self.assertIn('data-sort-column="university"', html)
        self.assertIn('data-sort-column="country"', html)
        self.assertIn('data-sort-column="scored"', html)
        self.assertIn('data-sort-column="score"', html)
        self.assertIn("const compareGroups", html)
        self.assertIn("const cycleSort", html)
        self.assertIn('state.sort = state.sort === first ? second : first', html)
        self.assertEqual(
            _cycle_sort_states("asc", "university-asc", "university-desc"),
            ["university-asc", "university-desc", "university-asc", "university-desc"],
        )
        self.assertEqual(
            _cycle_sort_states("desc", "scored-asc", "scored-desc"),
            ["scored-desc", "scored-asc", "scored-desc", "scored-asc"],
        )
        self.assertEqual(
            _cycle_sort_states("desc", "score-asc", "score-desc"),
            ["score-desc", "score-asc", "score-desc", "score-asc"],
        )
        self.assertIn('sortKey === "university-asc"', html)
        self.assertIn('sortKey === "country-asc"', html)
        self.assertIn('sortKey === "scored-desc"', html)
        self.assertIn("fragment.append(summary, detail)", html)
        self.assertIn('state.sort = "source"', html)
        self.assertIn('closest("tr.university-row")', html)
        self.assertIn('closest("tr.annual-data-row")', html)
        self.assertIn("tableFrame.addEventListener", html)

        groups = [
            {
                "index": 0,
                "university": "Zulu University",
                "country": "Brazil",
                "scored_count": 1,
                "mean_score": 40.0,
            },
            {
                "index": 1,
                "university": "Alpha University",
                "country": "Argentina",
                "scored_count": 3,
                "mean_score": None,
            },
            {
                "index": 2,
                "university": "Mid University",
                "country": "Brazil",
                "scored_count": 2,
                "mean_score": 80.0,
            },
        ]
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "source")],
            ["Zulu University", "Alpha University", "Mid University"],
        )
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "university-asc")],
            ["Alpha University", "Mid University", "Zulu University"],
        )
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "university-desc")],
            ["Zulu University", "Mid University", "Alpha University"],
        )
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "country-asc")],
            ["Alpha University", "Mid University", "Zulu University"],
        )
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "scored-desc")],
            ["Alpha University", "Mid University", "Zulu University"],
        )
        self.assertEqual(
            [group["university"] for group in _sorted_groups(groups, "score-desc")],
            ["Mid University", "Zulu University", "Alpha University"],
        )

        annual_years = [
            record["year"]
            for group in self.groups
            for record in group["records"]
        ]
        for group in self.groups:
            years = [record["year"] for record in group["records"]]
            self.assertEqual(years, sorted(years, reverse=True))
        self.assertTrue(annual_years)

    def test_search_folds_accents_and_matches_every_token(self) -> None:
        html = report._build_html(self.normalized, self.calibration)
        self.assertIn("const foldSearch", html)
        self.assertIn("tokens.every", html)
        self.assertIn('placeholder="UFRJ, São Paulo, Brazil, 2024"', html)
        concepcion = next(
            group
            for group in self.groups
            if "Concepción" in group["university"] or "Concepcion" in group["university"]
        )
        self.assertTrue(_search_matches(concepcion["search_text"], "concepcion chile"))
        self.assertTrue(_search_matches(concepcion["search_text"], "UDEC"))
        ufrj = self.by_university["Universidade Federal do Rio de Janeiro (UFRJ)"]
        self.assertTrue(_search_matches(ufrj["search_text"], "ufrj brazil"))
        self.assertFalse(_search_matches(ufrj["search_text"], "ufrj tokyo"))

    def test_mobile_layout_stacks_rows_and_keeps_header_sorts(self) -> None:
        html = report._build_html(self.normalized, self.calibration)
        self.assertIn('viewport-fit=cover', html)
        self.assertIn('overflow-x: clip', html)
        self.assertIn("@media (max-width: 960px)", html)
        self.assertIn("tr.university-row > td::before", html)
        self.assertIn("class=\"mobile-sort\"", html)
        self.assertIn('aria-label="Sort this list"', html)
        self.assertIn('data-label="Country"', html)
        self.assertIn('data-label="Years scored"', html)
        self.assertIn('data-label="Mean score"', html)
        self.assertIn('data-label="Source"', html)
        self.assertIn("const mobileSortButtons", html)
        self.assertIn('querySelector(".mobile-sort")', html)
        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn("font-size: 16px", html)
        self.assertNotIn(">Sort universities</label>", html)


def _fold_search(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(character for character in text if not unicodedata.combining(character))
    return text.casefold()


def _search_matches(search_text: str, query: str) -> bool:
    haystack = _fold_search(search_text)
    tokens = [token for token in _fold_search(query).split() if token]
    return all(token in haystack for token in tokens)


def _cycle_sort_states(first: str, asc: str, desc: str) -> list[str]:
    """Mirror header clicks: toggle between the two directions."""

    first_key = desc if first == "desc" else asc
    second_key = asc if first == "desc" else desc
    current = "source"
    states: list[str] = []
    for _ in range(4):
        current = second_key if current == first_key else first_key
        states.append(current)
    return states


def _sorted_groups(groups: list[dict], sort_key: str) -> list[dict]:
    """Mirror the report's JS comparator for the documented top-level sorts."""

    def text(value: object) -> str:
        return "" if value is None else str(value)

    def numeric(value: object) -> float | None:
        if value is None or isinstance(value, bool) or value == "":
            return None
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def compare_text(left: object, right: object) -> int:
        left_key, right_key = text(left).casefold(), text(right).casefold()
        return (left_key > right_key) - (left_key < right_key)

    def compare_nullable(left: float | None, right: float | None, descending: bool) -> int:
        if left is None and right is not None:
            return 1
        if left is not None and right is None:
            return -1
        if left is not None and right is not None and left != right:
            sign = (left > right) - (left < right)
            return -sign if descending else sign
        return 0

    def compare(left: dict, right: dict) -> int:
        source_order = int(left["index"]) - int(right["index"])
        if sort_key == "source":
            return source_order
        if sort_key in {"university-asc", "university-desc"}:
            direction = -1 if sort_key == "university-desc" else 1
            return direction * compare_text(left["university"], right["university"]) or source_order
        if sort_key in {"country-asc", "country-desc"}:
            direction = -1 if sort_key == "country-desc" else 1
            return (
                direction * compare_text(left["country"], right["country"])
                or compare_text(left["university"], right["university"])
                or source_order
            )
        if sort_key in {"scored-desc", "scored-asc"}:
            return (
                compare_nullable(
                    numeric(left["scored_count"]),
                    numeric(right["scored_count"]),
                    sort_key == "scored-desc",
                )
                or compare_text(left["university"], right["university"])
                or source_order
            )
        if sort_key in {"score-desc", "score-asc"}:
            return (
                compare_nullable(
                    numeric(left["mean_score"]),
                    numeric(right["mean_score"]),
                    sort_key == "score-desc",
                )
                or compare_text(left["university"], right["university"])
                or source_order
            )
        return source_order

    return sorted(groups, key=cmp_to_key(compare))


if __name__ == "__main__":
    unittest.main()
