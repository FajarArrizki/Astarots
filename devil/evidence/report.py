"""Stable reports that require enough evidence for independent reproduction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from devil.core.types import Call, EdgeCase, EnvironmentTransition, RelayTransition, SearchResult
from devil.evidence.replay import _digest

_REQUIRED_METADATA = {
    "run_id",
    "project",
    "tools",
    "runtime",
    "snapshot_set",
    "relay",
    "actor_policy_hash",
    "execution",
    "search",
}
_REQUIRED_PROJECT = {"revision", "effective_config_hash", "invariant_ir_hash"}
_REQUIRED_SNAPSHOT = {"id", "coherence_checks_hash", "chains"}
_REQUIRED_RELAY = {"mode", "dataset_hash", "policy_hash", "adapter_config_hash", "message_ids"}


@dataclass(frozen=True)
class EvidenceReport:
    invariant_id: str
    result: SearchResult
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        errors = validate_evidence_metadata(self.metadata)
        if errors:
            raise ValueError("incomplete evidence metadata: " + "; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        findings = [
            _finding(self.invariant_id, edge, self.metadata)
            for edge in sorted(
                self.result.edges,
                key=lambda item: (
                    item.depth,
                    _digest(_trace(item)),
                ),
            )
        ]
        verdict = self.result.outcome.value
        return {
            "schema_version": "1.0.0",
            "mode": "mainnet-fork",
            "run_id": self.metadata["run_id"],
            "metadata": dict(self.metadata),
            "findings": findings,
            "invariant_results": [
                {
                    "name": self.invariant_id,
                    "verdict": verdict,
                    "finding_ids": [item["finding_id"] for item in findings],
                    "baseline": self.result.baseline.status.value,
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
        lines = [
            f"Invariant: {self.invariant_id}",
            f"Verdict: {payload['invariant_results'][0]['verdict']}",
            f"Baseline: {self.result.baseline.status.value}",
            f"Findings: {payload['summary']['findings_total']}",
            f"Budget: {self.result.budget_used}/{self.result.budget_total}",
        ]
        if self.result.deepest_edge:
            edge = self.result.deepest_edge
            lines.extend(
                (
                    f"Deepest edge: depth={edge.depth}",
                    f"Violation source: {edge.violation_source.value}",
                    f"Aggregate strength: {edge.aggregate_strength.value}",
                    f"Violated clauses: {', '.join(edge.violated_clauses)}",
                )
            )
        return "\n".join(lines)


def validate_evidence_metadata(metadata: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = _REQUIRED_METADATA - set(metadata)
    if missing:
        errors.append("missing top-level fields: " + ", ".join(sorted(missing)))
    project = metadata.get("project")
    if isinstance(project, Mapping):
        absent = _REQUIRED_PROJECT - set(project)
        if absent:
            errors.append("project missing: " + ", ".join(sorted(absent)))
    elif "project" in metadata:
        errors.append("project must be an object")
    snapshot = metadata.get("snapshot_set")
    if isinstance(snapshot, Mapping):
        absent = _REQUIRED_SNAPSHOT - set(snapshot)
        if absent:
            errors.append("snapshot_set missing: " + ", ".join(sorted(absent)))
        chains = snapshot.get("chains")
        if not isinstance(chains, Mapping) or len(chains) < 2:
            errors.append("snapshot_set.chains must contain at least two chains")
        else:
            for chain, value in chains.items():
                required = {"chain_id", "fork_block", "block_hash", "state_root", "targets"}
                if not isinstance(value, Mapping) or required - set(value):
                    errors.append(f"snapshot chain {chain!r} lacks fingerprints")
    elif "snapshot_set" in metadata:
        errors.append("snapshot_set must be an object")
    relay = metadata.get("relay")
    if isinstance(relay, Mapping):
        absent = _REQUIRED_RELAY - set(relay)
        if absent:
            errors.append("relay missing: " + ", ".join(sorted(absent)))
    elif "relay" in metadata:
        errors.append("relay must be an object")
    for mapping_name in ("tools", "runtime", "execution", "search"):
        value = metadata.get(mapping_name)
        if mapping_name in metadata and (not isinstance(value, Mapping) or not value):
            errors.append(f"{mapping_name} must be a non-empty object")
    for name in (
        "effective_config_hash",
        "invariant_ir_hash",
    ):
        if isinstance(project, Mapping) and not str(project.get(name, "")).startswith("sha256:"):
            errors.append(f"project.{name} must be content-addressed")
    return errors


def _finding(invariant_id: str, edge: EdgeCase, metadata: Mapping[str, Any]) -> dict[str, Any]:
    trace = _trace(edge)
    finding_id = f"{invariant_id}-{_digest([trace, edge.violated_clauses])[7:19]}"
    witness_evidence = edge.witness.evidence if edge.witness else ()
    actor = next(
        (
            step.actor
            for step in (edge.witness.call_sequence if edge.witness else ())
            if isinstance(step, Call) and step.actor is not None
        ),
        None,
    )
    return {
        "finding_id": finding_id,
        "invariant": invariant_id,
        "verdict": "violated",
        "violation_source": edge.violation_source.value,
        "violated_clauses": list(edge.violated_clauses),
        "aggregate_strength": edge.aggregate_strength.value,
        "segment_strengths": {
            segment: strength.value for segment, strength in edge.segment_strengths.items()
        },
        "aggregation_rule": edge.aggregation_rule,
        "bounds": {
            "global_depth": edge.depth,
            "budget_used": metadata["search"].get("budget_used"),
            "budget_total": metadata["search"].get("budget_total"),
        },
        "impact": {
            "severity": edge.impact.severity,
            "description": edge.impact.description,
            "attacker_model": edge.impact.attacker_model.value,
        },
        "actor": vars(actor) if actor else None,
        "actor_policy_hash": metadata["actor_policy_hash"],
        "chains": [chain.value for chain in edge.chains],
        "snapshot_set": metadata["snapshot_set"],
        "relay": metadata["relay"],
        "assumptions": list(edge.witness.snapshot.assumptions) if edge.witness else [],
        "action_trace": trace,
        "evidence": [
            {
                "tool": evidence.tool,
                "outcome": evidence.outcome.value,
                "raw_hash": evidence.raw_hash,
                "artifact_hashes": list(evidence.artifact_hashes),
            }
            for evidence in witness_evidence
        ],
        "tool_versions": metadata["tools"],
        "runtime_versions": metadata["runtime"],
    }


def _trace(edge: EdgeCase) -> list[dict[str, Any]]:
    if edge.witness is None:
        return []
    return _serialize_trace(edge.witness.call_sequence)


def _serialize_trace(steps: tuple[Any, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, Call):
            result.append(
                {
                    "kind": "call",
                    "chain": step.chain.value if step.chain else None,
                    "context_id": step.context_id,
                    "call": step.function_signature,
                    "calldata": step.calldata,
                    "args": list(step.args),
                    "value": step.value,
                    "actor": step.actor.address if step.actor else None,
                }
            )
        elif isinstance(step, RelayTransition):
            result.append(
                {
                    "kind": "relay",
                    "message_id": step.message_id,
                    "action": step.action.value,
                    "from": step.from_status.value,
                    "to": step.to_status.value,
                    "mode": step.relay_mode.value,
                    "policy_ref": step.policy_ref,
                }
            )
        elif isinstance(step, EnvironmentTransition):
            result.append(
                {
                    "kind": "environment",
                    "chain": step.chain.value,
                    "block": step.target_block,
                    "timestamp": step.target_timestamp,
                    "reason": step.reason.value,
                    "policy_ref": step.policy_ref,
                }
            )
    return result
