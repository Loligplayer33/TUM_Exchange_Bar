"""Focused tests for the network-independent MoveON catalog contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tumexchange.moveon import (
    MoveOnError,
    _decode_response,
    _details_url,
    _parse_field,
    _validate_payload,
    write_catalog,
)


def _source_record(relation_id: str, *, fields: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "core_id": "core-1",
        "relation_id": relation_id,
        "universityname": "Example University",
        "country_fullname": "Exampleland",
        "informatics": [{"institutions": fields if fields is not None else [{"shortname": "Level", "fullname": "Bachelor"}]}],
    }


class MoveOnParsingTests(unittest.TestCase):
    def test_field_parser_keeps_paragraphs_and_resolves_relative_and_dynamic_links(self) -> None:
        parsed = _parse_field(
            "Partner Information",
            "<p>first<br>second</p><p><a href='/facts' title='Facts'>link</a> "
            "<a href='#' data-id='doc-1'>PDF</a></p>",
            source_url="https://tum.adv-pub.moveon4.de/report-page-6940/",
            details_url="https://tum.adv-pub.moveon4.de/report-page-6940/?relation_id=1",
        )

        self.assertEqual(parsed["label"], "Partner Information")
        self.assertEqual(parsed["text"], "first\nsecond\nlink PDF")
        self.assertEqual(parsed["links"], [
            {"label": "link", "url": "https://tum.adv-pub.moveon4.de/facts"},
            {
                "label": "PDF (on TUM's directory)",
                "url": "https://tum.adv-pub.moveon4.de/report-page-6940/",
                "document_id": "doc-1",
            },
        ])

    def test_dynamic_relation_url_retains_both_stable_identifiers(self) -> None:
        url = _details_url(
            "https://tum.adv-pub.moveon4.de/report-page-6940/",
            {"core_id": 234, "relation_id": 567},
        )
        self.assertIn("core_id=234", url)
        self.assertIn("relation_id=567", url)
        self.assertIn("openwhichtab=travelreports", url)
        self.assertIn("whichtab=all", url)

    def test_source_fields_with_unexpected_labels_are_retained(self) -> None:
        fields = [
            {"shortname": "Level", "fullname": "Bachelor"},
            {"shortname": "Specific Programs", "fullname": "Architecture"},
            {"shortname": "Partner Information", "fullname": "Partner notes"},
        ]
        payload = {
            "counters": {"institutions": 1},
            "labelvalue": [_source_record("1", fields=fields)],
        }
        records = _validate_payload(payload)
        self.assertEqual(
            [field["shortname"] for field in records[0]["informatics"][0]["institutions"]],
            ["Level", "Specific Programs", "Partner Information"],
        )


class MoveOnValidationTests(unittest.TestCase):
    def test_decode_rejects_non_json_and_non_object_responses(self) -> None:
        with self.assertRaisesRegex(MoveOnError, "not JSON"):
            _decode_response(b"<html>temporarily unavailable</html>")
        with self.assertRaisesRegex(MoveOnError, "must be a JSON object"):
            _decode_response("[]")

    def test_payload_requires_current_counter_coverage_and_unique_complete_relations(self) -> None:
        records = [_source_record("1"), _source_record("2")]
        with self.assertRaisesRegex(MoveOnError, "count mismatch"):
            _validate_payload({"counters": {"institutions": 3}, "labelvalue": records})
        with self.assertRaisesRegex(MoveOnError, "coverage incomplete"):
            _validate_payload({"counters": {"institutions": 2}, "labelvalue": records}, expected_count=3)
        with self.assertRaisesRegex(MoveOnError, "duplicate relation_id"):
            _validate_payload({"counters": {"institutions": 2}, "labelvalue": [_source_record("1"), _source_record("1")]})
        incomplete = _source_record("3", fields=[])
        with self.assertRaisesRegex(MoveOnError, "unnamed or incomplete"):
            _validate_payload({"counters": {"institutions": 1}, "labelvalue": [incomplete]})

    def test_invalid_catalog_cannot_replace_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tumexchange-moveon-") as tmp:
            destination = Path(tmp) / "catalog.json"
            original = b'{"profiles": ["old"]}\n'
            destination.write_bytes(original)
            invalid = {
                "expected_relation_count": 2,
                "profiles": [{"relation_id": "only-one"}],
            }

            with self.assertRaisesRegex(MoveOnError, "coverage incomplete"):
                write_catalog(invalid, destination)

            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
