from __future__ import annotations

from collections import Counter

from reference_agent.repair import (
    CONCEPT_PASS_CHECKS,
    ToolCallCheck,
    corrective_message,
)


def test_satisfied_returns_none():
    counts = Counter({"read_concept_raw": 1, "write_concept_doc": 1})
    assert corrective_message(CONCEPT_PASS_CHECKS, counts, concept_id="utils/Foo") is None


def test_missing_write_yields_corrective_with_concept_id():
    counts = Counter({"read_concept_raw": 1})  # never called write_concept_doc
    msg = corrective_message(CONCEPT_PASS_CHECKS, counts, concept_id="utils/Foo")
    assert msg is not None
    assert "write_concept_doc" in msg
    assert "utils/Foo" in msg


def test_checklist_is_extensible_multiple_checks():
    checks = (
        ToolCallCheck(tool="read_concept_raw", instruction="call read_concept_raw"),
        ToolCallCheck(tool="write_concept_doc", instruction="call write_concept_doc for {concept_id}"),
    )
    # Neither called -> both surfaced in one combined message.
    msg = corrective_message(checks, Counter(), concept_id="a/B")
    assert "read_concept_raw" in msg
    assert "write_concept_doc" in msg
    assert "a/B" in msg
    # One satisfied -> only the other remains.
    msg2 = corrective_message(checks, Counter({"read_concept_raw": 1}), concept_id="a/B")
    assert "read_concept_raw" not in msg2
    assert "write_concept_doc" in msg2


def test_min_calls_threshold():
    check = ToolCallCheck(tool="t", min_calls=2, instruction="call t twice")
    assert check.unsatisfied(Counter({"t": 1})) is True
    assert check.unsatisfied(Counter({"t": 2})) is False
