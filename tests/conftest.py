from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devil.core.config import (
    ActorsConfig,
    CampaignConfig,
    ChainConfig,
    ConfigDeadline,
    CorrelationConfig,
    RelayConfig,
    SnapshotConfig,
    TargetConfig,
)
from devil.core.snapshot import BaseForkFingerprint, SnapshotSet, TargetFingerprint
from devil.core.types import (
    Actor,
    ActorPolicy,
    ChainId,
    ForkSnapshot,
    RelayDataset,
    RelayMessage,
    RelayMode,
)


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def doch_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "doch"


@pytest.fixture
def actor() -> Actor:
    return Actor("0x00000000000000000000000000000000000000aa")


@pytest.fixture
def actor_policy(actor: Actor) -> ActorPolicy:
    return ActorPolicy("permissionless", "sha256:" + "a" * 64, (actor,))


@pytest.fixture
def relay_dataset() -> RelayDataset:
    message = RelayMessage(
        emitter="0x00000000000000000000000000000000000000e1",
        sequence=1,
        source_chain=ChainId.ETHEREUM,
        destination_chain=ChainId.POLYGON,
        destination_context="polygon.bridge",
        payload="0x12",
        payload_hash="0xpayload",
        attestation="0x34",
        attestation_hash="0xattestation",
        message_id="message-1",
        correlation_value="key-1",
        source_block_number=100,
        source_block_hash="0xsource",
        source_event_hash="0xevent",
        emitted_timestamp=1_000,
        destination_status="unknown",
    )
    return RelayDataset(
        "1.0.0",
        "sha256:" + "d" * 64,
        "generic",
        {ChainId.ETHEREUM: (100, 100)},
        (message,),
        provenance="fixture",
        provenance_hash="sha256:" + "e" * 64,
    )


@pytest.fixture
def relay_config() -> RelayConfig:
    return RelayConfig(
        dataset="relay.json",
        mode=RelayMode.HISTORICAL_AUTHENTIC,
        protocol_adapter="generic",
        delay_model="bounded",
        dataset_hash="sha256:" + "d" * 64,
        adapter_config="relay.toml",
        adapter_config_hash="sha256:" + "c" * 64,
        ordering="fifo_per_emitter",
        duplicate_delivery="reject",
        reorg_assumption="no_reorg_after_finality",
        delivery_deadline=ConfigDeadline(100, "seconds", "polygon"),
        finality_blocks={"ethereum": 2},
        min_delay_seconds={"polygon": 5},
        max_delay_seconds={"polygon": 20},
    )


@pytest.fixture
def snapshot_set() -> SnapshotSet:
    eth_target = TargetFingerprint(
        "ethereum.bridge",
        "0x0000000000000000000000000000000000000011",
        "0xcode1",
        artifact_hash="sha256:" + "1" * 64,
    )
    poly_target = TargetFingerprint(
        "polygon.bridge",
        "0x0000000000000000000000000000000000000022",
        "0xcode2",
        artifact_hash="sha256:" + "2" * 64,
    )
    fingerprints = {
        ChainId.ETHEREUM: BaseForkFingerprint(
            ChainId.ETHEREUM,
            1,
            100,
            "0xblock1",
            "0xroot1",
            1_000,
            {"ethereum.bridge": eth_target},
            "sha256:" + "3" * 64,
        ),
        ChainId.POLYGON: BaseForkFingerprint(
            ChainId.POLYGON,
            137,
            200,
            "0xblock2",
            "0xroot2",
            1_005,
            {"polygon.bridge": poly_target},
            "sha256:" + "4" * 64,
        ),
    }
    snapshots = {
        chain: ForkSnapshot(
            chain,
            fingerprint.block_number,
            fingerprint.block_hash,
            fingerprint.state_root,
            fingerprint.timestamp,
        )
        for chain, fingerprint in fingerprints.items()
    }
    return SnapshotSet(
        snapshots=snapshots,
        base_fingerprints=fingerprints,
        anchor_timestamp=1_000,
        finality_policy="probabilistic",
        id="snapshot-fixture",
    )


@pytest.fixture
def complete_config(tmp_path: Path) -> CampaignConfig:
    artifact = {"abi": [], "storageLayout": {"storage": []}}
    (tmp_path / "eth.json").write_text(json.dumps(artifact))
    (tmp_path / "poly.json").write_text(json.dumps(artifact))
    (tmp_path / "relay.json").write_text("{}")
    (tmp_path / "relay.toml").write_text("")
    (tmp_path / "actors.toml").write_text("")
    (tmp_path / "invariants").mkdir()
    (tmp_path / "output").mkdir()
    return CampaignConfig(
        invariants="invariants",
        output="output",
        chains={
            "ethereum": ChainConfig("ethereum", 1, "ETH_RPC_URL", 100),
            "polygon": ChainConfig("polygon", 137, "POLY_RPC_URL", 200),
        },
        targets={
            "ethereum.bridge": TargetConfig(
                "ethereum.bridge",
                "ethereum",
                "0x0000000000000000000000000000000000000011",
                "eth.json",
                role="source",
                expected_code_hash="0xcode1",
            ),
            "polygon.bridge": TargetConfig(
                "polygon.bridge",
                "polygon",
                "0x0000000000000000000000000000000000000022",
                "poly.json",
                role="destination",
                expected_code_hash="0xcode2",
            ),
        },
        correlations={
            "bridge_message": CorrelationConfig(
                "bridge_message",
                "ethereum.bridge",
                "Locked(bytes32,uint256)",
                ("messageHash",),
                "polygon.bridge",
                "Minted(bytes32,uint256)",
                ("messageHash",),
                "identity@1",
            )
        },
        relay=RelayConfig(
            "relay.json",
            RelayMode.HISTORICAL_AUTHENTIC,
            "generic",
            "none",
            "sha256:" + "d" * 64,
            "relay.toml",
            "sha256:" + "c" * 64,
            "fifo_per_emitter",
            "reject",
            "no_reorg_after_finality",
        ),
        snapshot=SnapshotConfig(max_timestamp_delta=10),
        actors=ActorsConfig("actors.toml", "sha256:" + "a" * 64),
        source_path=tmp_path / "astarots.toml",
    )
