"""Content-addressed relay, actor, ABI, and correlation input loaders."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from devil.core.config import CampaignConfig
from devil.core.types import Actor, ActorPolicy, ChainId, RelayDataset, RelayMessage
from devil.invariant.ir import CorrelationExtractor, EventSelector, TransformRef


def content_hash(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_relay_dataset(path: str | Path, expected_hash: str) -> RelayDataset:
    source = Path(path)
    observed_hash = content_hash(source)
    if observed_hash != expected_hash:
        raise ValueError(f"relay dataset hash mismatch ({observed_hash} != {expected_hash})")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("relay dataset root must be an object")
    messages = tuple(_relay_message(item) for item in payload.get("messages", []))
    ranges = {
        ChainId(chain): (int(value[0]), int(value[1]))
        for chain, value in _mapping(
            payload.get("source_block_ranges", {}), "source_block_ranges"
        ).items()
    }
    return RelayDataset(
        schema_version=str(payload.get("schema_version", "1.0.0")),
        dataset_hash=observed_hash,
        protocol=str(payload["protocol"]),
        source_block_ranges=ranges,
        messages=messages,
        provenance=str(payload.get("provenance", "")),
        provenance_hash=str(payload.get("provenance_hash", "")),
    )


def load_actor_policy(path: str | Path, expected_hash: str) -> ActorPolicy:
    source = Path(path)
    observed_hash = content_hash(source)
    if observed_hash != expected_hash:
        raise ValueError(f"actor policy hash mismatch ({observed_hash} != {expected_hash})")
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
    else:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("actor policy root must be an object")
    actors = tuple(
        Actor(
            address=str(item["address"]),
            role=str(item.get("role", "attacker")),
            provenance=str(item.get("provenance", "fork_state")),
            privilege_level=str(item.get("privilege_level", "none")),
            impersonation_allowed=bool(item.get("impersonation_allowed", False)),
            funding_method=str(item.get("funding_method", "from_fork_balance")),
        )
        for item in payload.get("actors", [])
        if isinstance(item, Mapping)
    )
    if not actors:
        raise ValueError("actor policy must declare at least one actor")
    return ActorPolicy(str(payload.get("id", source.stem)), observed_hash, actors)


def correlation_extractors(config: CampaignConfig) -> dict[str, CorrelationExtractor]:
    result: dict[str, CorrelationExtractor] = {}
    for name, value in config.correlations.items():
        function, separator, version = value.normalize.partition("@")
        if not separator:
            function, version = value.normalize, "1"
        result[name] = CorrelationExtractor(
            EventSelector(value.source_context, value.source_event),
            EventSelector(value.destination_context, value.destination_event),
            value.source_fields,
            value.destination_fields,
            TransformRef(function, version),
        )
    return result


def load_abi_signatures(config: CampaignConfig) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for context, target in config.targets.items():
        payload = json.loads(config.resolve_path(target.artifact).read_text(encoding="utf-8"))
        abi = payload.get("abi", payload) if isinstance(payload, Mapping) else payload
        if not isinstance(abi, list):
            raise ValueError(f"{context}: artifact ABI is not a list")
        signatures: set[str] = set()
        for item in abi:
            if not isinstance(item, Mapping) or item.get("type") != "function":
                continue
            inputs = ",".join(str(argument["type"]) for argument in item.get("inputs", []))
            signatures.add(f"{item['name']}({inputs})")
        result[context] = frozenset(signatures)
    return result


def load_storage_layouts(config: CampaignConfig) -> dict[str, dict[str, str]]:
    layouts: dict[str, dict[str, str]] = {}
    for context, target in config.targets.items():
        payload = json.loads(config.resolve_path(target.artifact).read_text(encoding="utf-8"))
        layout = payload.get("storageLayout", {}) if isinstance(payload, Mapping) else {}
        storage = layout.get("storage", []) if isinstance(layout, Mapping) else []
        layouts[context] = {
            str(item["label"]): str(item["slot"])
            for item in storage
            if isinstance(item, Mapping) and "label" in item and "slot" in item
        }
    return layouts


def _relay_message(value: Any) -> RelayMessage:
    if not isinstance(value, Mapping):
        raise ValueError("relay message must be an object")
    return RelayMessage(
        emitter=str(value["emitter"]),
        sequence=int(value["sequence"]),
        source_chain=ChainId(str(value["source_chain"])),
        destination_chain=ChainId(str(value["destination_chain"])),
        destination_context=str(value["destination_context"]),
        payload=str(value.get("payload", "")),
        payload_hash=str(value.get("payload_hash", "")),
        attestation=str(value.get("attestation", value.get("vaa_bytes", ""))),
        attestation_hash=str(value.get("attestation_hash", value.get("vaa_hash", ""))),
        message_id=str(value.get("message_id", "")),
        correlation_value=str(value.get("correlation_value", "")),
        source_block_number=int(value.get("source_block_number", 0)),
        source_block_hash=str(value.get("source_block_hash", "")),
        source_log_index=int(value.get("source_log_index", 0)),
        source_event_hash=str(value.get("source_event_hash", "")),
        emitted_timestamp=int(value.get("emitted_timestamp", 0)),
        guardian_set_index=int(value.get("guardian_set_index", 0)),
        destination_status=str(value.get("destination_status", "unknown")),
        status_evidence_hash=str(value.get("status_evidence_hash", "")),
        protocol_metadata=_mapping(value.get("protocol_metadata", {}), "protocol_metadata"),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value
