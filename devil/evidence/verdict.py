"""Conservative whole-trace evidence-strength aggregation."""

from __future__ import annotations

from dataclasses import dataclass

from devil.core.types import EdgeCase, EvidenceStrength, SearchResult, Verdict


@dataclass(frozen=True)
class Confidence:
    """Explain evidence without converting labels into an invented probability."""

    verdict: Verdict
    strength: EvidenceStrength
    reasons: tuple[str, ...] = ()


def assess(result: SearchResult) -> Confidence:
    if result.outcome is Verdict.VIOLATED and result.deepest_edge:
        edge = result.deepest_edge
        return Confidence(
            result.outcome,
            edge.aggregate_strength,
            (
                "counterexample observed",
                f"trace depth {edge.depth}",
                f"aggregation rule {edge.aggregation_rule}",
            ),
        )
    if result.outcome is Verdict.INCONCLUSIVE:
        return Confidence(
            result.outcome,
            EvidenceStrength.OBSERVED,
            ("campaign incomplete",),
        )
    return Confidence(
        result.outcome,
        EvidenceStrength.OBSERVED,
        ("no violation observed within bounds",),
    )


def strongest_trace_strength(edge: EdgeCase) -> EvidenceStrength:
    return edge.aggregate_strength
