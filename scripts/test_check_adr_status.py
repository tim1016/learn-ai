"""Regression tests for scripts/check_adr_status.py.

Stdlib-only (unittest), mirroring the guard's own zero-dependency design —
the CI job that runs the guard installs nothing but Python itself.

Run directly: ``python scripts/test_check_adr_status.py``
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import check_adr_status as guard

_ACCEPTED_0040_PLUS = "\n".join(
    [
        "# ADR 0042: Example",
        "",
        "**Status:** Accepted",
        "",
        "- **Date:** 2026-08-19",
        "- **Vocabulary:** `CONTEXT.md` § \"Example section\".",
        "",
        "## Decision",
        "",
        "Body text.",
        "",
    ]
)


class ExistingCorpusStaysGreen(unittest.TestCase):
    def test_current_corpus_has_no_violations(self) -> None:
        paths = sorted(p for p in guard.ADR_DIR.glob("*.md") if p.is_file())
        self.assertTrue(paths, "expected at least one committed ADR")
        violations = guard.check_all(paths)
        self.assertEqual(violations, [], f"unexpected violations in committed corpus: {violations}")


class VocabularyGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_adr_0040_plus_accepted_with_vocabulary_line_is_clean(self) -> None:
        path = self._write("0042-example.md", _ACCEPTED_0040_PLUS)
        self.assertEqual(guard.check_file(path), [])

    def test_removing_the_vocabulary_line_is_a_violation(self) -> None:
        """Regression for #1668: stripping ADR 0041's Vocabulary line must fail."""
        without_vocab = "\n".join(
            line for line in _ACCEPTED_0040_PLUS.splitlines() if "Vocabulary" not in line
        )
        path = self._write("0042-example.md", without_vocab)
        violations = guard.check_file(path)
        self.assertEqual([v.rule for v in violations], ["missing-vocabulary"])

    def test_empty_vocabulary_line_is_a_violation(self) -> None:
        text = _ACCEPTED_0040_PLUS.replace(
            '- **Vocabulary:** `CONTEXT.md` § "Example section".',
            "- **Vocabulary:**",
        )
        path = self._write("0042-example.md", text)
        violations = guard.check_file(path)
        self.assertEqual([v.rule for v in violations], ["empty-vocabulary"])

    def test_duplicate_vocabulary_lines_is_a_violation(self) -> None:
        text = _ACCEPTED_0040_PLUS.replace(
            '- **Vocabulary:** `CONTEXT.md` § "Example section".',
            '- **Vocabulary:** `CONTEXT.md` § "Example section".\n'
            "- **Vocabulary:** none owed — duplicate for the test.",
        )
        path = self._write("0042-example.md", text)
        violations = guard.check_file(path)
        self.assertEqual([v.rule for v in violations], ["duplicate-vocabulary"])

    def test_none_owed_reason_counts_as_present(self) -> None:
        text = _ACCEPTED_0040_PLUS.replace(
            '- **Vocabulary:** `CONTEXT.md` § "Example section".',
            "- **Vocabulary:** none owed — repo-process decision, not domain language.",
        )
        path = self._write("0042-example.md", text)
        self.assertEqual(guard.check_file(path), [])

    def test_pre_0040_adr_does_not_owe_vocabulary(self) -> None:
        """Forward-only per ADR 0040 Decision 4 — no backfill for 0039 and earlier."""
        without_vocab = "\n".join(
            line for line in _ACCEPTED_0040_PLUS.splitlines() if "Vocabulary" not in line
        )
        path = self._write("0039-pre-existing.md", without_vocab)
        self.assertEqual(guard.check_file(path), [])

    def test_non_accepted_adr_does_not_owe_vocabulary(self) -> None:
        proposed = _ACCEPTED_0040_PLUS.replace("**Status:** Accepted", "**Status:** Proposed").replace(
            "- **Vocabulary:** `CONTEXT.md` § \"Example section\".", ""
        )
        path = self._write("0042-example.md", proposed)
        self.assertEqual(guard.check_file(path), [])


if __name__ == "__main__":
    unittest.main()
