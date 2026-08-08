"""Pinned fork verification, coherence evidence, and immutable overlays."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from eth_hash.auto import keccak

from devil.core.config import CampaignConfig, TargetConfig
from devil.core.types import ChainId, ForkSnapshot, SlotChange, frozen_mapping

_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
_IMPLEMENTATION_SELECTOR = "0x5c60da1b"


class SnapshotError(ValueError):
    """Raised when fork snapshots or target identities fail validation."""


class RpcClient(Protocol):
    def call(self, method: str, params: list[Any]) -> Any: ...


class JsonRpcClient:
    """Small JSON-RPC client that never includes its URL in diagnostics."""

    def __init__(self, url: str, *, timeout: int = 30) -> None:
        self._url = url
        self._timeout = timeout
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"RPC {method} failed: {type(exc).__name__}") from exc
        if payload.get("error"):
            error = payload["error"]
            raise SnapshotError(f"RPC {method} failed: {error.get('code', 'unknown')}")
        if "result" not in payload:
            raise SnapshotError(f"RPC {method} returned no result")
        return payload["result"]


def keccak_hex(client: RpcClient, value: str) -> str:
    """Hash hex data through RPC, falling back to local Ethereum Keccak-256."""
    try:
        result = client.call("web3_sha3", [value])
    except Exception:
        raw = bytes.fromhex(value.removeprefix("0x"))
        return "0x" + keccak(raw).hex()
    return str(result)


@dataclass(frozen=True)
class TargetFingerprint:
    context_id: str
    address: str
    runtime_code_hash: str
    artifact_hash: str
    proxy_kind: str = "none"
    implementation_address: str = ""
    implementation_code_hash: str = ""


@dataclass(frozen=True)
class BaseForkFingerprint:
    chain: ChainId
    chain_id: int
    block_number: int
    block_hash: str
    state_root: str
    timestamp: int
    targets: Mapping[str, TargetFingerprint] = field(default_factory=dict)
    fork_cache_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", frozen_mapping(self.targets))

    @property
    def digest(self) -> str:
        payload = {
            "chain": self.chain.value,
            "chain_id": self.chain_id,
            "block": self.block_number,
            "block_hash": self.block_hash,
            "state_root": self.state_root,
            "timestamp": self.timestamp,
            "targets": {
                name: {
                    "address": target.address.lower(),
                    "code": target.runtime_code_hash,
                    "artifact": target.artifact_hash,
                    "proxy_kind": target.proxy_kind,
                    "implementation": target.implementation_address.lower(),
                    "implementation_code": target.implementation_code_hash,
                }
                for name, target in sorted(self.targets.items())
            },
            "fork_cache_hash": self.fork_cache_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CoherenceCheck:
    kind: str
    observed: Any
    relation: str
    expected: Any
    evidence_hash: str


@dataclass(frozen=True)
class SnapshotSet:
    snapshots: Mapping[ChainId, ForkSnapshot]
    base_fingerprints: Mapping[ChainId, BaseForkFingerprint]
    anchor_timestamp: int
    finality_policy: str
    protocol_epochs: Mapping[str, str] = field(default_factory=dict)
    message_cutoffs: Mapping[str, int] = field(default_factory=dict)
    coherence_checks: tuple[CoherenceCheck, ...] = ()
    schema_version: str = "1.0.0"
    id: str = ""

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise SnapshotError("snapshot set cannot be empty")
        object.__setattr__(self, "snapshots", frozen_mapping(self.snapshots))
        object.__setattr__(self, "base_fingerprints", frozen_mapping(self.base_fingerprints))
        object.__setattr__(self, "protocol_epochs", frozen_mapping(self.protocol_epochs))
        object.__setattr__(self, "message_cutoffs", frozen_mapping(self.message_cutoffs))
        if set(self.snapshots) != set(self.base_fingerprints):
            raise SnapshotError("snapshots and base fingerprints cover different chains")
        if not self.id:
            object.__setattr__(self, "id", self.fingerprint())

    @property
    def coherence_checks_hash(self) -> str:
        payload = [
            {
                "kind": check.kind,
                "observed": check.observed,
                "relation": check.relation,
                "expected": check.expected,
                "evidence_hash": check.evidence_hash,
            }
            for check in self.coherence_checks
        ]
        encoded = json.dumps(payload, sort_keys=True, default=str).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def fingerprint(self) -> str:
        payload = {
            "schema": self.schema_version,
            "bases": {
                chain.value: fingerprint.digest
                for chain, fingerprint in sorted(
                    self.base_fingerprints.items(), key=lambda item: item[0].value
                )
            },
            "anchor": self.anchor_timestamp,
            "finality": self.finality_policy,
            "epochs": dict(sorted(self.protocol_epochs.items())),
            "cutoffs": dict(sorted(self.message_cutoffs.items())),
            "checks": self.coherence_checks_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "snapshot-set:" + hashlib.sha256(encoded).hexdigest()[:24]

    def snapshot(self, chain: ChainId) -> ForkSnapshot:
        try:
            return self.snapshots[chain]
        except KeyError as exc:
            raise SnapshotError(f"chain {chain.value!r} is absent from snapshot set") from exc

    def with_snapshot(self, snapshot: ForkSnapshot) -> SnapshotSet:
        next_snapshots = dict(self.snapshots)
        next_snapshots[snapshot.chain_id] = snapshot
        return replace(self, snapshots=next_snapshots, id="")


def verify_campaign_snapshots(
    config: CampaignConfig,
    *,
    clients: Mapping[str, RpcClient] | None = None,
    observed_protocol_epochs: Mapping[str, str] | None = None,
    observed_message_cutoffs: Mapping[str, int] | None = None,
) -> SnapshotSet:
    """Verify RPC, block, code, proxy, artifact, finality, and coherence fingerprints."""
    errors = config.validate(check_paths=True)
    if errors:
        raise SnapshotError("invalid campaign configuration: " + "; ".join(errors))
    supplied_clients = dict(clients or {})
    fingerprints: dict[ChainId, BaseForkFingerprint] = {}
    snapshots: dict[ChainId, ForkSnapshot] = {}
    checks: list[CoherenceCheck] = []
    timestamps: list[int] = []

    for alias, chain_config in sorted(config.chains.items()):
        client = supplied_clients.get(alias) or JsonRpcClient(chain_config.rpc_url())
        observed_chain_id = int(client.call("eth_chainId", []), 16)
        if observed_chain_id != chain_config.chain_id:
            raise SnapshotError(
                f"{alias}: chain ID mismatch ({observed_chain_id} != {chain_config.chain_id})"
            )
        block_tag = hex(chain_config.fork_block)
        block = client.call("eth_getBlockByNumber", [block_tag, False])
        if not isinstance(block, Mapping):
            raise SnapshotError(f"{alias}: pinned block does not exist")
        block_hash = str(block.get("hash", ""))
        state_root = str(block.get("stateRoot", ""))
        timestamp = int(str(block.get("timestamp", "0x0")), 16)
        _require_expected(alias, "block hash", chain_config.expected_block_hash, block_hash)
        _require_expected(alias, "state root", chain_config.expected_state_root, state_root)
        if config.snapshot.require_finalized:
            finalized = client.call("eth_getBlockByNumber", ["finalized", False])
            if not isinstance(finalized, Mapping):
                raise SnapshotError(f"{alias}: RPC does not expose a finalized block")
            finalized_number = int(str(finalized.get("number", "0x0")), 16)
            if chain_config.fork_block > finalized_number:
                raise SnapshotError(
                    f"{alias}: block {chain_config.fork_block} is newer than "
                    f"finalized {finalized_number}"
                )
            checks.append(
                _check("finality", chain_config.fork_block, "<=", finalized_number, block_hash)
            )
        target_fingerprints: dict[str, TargetFingerprint] = {}
        for context, target in sorted(config.targets.items()):
            if target.chain != alias:
                continue
            target_fingerprints[context] = _verify_target(
                client, target, block_tag, config.resolve_path(target.artifact)
            )
        chain_id = ChainId(alias)
        fingerprint = BaseForkFingerprint(
            chain=chain_id,
            chain_id=chain_config.chain_id,
            block_number=chain_config.fork_block,
            block_hash=block_hash,
            state_root=state_root,
            timestamp=timestamp,
            targets=target_fingerprints,
        )
        fingerprints[chain_id] = fingerprint
        snapshots[chain_id] = ForkSnapshot(
            chain_id=chain_id,
            base_block=chain_config.fork_block,
            base_block_hash=block_hash,
            state_root=state_root,
            base_timestamp=timestamp,
        )
        timestamps.append(timestamp)

    anchor = config.snapshot.anchor_timestamp
    if anchor is None:
        anchor = min(timestamps)
    for chain, fingerprint in fingerprints.items():
        delta = abs(fingerprint.timestamp - anchor)
        if delta > config.snapshot.max_timestamp_delta:
            raise SnapshotError(
                f"{chain.value}: timestamp delta {delta} exceeds "
                f"{config.snapshot.max_timestamp_delta}"
            )
        checks.append(
            _check(
                "timestamp_delta",
                delta,
                "<=",
                config.snapshot.max_timestamp_delta,
                fingerprint.block_hash,
            )
        )

    observed_epochs = dict(observed_protocol_epochs or {})
    if config.snapshot.protocol_epochs and not observed_epochs:
        raise SnapshotError("protocol epoch checks require adapter observations")
    for name, expected in config.snapshot.protocol_epochs.items():
        observed = observed_epochs.get(name)
        if observed != expected:
            raise SnapshotError(f"protocol epoch {name!r} mismatch ({observed!r} != {expected!r})")
        checks.append(_check("epoch", observed, "==", expected, fingerprints_digest(fingerprints)))

    observed_cutoffs = dict(observed_message_cutoffs or {})
    if config.snapshot.message_cutoffs and not observed_cutoffs:
        raise SnapshotError("message cutoff checks require relay dataset observations")
    for emitter, expected in config.snapshot.message_cutoffs.items():
        observed = observed_cutoffs.get(emitter)
        if observed != expected:
            raise SnapshotError(
                f"message cutoff {emitter!r} mismatch ({observed!r} != {expected!r})"
            )
        checks.append(_check("cutoff", observed, "==", expected, fingerprints_digest(fingerprints)))

    return SnapshotSet(
        snapshots=snapshots,
        base_fingerprints=fingerprints,
        anchor_timestamp=anchor,
        finality_policy=config.snapshot.finality_policy,
        protocol_epochs=config.snapshot.protocol_epochs,
        message_cutoffs=config.snapshot.message_cutoffs,
        coherence_checks=tuple(checks),
    )


def _verify_target(
    client: RpcClient, target: TargetConfig, block_tag: str, artifact_path: Path
) -> TargetFingerprint:
    code = str(client.call("eth_getCode", [target.address, block_tag]))
    if code in {"", "0x"}:
        raise SnapshotError(f"{target.context}: no deployed runtime code at pinned block")
    code_hash = keccak_hex(client, code)
    _require_expected(target.context, "runtime code hash", target.expected_code_hash, code_hash)
    artifact_hash = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    implementation_address = ""
    implementation_code_hash = ""
    if target.proxy_kind != "none":
        implementation_address = _resolve_proxy(client, target, block_tag)
        if implementation_address.lower() != target.implementation_address.lower():
            raise SnapshotError(
                f"{target.context}: proxy implementation mismatch "
                f"({implementation_address} != {target.implementation_address})"
            )
        implementation_code = str(client.call("eth_getCode", [implementation_address, block_tag]))
        if implementation_code in {"", "0x"}:
            raise SnapshotError(f"{target.context}: implementation has no code")
        implementation_code_hash = keccak_hex(client, implementation_code)
        _require_expected(
            target.context,
            "implementation code hash",
            target.expected_implementation_code_hash,
            implementation_code_hash,
        )
    return TargetFingerprint(
        context_id=target.context,
        address=target.address,
        runtime_code_hash=code_hash,
        artifact_hash=artifact_hash,
        proxy_kind=target.proxy_kind,
        implementation_address=implementation_address,
        implementation_code_hash=implementation_code_hash,
    )


def _resolve_proxy(client: RpcClient, target: TargetConfig, block_tag: str) -> str:
    slot = _BEACON_SLOT if target.proxy_kind == "beacon" else _IMPLEMENTATION_SLOT
    value = str(client.call("eth_getStorageAt", [target.address, slot, block_tag]))
    referenced_address = _word_address(value)
    if target.proxy_kind != "beacon":
        return referenced_address
    result = str(
        client.call(
            "eth_call",
            [{"to": referenced_address, "data": _IMPLEMENTATION_SELECTOR}, block_tag],
        )
    )
    return _word_address(result)


def _word_address(value: str) -> str:
    cleaned = value.removeprefix("0x").rjust(64, "0")
    address = "0x" + cleaned[-40:]
    if int(address, 16) == 0:
        raise SnapshotError("proxy implementation slot is zero")
    return address


def _require_expected(scope: str, label: str, expected: str, observed: str) -> None:
    if not expected:
        raise SnapshotError(f"{scope}: expected {label} is required before probing")
    if observed.lower() != expected.lower():
        raise SnapshotError(f"{scope}: {label} mismatch ({observed} != {expected})")


def _check(kind: str, observed: Any, relation: str, expected: Any, seed: str) -> CoherenceCheck:
    material = json.dumps(
        {
            "kind": kind,
            "observed": observed,
            "relation": relation,
            "expected": expected,
            "seed": seed,
        },
        sort_keys=True,
        default=str,
    ).encode()
    return CoherenceCheck(
        kind,
        observed,
        relation,
        expected,
        "sha256:" + hashlib.sha256(material).hexdigest(),
    )


def fingerprints_digest(fingerprints: Mapping[ChainId, BaseForkFingerprint]) -> str:
    material = "|".join(
        fingerprint.digest
        for _, fingerprint in sorted(fingerprints.items(), key=lambda item: item[0].value)
    )
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()


def apply_slot_changes(snapshot: ForkSnapshot, changes: tuple[SlotChange, ...]) -> ForkSnapshot:
    existing = {(change.contract, change.slot): change for change in snapshot.state_diff}
    for change in changes:
        existing[(change.contract, change.slot)] = change
    ordered = tuple(existing[key] for key in sorted(existing))
    touched = tuple(sorted(set(snapshot.touched_slots) | {change.slot for change in changes}))
    return replace(
        snapshot,
        state_diff=ordered,
        touched_slots=touched,
        overlay_id=snapshot.overlay_id + 1,
    )


def advance_environment(
    snapshot: ForkSnapshot, *, blocks: int = 0, seconds: int = 0
) -> ForkSnapshot:
    if blocks < 0 or seconds < 0:
        raise SnapshotError("environment deltas cannot move backwards")
    return replace(
        snapshot,
        block_number_delta=snapshot.block_number_delta + blocks,
        timestamp_delta=snapshot.timestamp_delta + seconds,
        overlay_id=snapshot.overlay_id + 1,
    )
