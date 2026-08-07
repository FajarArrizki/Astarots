"""Fork base fingerprints and branch-local snapshot transformations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from devil.core.types import ChainId, ForkSnapshot, SlotChange


class SnapshotError(ValueError):
    """Raised when fork snapshots cannot form a coherent campaign base."""


@dataclass(frozen=True)
class BaseForkFingerprint:
    """Evidence identity for one chain's immutable campaign base."""

    chain: ChainId
    block_number: int
    block_hash: str
    state_root: str
    code_hashes: tuple[tuple[str, str], ...] = ()
    artifact_hashes: tuple[tuple[str, str], ...] = ()

    @property
    def digest(self) -> str:
        payload = {
            "chain": self.chain.value,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "state_root": self.state_root,
            "code_hashes": self.code_hashes,
            "artifact_hashes": self.artifact_hashes,
        }
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )


@dataclass(frozen=True)
class SnapshotSet:
    """Coherent pinned snapshot set shared by every search branch."""

    snapshots: dict[ChainId, ForkSnapshot]
    id: str = ""

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise SnapshotError("snapshot set cannot be empty")
        if any(snapshot.base_block < 0 for snapshot in self.snapshots.values()):
            raise SnapshotError("snapshot block numbers must be non-negative")
        if not self.id:
            object.__setattr__(self, "id", self.fingerprint())

    def fingerprint(self) -> str:
        values = [
            {
                "chain": chain.value,
                "block": snapshot.base_block,
                "block_hash": snapshot.base_block_hash,
                "state_root": snapshot.state_root,
            }
            for chain, snapshot in sorted(self.snapshots.items(), key=lambda item: item[0].value)
        ]
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return "snapshot-set:" + hashlib.sha256(encoded).hexdigest()[:24]

    def snapshot(self, chain: ChainId) -> ForkSnapshot:
        try:
            return self.snapshots[chain]
        except KeyError as exc:
            raise SnapshotError(f"chain {chain.value!r} is absent from snapshot set") from exc

    def with_snapshot(self, snapshot: ForkSnapshot) -> SnapshotSet:
        next_snapshots = dict(self.snapshots)
        next_snapshots[snapshot.chain_id] = snapshot
        return SnapshotSet(next_snapshots)


def apply_slot_changes(snapshot: ForkSnapshot, changes: tuple[SlotChange, ...]) -> ForkSnapshot:
    """Apply a branch overlay without mutating the base snapshot."""
    existing = {(change.contract, change.slot): change for change in snapshot.state_diff}
    for change in changes:
        existing[(change.contract, change.slot)] = change
    touched = tuple(sorted({*snapshot.touched_slots, *(f"{c.contract}:{c.slot}" for c in changes)}))
    return replace(
        snapshot,
        overlay_id=snapshot.overlay_id + 1,
        state_diff=tuple(existing.values()),
        touched_slots=touched,
    )


def advance_environment(
    snapshot: ForkSnapshot,
    *,
    blocks: int = 0,
    seconds: int = 0,
) -> ForkSnapshot:
    """Advance only the branch-local environment, never historical base state."""
    if blocks < 0 or seconds < 0:
        raise SnapshotError("environment deltas must be non-negative")
    return replace(
        snapshot,
        block_number_delta=snapshot.block_number_delta + blocks,
        timestamp_delta=snapshot.timestamp_delta + seconds,
    )
