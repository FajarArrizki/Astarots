"""Verdict and whole-trace evidence-strength aggregation."""

from __future__ import annotations

from dataclasses import dataclass

from devil.core.types import EdgeCase, EvidenceStrength, SearchResult, Verdict


@dataclass(frozen=True)
class Confidence:
    """Explainable confidence score; it never changes the factual verdict."""

    verdict: Verdict
    strength: EvidenceStrength
    score: float
    reasons: tuple[str, ...] = ()


def assess(result: SearchResult) -> Confidence:
    """Aggregate strength conservatively across the entire causal trace."""
    if result.outcome is Verdict.VIOLATED and result.deepest_edge:
        edge = result.deepest_edge
        strength = edge.evidence_strength
        score = {
            EvidenceStrength.OBSERVED: 0.70,
            EvidenceStrength.REPLAYED: 0.85,
            EvidenceStrength.SYMBOLICALLY_CONFIRMED: 0.95,
            EvidenceStrength.SYMBOLICALLY_CONFIRMED_UNDER_PROJECTED_STATE: 0.80,
        }[strength]
        reasons = ("counterexample observed", f"trace depth {edge.depth}")
        if edge.independently_confirmed:
            reasons += ("independently confirmed",)
        return Confidence(result.outcome, strength, score, reasons)
    if result.outcome is Verdict.INCONCLUSIVE:
        return Confidence(result.outcome, EvidenceStrength.OBSERVED, 0.0, ("campaign incomplete",))
    return Confidence(
        result.outcome, EvidenceStrength.OBSERVED, 0.0, ("no violation observed within bounds",)
    )


def strongest_trace_strength(edge: EdgeCase) -> EvidenceStrength:
    """Return the edge's already-conservative aggregate strength."""
    return edge.evidence_strength
