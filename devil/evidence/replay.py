"""Deterministic replay artifacts for vulnerable and regression traces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devil.core.types import WitnessState

_SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]+|https?://[^\s/@:]+:[^\s/@]+@)")


@dataclass(frozen=True)
class ReplayArtifact:
    """Self-contained replay contract with redacted, hashed metadata."""

    finding_id: str
    mode: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "finding_id": self.finding_id,
            "mode": self.mode,
            **dict(self.payload),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), sort_keys=True, indent=2, default=str) + "\n"
        )
        return destination


def build_replay_artifact(
    finding_id: str,
    witness: WitnessState,
    *,
    mode: str,
    metadata: Mapping[str, Any] | None = None,
) -> ReplayArtifact:
    """Build a replay from recorded base fingerprints and exact calls."""
    snapshots = {
        chain.value: {
            "fork_block": snapshot.base_block,
            "block_hash": snapshot.base_block_hash,
            "state_root": snapshot.state_root,
            "overlay_id": snapshot.overlay_id,
        }
        for chain, snapshot in sorted(
            witness.snapshot.chain_snapshots.items(), key=lambda item: item[0].value
        )
    }
    trace = [
        {
            "chain": call.chain.value if call.chain else None,
            "call": call.function_signature,
            "args": list(call.args),
        }
        for call in witness.call_sequence
    ]
    payload = _redact(
        {
            "metadata": dict(metadata or {}),
            "branch_id": witness.branch_id,
            "correlation_value": witness.correlation_value,
            "constraints": [
                {
                    "kind": item.kind,
                    "target": item.target,
                    "value": item.value,
                    "chain": item.chain.value,
                }
                for item in witness.constraints
            ],
            "snapshots": snapshots,
            "action_trace": trace,
            "trace_hash": _digest(trace),
            "assumptions": list(witness.snapshot.assumptions),
        }
    )
    return ReplayArtifact(finding_id=finding_id, mode=mode, payload=payload)


def build_replay_pair(
    finding_id: str,
    witness: WitnessState,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[ReplayArtifact, ReplayArtifact]:
    """Return vulnerability and expected-to-pass regression artifacts."""
    return (
        build_replay_artifact(finding_id, witness, mode="vulnerable", metadata=metadata),
        build_replay_artifact(finding_id, witness, mode="regression", metadata=metadata),
    )


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("<REDACTED>", value)
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value
