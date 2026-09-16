"""Profile catalogue rendering and filter contracts for the static report."""

from __future__ import annotations

import json
import re
import unittest

from tumexchange import report


def _record(university: str, year: int = 2026, score: float = 50.0) -> dict[str, object]:
    return {
        "country": "Example",
        "university": university,
        "year": year,
        "page": 1,
        "row_index": 1,
        "status": "measured",
        "score_percent": score,
        "raw_width_pt": score,
        "full_width_pt": 100.0,
        "source_release_year": 2027,
        "source_filename": "Chances_2027.pdf",
        "source_document_id": "example-document",
        "calibration_id": "example-calibration",
    }


def _profile(
    relation_id: str,
    name: str,
    *,
    levels: list[str],
    subjects: list[str],
    city: str = "Example City",
    text: str = "Semester dates and exchange information.",
) -> dict[str, object]:
    labels = [
        "Campus",
        "Semester Dates",
        "Study System",
        "Admission Requirements",
        "Academic Restrictions",
        "Language Requirements",
        "Additional Information",
        "Application Deadlines at Host University",
        "Exchange Information",
        "Website",
        "Level",
        "Subject Area",
    ]
    fields = [
        {
            "label": label,
            "text": ("Bachelor || Master" if label == "Level" else "Mathematics" if label == "Subject Area" else text),
            "links": ([{"label": "Official page", "url": "https://example.test/official"}] if label == "Website" else []),
        }
        for label in labels
    ]
    return {
        "institution_id": "institution-" + relation_id,
        "relation_id": relation_id,
        "name": name,
        "country": "Example",
        "city": city,
        "levels": levels,
        "subject_areas": subjects,
        "fields": fields,
        "source_url": "https://example.test/report-page-6940/",
        "details_url": "https://example.test/report-page-6940/?relation_id=" + relation_id,
        "retrieved_at": "2026-09-16T12:00:00Z",
    }


def _catalog(*profiles: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "moveon-catalog-v1",
        "retrieved_at": "2026-09-16T12:00:00Z",
        "source_url": "https://example.test/report-page-6940/",
        "expected_relation_count": len(profiles),
        "profiles": list(profiles),
    }


class ProfileReportTests(unittest.TestCase):
    def test_profile_overview_preserves_fields_links_and_source_only_rows(self) -> None:
        profiles = _catalog(
            _profile("r1", "Example University", levels=["bachelor"], subjects=["Mathematics"]),
            _profile("r2", "Example University", levels=["master"], subjects=["Computer Science"]),
            _profile("r3", "Profile Only University", levels=[], subjects=[], city="Profile City"),
        )
        rendered = report._build_html([_record("Example University")], {}, profiles)

        self.assertIn("TUM partner details", rendered)
        self.assertIn("Application Deadlines at Host University", rendered)
        self.assertIn("Official TUM directory", rendered)
        self.assertIn('href="https://example.test/official"', rendered)
        self.assertIn('href="https://example.test/report-page-6940/?relation_id=r1"', rendered)
        self.assertIn("Profile Only University", rendered)
        self.assertIn("No historical data", rendered)
        self.assertIn("Bachelor", rendered)
        self.assertIn("Master", rendered)

        payload_match = re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>',
            rendered,
            re.S,
        )
        self.assertIsNotNone(payload_match)
        payload = json.loads(payload_match.group(1))
        self.assertEqual(len(payload["groups"]), 2)
        source_only = next(group for group in payload["groups"] if group["university"] == "Profile Only University")
        self.assertEqual(source_only["records"], [])
        self.assertEqual(source_only["total_count"], 0)

    def test_degree_and_subject_filters_use_the_same_relation(self) -> None:
        profiles = _catalog(
            _profile("r1", "Example University", levels=["bachelor"], subjects=["Mathematics"]),
            _profile("r2", "Example University", levels=["master"], subjects=["Computer Science"]),
        )
        rendered = report._build_html([_record("Example University")], {}, profiles)

        self.assertIn('id="degree-filter"', rendered)
        self.assertIn('value="unknown">Unknown', rendered)
        self.assertIn('id="subject-filter"', rendered)
        self.assertIn('value="Computer Science">Computer Science', rendered)
        self.assertIn("const profileMatches", rendered)
        self.assertIn("return degreeMatches && subjectMatches", rendered)
        self.assertIn("state.degree", rendered)
        self.assertIn("state.subject", rendered)

    def test_unsafe_profile_urls_are_omitted_and_text_is_escaped(self) -> None:
        profile = _profile(
            "unsafe",
            "<Example University>",
            levels=["bachelor"],
            subjects=["<Subject>"],
            text="<script>alert('x')</script>",
        )
        profile["source_url"] = "https://example.test/report"
        profile["details_url"] = "https://example.test/details"
        profile["fields"] = [
            {
                "label": "Website",
                "text": "<b>unsafe copy</b>",
                "links": [
                    {"label": "Bad", "url": "javascript:alert(1)"},
                    {"label": "Good", "url": "https://example.test/good"},
                ],
            }
        ]
        safe_payload = report._profile_payload(profile)
        self.assertEqual(safe_payload["fields"][0]["links"], [{"label": "Good", "url": "https://example.test/good"}])
        profile["fields"][0]["links"] = [{"label": "Good", "url": "https://example.test/good"}]
        rendered = report._build_html([_record("<Example University>")], {}, _catalog(profile))

        self.assertIn("&lt;Example University&gt;", rendered)
        self.assertIn("&lt;b&gt;unsafe copy&lt;/b&gt;", rendered)
        self.assertIn('href="https://example.test/good"', rendered)
        self.assertNotIn('href="javascript:alert(1)"', rendered)


if __name__ == "__main__":
    unittest.main()
