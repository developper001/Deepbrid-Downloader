from __future__ import annotations

import unittest

from scripts.extract_release_notes import release_notes_for_tag


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_the_requested_version_section(self) -> None:
        changelog = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n- Upcoming change\n\n"
            "## [v1.2.3] - 2026-11-04\n\n"
            "- First change\n- Second change\n\n"
            "## [v1.2.2] - 2026-11-03\n\n- Previous change\n"
        )

        self.assertEqual(
            release_notes_for_tag(changelog, "v1.2.3"),
            "- First change\n- Second change",
        )

    def test_rejects_missing_release_section(self) -> None:
        with self.assertRaisesRegex(ValueError, "No changelog section"):
            release_notes_for_tag("# Changelog\n\n## [Unreleased]\n\n- Next\n", "v1.0.0")

    def test_rejects_empty_release_section(self) -> None:
        with self.assertRaisesRegex(ValueError, "is empty"):
            release_notes_for_tag("## [v1.0.0] - 2026-11-04\n\n## [Unreleased]\n", "v1.0.0")


if __name__ == "__main__":
    unittest.main()
