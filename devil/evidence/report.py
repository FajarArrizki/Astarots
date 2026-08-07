"""Stable JSON and human-readable campaign reporting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from devil.core.types import EdgeCase, SearchResult
from devil.evidence.replay import _digest


@dataclass(frozen=True)
class EvidenceReport:
    """Serializable report separating verdict from evidence strength."""

    invariant_id: str
    result: SearchResult
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        findings = (
            [_finding(self.invariant_id, self.result.deepest_edge)]
            if self.result.deepest_edge
            else []
        )
        verdict = self.result.outcome.value
        return {
            "schema_version": "1.0.0",
            "mode": "mainnet-fork",
            "metadata": dict(self.metadata),
            "findings": findings,
            "invariant_results": [
                {
                    "name": self.invariant_id,
                    "verdict": verdict,
                    "finding_ids": [item["finding_id"] for item in findings],
                }
            ],
            "summary": {
                "invariants_total": 1,
                "findings_total": len(findings),
                "invariants_violated": int(verdict == "violated"),
                "invariants_not_observed": int(verdict == "not_observed"),
                "invariants_inconclusive": int(verdict == "inconclusive"),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, default=str) + "\n"

    def to_console(self) -> str:
        payload = self.to_dict()
        summary = payload["summary"]
        lines = [
            f"Invariant: {self.invariant_id}",
            f"Verdict: {payload['invariant_results'][0]['verdict']}",
            f"Findings: {summary['findings_total']}",
            f"Budget: {self.result.budget_used}/{self.result.budget_total}",
        ]
        if self.result.deepest_edge:
            lines.append(f"Deepest edge: depth={self.result.deepest_edge.depth}")
        return "\n".join(lines)


def _finding(invariant_id: str, edge: EdgeCase) -> dict[str, Any]:
    witness = edge.witnesses[0] if edge.witnesses else None
    trace = []
    if witness:
        trace = [
            {
                "chain": call.chain.value if call.chain else None,
                "call": call.function_signature,
                "args": list(call.args),
            }
            for call in witness.call_sequence
        ]
    finding_id = f"{invariant_id}-{_digest(trace)[7:19]}"
    return {
        "finding_id": finding_id,
        "invariant": invariant_id,
        "verdict": "violated",
        "violation_source": edge.violation_source.value,
        "aggregate_strength": edge.evidence_strength.value,
        "bounds": {"global_depth": edge.depth},
        "chains": [chain.value for chain in edge.chains],
        "action_trace": trace,
        "evidence": [
            {
                "tool": evidence.tool,
                "outcome": evidence.outcome.value,
                "raw_hash": evidence.raw_hash,
            }
            for evidence in (witness.evidence if witness else ())
        ],
    }
