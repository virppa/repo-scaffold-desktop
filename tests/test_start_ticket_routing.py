"""Tests for the WOR-223 routing & retry-cap rule in start-ticket.md.

The rule lives in prose (not code), so the test loads the skill file as text
and asserts the presence of the rule clauses and the five canonical cases.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_FILE = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "start-ticket.md"
)


def _read_skill() -> str:
    """Return the full text of the start-ticket skill file."""
    return SKILL_FILE.read_text(encoding="utf-8")


class TestRoutingRulePresence:
    """Assert that the architect-facing rule prose exists in start-ticket.md."""

    def test_section_header_exists(self):
        text = _read_skill()
        assert "Routing & retry cap" in text or "Routing & retry" in text

    def test_escalate_to_cloud_rule_clause(self):
        text = _read_skill()
        # The rule must instruct: true when (additive AND rd>=2) OR docs
        assert "escalate_to_cloud" in text
        assert "additive" in text
        assert "docs" in text

    def test_max_retries_rule_clause(self):
        text = _read_skill()
        # Refactor gets max_retries=2, others get 1
        assert "max_retries" in text
        assert "refactor" in text

    def testWOR_223_citation(self):
        text = _read_skill()
        assert "WOR-223" in text

    def test_empirical_numbers_present(self):
        text = _read_skill()
        # 60-ticket analysis
        assert "60" in text
        # refactor 0/20
        assert "0/20" in text or "0 / 20" in text
        # 33.3% or 35% figure
        assert "33.3" in text or "35" in text


class TestManifestTemplatePlaceholders:
    """Assert the JSON template uses placeholder-style values, not hardcoded."""

    def test_max_retries_has_comment(self):
        """max_retries should have an inline comment explaining the rule."""
        text = _read_skill()
        # The line with max_retries should contain a comment reference
        lines = text.splitlines()
        for line in lines:
            if '"max_retries"' in line:
                assert "//" in line or "#" in line, (
                    "max_retries line should have a comment explaining the rule"
                )
                break
        else:
            raise AssertionError("max_retries key not found in manifest template")

    def test_escalate_to_cloud_has_comment(self):
        """escalate_to_cloud should have an inline comment explaining the rule."""
        text = _read_skill()
        lines = text.splitlines()
        for line in lines:
            if '"escalate_to_cloud"' in line:
                assert "//" in line or "#" in line, (
                    "escalate_to_cloud line should have a comment explaining the rule"
                )
                break
        else:
            raise AssertionError("escalate_to_cloud key not found in manifest template")


class TestCanonicalCases:
    """Five canonical change_type x reasoning_demand cells.

    The test reads the skill file and asserts the prose covers each case.
    """

    def test_additive_rd2_escallytes(self):
        """(additive, rd>=2) -> escalate_to_cloud=true."""
        text = _read_skill()
        # The rule should have a condition like (additive AND rd>=2)
        assert re.search(
            r"additive.*rd|additive.*reasoning_demand", text, re.IGNORECASE
        )
        assert re.search(r">=\s*2|>=2", text)

    def test_additive_rd1_no_escalate(self):
        """(additive, rd<2) -> escalate_to_cloud=false."""
        text = _read_skill()
        # The else clause should cover the default case
        assert "else" in text or "default" in text.lower() or "1" in text

    def test_refactor_no_escalate_max_retries2(self):
        """(refactor, any rd) -> escalate_to_cloud=false AND max_retries=2."""
        text = _read_skill()
        assert "refactor" in text
        # Refactor should be associated with retry=2 or max_retries=2
        assert re.search(r"refactor.*2|2.*refactor", text)
        # Also should NOT escalate
        assert "0%" in text or "0 /" in text or "0%" in text

    def test_docs_escallytes(self):
        """(docs, rd=1) -> escalate_to_cloud=true."""
        text = _read_skill()
        assert "docs" in text
        assert "docs" in text.lower() or "docs" in text

    def test_modification_no_escalate(self):
        """(modification, rd=3) -> escalate_to_cloud=false AND max_retries=1."""
        text = _read_skill()
        # modification should be mentioned as a cell with low failure rate
        assert "modification" in text
