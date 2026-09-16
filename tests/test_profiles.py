"""Validation and identity-join contracts for the MoveON profile catalog."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tumexchange.profiles import (
    ProfileValidationError,
    attach_profiles,
    load_profiles,
    profile_coverage,
)


def _profile(
    relation_id: str,
    name: str,
    *,
    institution_id: str = "1",
    country: str = "Australia",
    city: str = "Sydney",
    levels: list[str] | None = None,
    subjects: list[str] | None = None,
    field_text: str = "Exchange details",
) -> dict[str, object]:
    return {
        "institution_id": institution_id,
        "relation_id": relation_id,
        "name": name,
        "country": country,
        "city": city,
        "levels": levels if levels is not None else ["bachelor", "master"],
        "subject_areas": subjects if subjects is not None else ["CIT-Informatics"],
        "fields": [
            {
                "label": "Specific Programs",
                "text": field_text,
                "links": [{"label": "Details", "url": "https://example.test/details"}],
            }
        ],
        "source_url": "https://example.test/report-page-6940/",
        "details_url": f"https://example.test/report-page-6940/?relation_id={relation_id}",
        "retrieved_at": "2026-09-16T00:00:00Z",
    }


def _catalog(*profiles: dict[str, object], matches: dict[str, list[str]] | None = None) -> dict[str, object]:
    return {
        "schema_version": "moveon-catalog-v1",
        "retrieved_at": "2026-09-16T00:00:00Z",
        "source_url": "https://example.test/report-page-6940/",
        "expected_relation_count": len(profiles),
        "retrieved_relation_count": len(profiles),
        "profiles": list(profiles),
        "matches": matches or {},
    }


class ProfileCatalogTests(unittest.TestCase):
    def test_load_validates_provenance_links_and_normalizes_ids(self) -> None:
        payload = _catalog(_profile("1", "Example University"))
        payload["profiles"][0]["institution_id"] = 7
        payload["profiles"][0]["relation_id"] = 11
        with tempfile.TemporaryDirectory(prefix="tumexchange-profile-") as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_profiles(path)
        self.assertEqual(loaded["profiles"][0]["institution_id"], "7")
        self.assertEqual(loaded["profiles"][0]["relation_id"], "11")

    def test_invalid_url_or_unknown_reviewed_relation_fails_closed(self) -> None:
        invalid_url = _catalog(_profile("1", "Example University"))
        invalid_url["profiles"][0]["details_url"] = "javascript:alert(1)"
        with self.assertRaises(ProfileValidationError):
            attach_profiles([], invalid_url)

        invalid_match = _catalog(
            _profile("1", "Example University"),
            matches={"Historical University": ["missing"]},
        )
        with self.assertRaises(ProfileValidationError):
            attach_profiles([], invalid_match)

        future = _catalog(_profile("1", "Example University"))
        future["schema_version"] = "moveon-catalog-v2"
        with self.assertRaises(ProfileValidationError):
            attach_profiles([], future)

        absent_version = _catalog(_profile("1", "Example University"))
        absent_version.pop("schema_version")
        self.assertEqual(len(attach_profiles([], absent_version)), 1)

    def test_exact_name_and_country_alias_match_and_extend_search_text(self) -> None:
        catalog = _catalog(
            _profile(
                "bath",
                "University of Bath",
                country="United Kingdom",
                city="Bath",
                levels=["bachelor"],
                subjects=["CIT-Informatics"],
                field_text="English-taught exchange courses",
            ),
            _profile(
                "qut",
                "Queensland University of Technology (QUT)",
                country="Australia",
                city="Brisbane",
                field_text="Course catalogue",
            ),
        )
        groups = [
            {"university": "University of Bath (planned transfer to the Erasmus+ programme)", "country": "UK", "search_text": "bath"},
            {"university": "Queensland University of Technology", "country": "Australia", "search_text": "brisbane"},
            {"university": "University of California, Davis", "country": "USA", "search_text": "davis"},
        ]
        enriched = attach_profiles(groups, catalog)
        bath, qut, davis = enriched[:3]
        self.assertEqual([profile["relation_id"] for profile in bath["profiles"]], ["bath"])
        self.assertEqual(bath["profile_levels"], ["bachelor"])
        self.assertEqual(bath["profile_cities"], ["Bath"])
        self.assertIn("english-taught exchange courses", bath["search_text"])
        self.assertEqual([profile["relation_id"] for profile in qut["profiles"]], ["qut"])
        self.assertEqual(davis["profiles"], [])

    def test_explicit_relation_mapping_preserves_school_and_degree_boundaries(self) -> None:
        catalog = _catalog(
            _profile(
                "science",
                "The University of Tokyo",
                institution_id="262",
                country="Japan",
                levels=["master"],
                subjects=["CIT-Informatics"],
                field_text="Graduate School of Information Science and Technology",
            ),
            _profile(
                "engineering",
                "The University of Tokyo",
                institution_id="262",
                country="Japan",
                levels=["bachelor", "master"],
                subjects=["ED-Mechanical Engineering"],
                field_text="School of Engineering",
            ),
            matches={
                "The University of Tokyo (Graduate School of Information, Science and Technology)": ["science"],
                "The University of Tokyo (Graduate School of Engineering)": ["engineering"],
            },
        )
        groups = [
            {"university": "The University of Tokyo (Graduate School of Information, Science and Technology)", "country": "Japan", "search_text": ""},
            {"university": "The University of Tokyo (Graduate School of Engineering)", "country": "Japan", "search_text": ""},
        ]
        enriched = attach_profiles(groups, catalog)
        self.assertEqual([p["relation_id"] for p in enriched[0]["profiles"]], ["science"])
        self.assertEqual(enriched[0]["profile_levels"], ["master"])
        self.assertEqual([p["relation_id"] for p in enriched[1]["profiles"]], ["engineering"])
        self.assertEqual(enriched[1]["profile_levels"], ["bachelor", "master"])
        self.assertNotIn("ED-Mechanical Engineering", enriched[0]["profile_subjects"])
        self.assertNotIn("CIT-Informatics", enriched[1]["profile_subjects"])

    def test_source_only_relations_are_retained_and_same_generic_identity_is_grouped(self) -> None:
        catalog = _catalog(
            _profile("new-1", "Deakin University", country="Australia", city="Geelong"),
            _profile("new-2", "Deakin University (DEAKIN)", country="Australia", city="Geelong"),
            _profile("new-3", "Swinburne University of Technology (SUT)", country="Australia", city="Melbourne"),
        )
        enriched = attach_profiles(
            [{"university": "Known University", "country": "Australia", "search_text": "known"}],
            catalog,
        )
        source_only = [group for group in enriched if group.get("source_only")]
        self.assertEqual(len(source_only), 2)
        deakin = next(group for group in source_only if group["university"] == "Deakin University")
        self.assertEqual({p["relation_id"] for p in deakin["profiles"]}, {"new-1", "new-2"})
        self.assertEqual(deakin["records"], [])
        self.assertEqual(deakin["mean_score"], None)
        self.assertIn("geelong", deakin["search_text"])

    def test_title_case_parenthetical_is_a_distinct_qualifier(self) -> None:
        catalog = _catalog(
            _profile("law", "Example University (Law)", country="Australia"),
            _profile("uc", "Example University (UC Davis)", country="Australia"),
        )
        enriched = attach_profiles(
            [{"university": "Example University", "country": "Australia", "search_text": ""}],
            catalog,
        )
        historical = next(group for group in enriched if not group.get("source_only"))
        self.assertEqual([profile["relation_id"] for profile in historical["profiles"]], ["uc"])
        source_only = [group for group in enriched if group.get("source_only")]
        self.assertEqual({group["university"] for group in source_only}, {"Example University (Law)"})

    def test_coverage_reports_unmatched_history_and_source_only_relations(self) -> None:
        catalog = _catalog(
            _profile("matched", "Known University"),
            _profile("source-only", "New University"),
        )
        coverage = profile_coverage(
            [{"university": "Known University", "country": "Australia"}, {"university": "Old University", "country": "Australia"}],
            catalog,
        )
        self.assertEqual(coverage["historical_groups"], 2)
        self.assertEqual(coverage["matched_groups"], 1)
        self.assertEqual(coverage["unmatched_groups"], ["Old University"])
        self.assertEqual(coverage["matched_relations"], 1)
        self.assertEqual(coverage["source_only_relations"], ["source-only"])


if __name__ == "__main__":
    unittest.main()
