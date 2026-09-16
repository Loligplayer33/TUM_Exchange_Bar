"""CLI-level contracts for refreshing the saved MoveON profile catalog."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tumexchange import cli
from tumexchange.moveon import MoveOnError


def _catalog(*profiles: dict[str, object], retrieved_at: str = "new") -> dict[str, object]:
    return {
        "schema_version": "moveon-catalog-v1",
        "retrieved_at": retrieved_at,
        "source_url": "https://example.test/report-page-6940/",
        "expected_relation_count": len(profiles),
        "retrieved_relation_count": len(profiles),
        "counts": {"countries": 1, "institutions": len(profiles), "programs": len(profiles)},
        "profiles": list(profiles),
    }


class ProfileRefreshCliTests(unittest.TestCase):
    def test_check_reports_changes_without_replacing_saved_snapshot(self) -> None:
        old = _catalog(
            {"relation_id": "1", "name": "Old", "retrieved_at": "old"},
            retrieved_at="old",
        )
        new = _catalog(
            {"relation_id": "1", "name": "New", "retrieved_at": "new"},
            {"relation_id": "2", "name": "Added", "retrieved_at": "new"},
        )
        with tempfile.TemporaryDirectory(prefix="tumexchange-profile-cli-") as tmp:
            output = Path(tmp) / "catalog.json"
            output.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
            before = output.read_bytes()
            stdout = io.StringIO()
            with (
                patch("tumexchange.moveon.fetch_catalog", return_value=new),
                patch("tumexchange.moveon.write_catalog") as write_catalog,
                contextlib.redirect_stdout(stdout),
            ):
                result = cli.main(["fetch-profiles", "--output", str(output), "--check"])

            self.assertEqual(result, 0)
            write_catalog.assert_not_called()
            self.assertEqual(output.read_bytes(), before)
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["written"])
            self.assertEqual(summary["changes"], {
                "added": ["2"],
                "removed": [],
                "changed": ["1"],
                "unchanged": 0,
            })

    def test_successful_refresh_passes_new_catalog_to_atomic_writer(self) -> None:
        new = _catalog({"relation_id": "1", "name": "New", "retrieved_at": "new"})
        with tempfile.TemporaryDirectory(prefix="tumexchange-profile-cli-") as tmp:
            output = Path(tmp) / "catalog.json"
            stdout = io.StringIO()
            with (
                patch("tumexchange.moveon.fetch_catalog", return_value=new),
                patch("tumexchange.moveon.write_catalog") as write_catalog,
                contextlib.redirect_stdout(stdout),
            ):
                result = cli.main(["fetch-profiles", "--output", str(output)])

            self.assertEqual(result, 0)
            write_catalog.assert_called_once_with(new, output)
            self.assertTrue(json.loads(stdout.getvalue())["written"])

    def test_failed_refresh_returns_cli_error_and_preserves_saved_snapshot(self) -> None:
        old = _catalog({"relation_id": "1", "name": "Old", "retrieved_at": "old"})
        with tempfile.TemporaryDirectory(prefix="tumexchange-profile-cli-") as tmp:
            output = Path(tmp) / "catalog.json"
            output.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
            before = output.read_bytes()
            stderr = io.StringIO()
            with (
                patch("tumexchange.moveon.fetch_catalog", side_effect=MoveOnError("upstream unavailable")),
                contextlib.redirect_stderr(stderr),
            ):
                result = cli.main(["fetch-profiles", "--output", str(output)])

            self.assertEqual(result, 2)
            self.assertIn("upstream unavailable", stderr.getvalue())
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
