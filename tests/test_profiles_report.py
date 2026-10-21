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

    def test_sjtu_faculties_use_separate_named_campus_pins(self) -> None:
        group = {
            "university": "Shanghai Jiao Tong University",
            "country": "China",
            "profiles": [
                {
                    "relation_id": "10362",
                    "name": "Shanghai Jiao Tong University",
                    "city": "Shanghai",
                    "country": "China",
                    "fields": [{"label": "Campus", "text": "Shanghai"}],
                }
            ],
        }
        places = report._map_places(group)
        labels = [place["label"] for place in places]
        self.assertEqual(labels, ["Minhang", "Xuhui"])
        minhang, xuhui = places
        self.assertIn("Informatics", minhang["meta"])
        self.assertIn("Management", xuhui["meta"])
        self.assertAlmostEqual(minhang["lat"], 31.0275548, places=4)
        self.assertAlmostEqual(xuhui["lat"], 31.2008911, places=4)
        self.assertGreater(abs(minhang["lat"] - xuhui["lat"]), 0.1)
        rendered = report._render_map_panel(group, 3)
        self.assertIn("campus-map-stage", rendered)
        self.assertIn(">Minhang<", rendered)
        self.assertIn(">Xuhui<", rendered)
        self.assertNotIn("<iframe", rendered)
        page = report._build_html([], {}, None)
        self.assertIn("tile.openstreetmap.fr/osmfr/", page)
        self.assertIn('addEventListener("wheel"', page)
        self.assertIn("campus-pin-name", page)

    def test_further_information_uses_last_year_reports_and_both_rankings(self) -> None:
        sjtu = report._render_further_panel(
            {
                "university": "Shanghai Jiao Tong University",
                "country": "China",
                "profiles": [
                    {
                        "relation_id": "10362",
                        "name": "Shanghai Jiao Tong University",
                        "levels": ["bachelor", "master"],
                    }
                ],
            }
        )
        self.assertIn("QS World University Rankings", sjtu)
        self.assertIn("#36", sjtu)
        self.assertIn("4 of 85 in China (Mainland)", sjtu)
        self.assertIn("THE World University Rankings", sjtu)
        self.assertIn("#39", sjtu)
        self.assertIn("5 of 102 in China", sjtu)
        self.assertIn("Winter semester 2024/25", sjtu)
        self.assertIn("1 September 2024", sjtu)
        self.assertIn("at least 9 places", sjtu)
        self.assertIn("Summer semester 2025", sjtu)
        self.assertIn("1 April 2025", sjtu)
        self.assertIn("at least 1 place", sjtu)
        self.assertNotIn("Summer semester 2026", sjtu)
        self.assertNotIn("just ended", sjtu)
        self.assertIn("lower bound", sjtu)
        self.assertIn("relation_id=10362", sjtu)
        self.assertIn("Checked 4 October 2026", sjtu)
        self.assertNotIn(">2024<", sjtu)
        self.assertNotIn("2023", sjtu)

        tokyo = report._render_further_panel(
            {
                "university": "The University of Tokyo",
                "profiles": [
                    {
                        "relation_id": "10377",
                        "levels": ["bachelor", "master"],
                        "subject_areas": ["ED-Aerospace"],
                    },
                    {
                        "relation_id": "10762",
                        "levels": ["master"],
                        "subject_areas": ["CIT-Informatics"],
                    },
                ],
            }
        )
        self.assertIn("at least 7 places", tokyo)
        self.assertIn("at least 6 places", tokyo)
        self.assertIn("These totals combine the TUM partner relations", tokyo)
        self.assertEqual(tokyo.count("Open experience reports"), 2)

        itba = report._render_further_panel(
            {"university": "Instituto Tecnológico de Buenos Aires (ITBA)", "profiles": []}
        )
        self.assertIn("Reporter", itba)
        self.assertIn("not given a rank", itba)
        self.assertNotIn(">n/a<", itba)

        adelaide = report._render_further_panel(
            {"university": "University of Adelaide", "profiles": []}
        )
        self.assertIn("Listed as Adelaide University.", adelaide)

        sigs = report._render_further_panel(
            {
                "university": "Tsinghua Shenzhen International Graduate School (Tsinghua SIGS)",
                "profiles": [],
            }
        )
        self.assertIn("Not listed in this edition", sigs)
        self.assertNotIn("#14", sigs)
        self.assertNotIn("#11", sigs)

        self.assertEqual(
            report._render_further_panel({"university": "Example University", "profiles": []}),
            "",
        )
        row = report._render_university_row(
            {
                "university": "Shanghai Jiao Tong University",
                "country": "China",
                "profiles": [
                    {
                        "relation_id": "10362",
                        "name": "Shanghai Jiao Tong University",
                        "levels": ["bachelor", "master"],
                    }
                ],
            },
            0,
        )
        self.assertLess(row.index("PARTNER PROFILE"), row.index("QS World University Rankings"))
        self.assertLess(row.index("Experience reports"), row.index("HISTORICAL ADMISSION DATA"))
        self.assertEqual(row.count('aria-label="University profile"'), 1)
        self.assertNotIn("further-panel", row)
        self.assertNotIn("Rankings and recent places", row)

    def test_moveon_listing_keeps_a_university_visible_without_a_2027_chart(self) -> None:
        catalog = _catalog(
            _profile(
                "sut",
                "Swinburne University of Technology (SUT)",
                levels=["bachelor", "master"],
                subjects=["Engineering"],
                city="Melbourne",
            )
        )
        groups = report._attach_profile_catalog(
            report._group_records(
                [
                    {
                        "country": "Example",
                        "university": "Swinburne University of Technology",
                        "year": 2022,
                        "status": "measured",
                        "score_percent": 12.5,
                        "source_release_year": 2022,
                        "is_latest_roster": False,
                    },
                    {
                        "country": "Example",
                        "university": "Cardiff University",
                        "year": 2026,
                        "status": "measured",
                        "score_percent": 40,
                        "source_release_year": 2027,
                        "is_latest_roster": True,
                    },
                ]
            ),
            catalog,
        )
        swinburne = next(group for group in groups if "Swinburne" in group["university"])
        cardiff = next(group for group in groups if group["university"] == "Cardiff University")
        self.assertTrue(swinburne["offers_places"])
        self.assertFalse(swinburne["is_latest_roster"])
        self.assertFalse(cardiff["offers_places"])
        self.assertTrue(cardiff["is_latest_roster"])
        swinburne_row = report._render_university_row(swinburne, 0)
        cardiff_row = report._render_university_row(cardiff, 1)
        self.assertNotIn(" hidden>", swinburne_row.split("</tr>", 1)[0])
        self.assertNotIn("Not listed in release", swinburne_row)
        self.assertIn("Not included in the 2027 chances document. The bars stop at 2022.", swinburne_row)
        self.assertNotIn("Not on MoveON", swinburne_row)
        self.assertIn(" hidden>", cardiff_row.split("</tr>", 1)[0])
        self.assertIn("Not on MoveON", cardiff_row)
        self.assertIn("no places to apply this year", cardiff_row)
        self.assertNotIn("Not included in the 2027 chances document", cardiff_row)
        page = report._build_html([], {}, catalog)
        self.assertIn("Places this year (listed on MoveON)", page)
        self.assertIn("if (state.roster === \"current\" && !group.offers_places) return false;", page)
        self.assertNotIn("vibecoded", page)
        self.assertNotIn("double check with MoveOn", page)
        self.assertIn("MoveOn was last fetched on 16 September 2026.", page)
        without_catalog = report._build_html([], {})
        self.assertNotIn('<p class="site-note">', without_catalog)
        self.assertNotIn("last fetched", without_catalog)

    def test_campus_field_splits_unnamed_faculties_without_coordinates(self) -> None:
        group = {
            "university": "Example University",
            "country": "Chile",
            "profiles": [
                {
                    "relation_id": "not-curated",
                    "city": "Santiago",
                    "country": "Chile",
                    "fields": [
                        {
                            "label": "Campus",
                            "text": "Engineering campus (FCFM)\nDowntown campus (Business)",
                        }
                    ],
                }
            ],
        }
        places = report._map_places(group)
        self.assertEqual([place["label"] for place in places], ["Engineering campus", "Downtown campus"])
        self.assertEqual(places[0]["tag"], "FCFM")
        self.assertNotIn("lat", places[0])


if __name__ == "__main__":
    unittest.main()
