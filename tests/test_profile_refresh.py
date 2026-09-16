"""Refresh summaries must identify source changes without timestamp noise."""

import unittest

from tumexchange.cli import _profile_changes


class ProfileRefreshTests(unittest.TestCase):
    def test_refresh_distinguishes_content_changes_from_retrieval_time(self):
        old = {"profiles": [
            {"relation_id": "1", "name": "A", "levels": ["bachelor"], "retrieved_at": "old"},
            {"relation_id": "2", "name": "B", "levels": ["bachelor"], "retrieved_at": "old"},
            {"relation_id": "3", "name": "C", "retrieved_at": "old"},
        ]}
        new = {"profiles": [
            {"relation_id": "1", "name": "A", "levels": ["bachelor"], "retrieved_at": "new"},
            {"relation_id": "2", "name": "B", "levels": ["master"], "retrieved_at": "new"},
            {"relation_id": "4", "name": "D", "retrieved_at": "new"},
        ]}
        self.assertEqual(_profile_changes(old, new), {
            "added": ["4"], "removed": ["3"], "changed": ["2"], "unchanged": 1,
        })

    def test_first_import_reports_all_source_relations_as_new(self):
        self.assertEqual(_profile_changes(None, {"profiles": [{"relation_id": "7"}]}), {
            "added": ["7"], "removed": [], "changed": [], "unchanged": 0,
        })


if __name__ == "__main__":
    unittest.main()
