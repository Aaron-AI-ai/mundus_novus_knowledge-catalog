"""Post-run tool-call expectations for the agent self-heal loop.

After the agent finishes a concept turn, the runner inspects which tools were
actually called. Small/local models sometimes *describe* a tool call (e.g.
emit JSON as text) instead of invoking it, leaving the document unwritten.
Each expectation below states a tool-call contract; when one is unmet the
runner sends the corrective instruction back into the same session and lets the
model try again.

Extending: append more `ToolCallCheck` entries to `CONCEPT_PASS_CHECKS` (or
build a different checklist) to enforce additional contracts — e.g. requiring a
`read_concept_raw` before `write_concept_doc`, or a minimum number of calls.
The runner treats the checklist generically, so no runner changes are needed to
add a new check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCallCheck:
    """A single expectation about how often a tool was called during a run."""

    tool: str
    #: Minimum number of times `tool` must be called for the run to be complete.
    min_calls: int = 1
    #: Corrective instruction emitted when unsatisfied. `{concept_id}` is
    #: substituted with the current concept id at runtime.
    instruction: str = ""

    def unsatisfied(self, counts: Mapping[str, int]) -> bool:
        return counts.get(self.tool, 0) < self.min_calls

    def render(self, *, concept_id: str) -> str:
        try:
            return self.instruction.format(concept_id=concept_id)
        except (KeyError, IndexError):
            return self.instruction


# Checklist for the per-concept enrichment pass. Add entries here to enforce
# more tool-call contracts without touching the runner.
CONCEPT_PASS_CHECKS: tuple[ToolCallCheck, ...] = (
    ToolCallCheck(
        tool="write_concept_doc",
        min_calls=1,
        instruction=(
            "You did NOT call the write_concept_doc tool, so no document was "
            "saved. Stop reading or calling other tools. Do not output the "
            "document as text or JSON — invoke write_concept_doc now, exactly "
            "once, with concept_id='{concept_id}' and the complete frontmatter "
            "and body."
        ),
    ),
)


def corrective_message(
    checks: tuple[ToolCallCheck, ...],
    counts: Mapping[str, int],
    *,
    concept_id: str,
) -> str | None:
    """Return a single follow-up message covering every unsatisfied check, or
    None when the run already met all expectations."""
    missing = [c for c in checks if c.unsatisfied(counts)]
    if not missing:
        return None
    lines = [
        "A required tool call was missing from your last response. "
        "Respond ONLY by calling the tool(s) below — no prose, no JSON text:"
    ]
    lines += [f"- {c.render(concept_id=concept_id)}" for c in missing]
    return "\n".join(lines)
