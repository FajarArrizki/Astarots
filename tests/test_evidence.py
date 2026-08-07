from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from devil.core.runtime import foundry_environment
from devil.core.types import (
    Call,
    ChainId,
    EdgeCase,
    Evidence,
    EvidenceStrength,
    Impact,
    Outcome,
    SearchResult,
    Verdict,
    ViolationSource,
    WitnessState,
)
from devil.evidence import (
    EvidenceReport,
    ReplayRunner,
    ViolationCheck,
    build_replay_pair,
    validate_evidence_metadata,
)


def _witness(snapshot_set, actor_policy) -> WitnessState:
    actor = actor_policy.actors[0]
    from devil.core.types import GlobalState

    state = GlobalState(
        chain_snapshots=snapshot_set.snapshots,
        snapshot_set_id=snapshot_set.id,
        actor_policy=actor_policy,
        relay_dataset_hash="sha256:" + "d" * 64,
        relay_policy_hash="sha256:" + "c" * 64,
        observed_values={"locked": 1, "minted": 2},
    )
    call = Call(
        "submit()",
        chain=ChainId.ETHEREUM,
        context_id="ethereum.bridge",
        calldata="0x1234",
        actor=actor,
    )
    return WitnessState(
        state,
        correlation_value="key-1",
        chain=ChainId.ETHEREUM,
        branch_id="branch-1",
        call_sequence=(call,),
        evidence=(
            Evidence(
                "canonical_executor",
                Outcome.SUCCESS,
                "receipt",
                "sha256:" + "7" * 64,
            ),
        ),
    )


def _metadata(snapshot_set) -> dict[str, object]:
    return {
        "run_id": "run-test",
        "project": {
            "revision": "abc123",
            "effective_config_hash": "sha256:" + "1" * 64,
            "invariant_ir_hash": "sha256:" + "2" * 64,
        },
        "tools": {"canonical_executor": "anvil-v1"},
        "runtime": {"astarots": "1.0.0", "python": "3.12"},
        "snapshot_set": {
            "id": snapshot_set.id,
            "coherence_checks_hash": snapshot_set.coherence_checks_hash,
            "chains": {
                chain.value: {
                    "chain_id": fingerprint.chain_id,
                    "fork_block": fingerprint.block_number,
                    "block_hash": fingerprint.block_hash,
                    "state_root": fingerprint.state_root,
                    "targets": {
                        context: {
                            "address": target.address,
                            "code_hash": target.runtime_code_hash,
                        }
                        for context, target in fingerprint.targets.items()
                    },
                }
                for chain, fingerprint in snapshot_set.base_fingerprints.items()
            },
        },
        "relay": {
            "mode": "historical-authentic",
            "dataset_hash": "sha256:" + "d" * 64,
            "policy_hash": "sha256:" + "c" * 64,
            "adapter_config_hash": "sha256:" + "b" * 64,
            "message_ids": [],
        },
        "actor_policy_hash": "sha256:" + "a" * 64,
        "execution": {
            "action_trace_hash": "sha256:" + "8" * 64,
            "environment_hash": "sha256:" + "9" * 64,
        },
        "search": {
            "global_depth": 1,
            "budget_used": 1,
            "budget_total": 10,
            "incomplete_outcomes": [],
            "unsupported_paths": 0,
        },
    }


def test_evidence_report_requires_reproduction_metadata(snapshot_set, actor_policy) -> None:
    witness = _witness(snapshot_set, actor_policy)
    edge = EdgeCase(
        depth=1,
        witness=witness,
        segment_strengths={
            "ethereum": EvidenceStrength.REPLAYED,
            "polygon": EvidenceStrength.OBSERVED,
        },
        aggregate_strength=EvidenceStrength.OBSERVED,
        violated_clauses=("locked == minted",),
        violation_source=ViolationSource.INTRODUCED_BY_TRACE,
        impact=Impact(
            "CRITICAL",
            "destination accounting diverges",
            (ChainId.ETHEREUM, ChainId.POLYGON),
        ),
        chains=(ChainId.ETHEREUM, ChainId.POLYGON),
    )
    result = SearchResult(
        witnesses=(witness,),
        edges=(edge,),
        deepest_edge=edge,
        outcome=Verdict.VIOLATED,
        budget_used=1,
        budget_total=10,
    )
    metadata = _metadata(snapshot_set)
    report = EvidenceReport("locked-equals-minted", result, metadata).to_dict()
    finding = report["findings"][0]
    assert finding["aggregate_strength"] == "observed"
    assert finding["aggregation_rule"] == "weakest-full-trace-segment"
    assert finding["violated_clauses"] == ["locked == minted"]
    assert finding["actor_policy_hash"] == "sha256:" + "a" * 64
    assert finding["snapshot_set"]["id"] == snapshot_set.id

    incomplete = dict(metadata)
    incomplete.pop("relay")
    assert validate_evidence_metadata(incomplete)
    with pytest.raises(ValueError, match="incomplete evidence metadata"):
        EvidenceReport("locked-equals-minted", result, incomplete)


def test_replay_pair_is_self_contained_redacted_and_compilable(
    tmp_path: Path, snapshot_set, actor_policy
) -> None:
    witness = _witness(snapshot_set, actor_policy)
    fingerprints = {
        chain.value: fingerprint for chain, fingerprint in snapshot_set.base_fingerprints.items()
    }
    pair = build_replay_pair(
        "finding-1",
        witness,
        base_fingerprints=fingerprints,
        rpc_env={"ethereum": "ETH_RPC_URL", "polygon": "POLY_RPC_URL"},
        relay={
            "dataset_hash": "sha256:" + "d" * 64,
            "policy_hash": "sha256:" + "c" * 64,
        },
        actor_policy_hash=actor_policy.policy_hash,
        violation_check=ViolationCheck("polygon.bridge", "0x1234"),
        replacement_targets={"polygon.bridge": "0x0000000000000000000000000000000000000033"},
        replacement_code_hashes={"polygon.bridge": "sha256:" + "f" * 64},
    )
    for artifact in pair:
        source_path, metadata_path = artifact.write(tmp_path)
        assert source_path.exists() and metadata_path.exists()
        assert "createSelectFork" in artifact.source
        assert "ETH_RPC_URL" in artifact.source
        assert "https://" not in metadata_path.read_text()
        payload = json.loads(metadata_path.read_text())
        assert payload["source_hash"].startswith("sha256:")

    if shutil.which("forge"):
        forge_environment = foundry_environment()
        health = subprocess.run(
            ["forge", "--version"],
            capture_output=True,
            text=True,
            check=False,
            env=forge_environment,
        )
        if health.returncode != 0:
            pytest.skip("installed Forge is not runnable on this host")
        (tmp_path / "foundry.toml").write_text('[profile.default]\ntest = "."\nsrc = "src"\n')
        (tmp_path / "src").mkdir()
        completed = subprocess.run(
            [
                "forge",
                "test",
                "--root",
                str(tmp_path),
                "--no-match-test",
                "test_replay",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=forge_environment,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


class ReplayRpc:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def call(self, method, params):
        if method == "eth_getBlockByNumber":
            return {
                "hash": self.fingerprint.block_hash,
                "stateRoot": self.fingerprint.state_root,
            }
        if method == "eth_getCode":
            return "0x6000"
        if method == "web3_sha3":
            return next(iter(self.fingerprint.targets.values())).runtime_code_hash
        raise AssertionError(method)


def test_replay_runner_checks_fingerprints_before_forge(
    tmp_path: Path, snapshot_set, actor_policy
) -> None:
    witness = _witness(snapshot_set, actor_policy)
    fingerprints = {
        chain.value: fingerprint for chain, fingerprint in snapshot_set.base_fingerprints.items()
    }
    artifact, _ = build_replay_pair(
        "finding-2",
        witness,
        base_fingerprints=fingerprints,
        rpc_env={"ethereum": "ETH_RPC_URL", "polygon": "POLY_RPC_URL"},
        relay={
            "dataset_hash": "sha256:" + "d" * 64,
            "policy_hash": "sha256:" + "c" * 64,
        },
        actor_policy_hash=actor_policy.policy_hash,
        violation_check=ViolationCheck("polygon.bridge", "0x1234"),
        replacement_targets={"polygon.bridge": "0x0000000000000000000000000000000000000033"},
        replacement_code_hashes={"polygon.bridge": "sha256:" + "f" * 64},
    )
    forge = tmp_path / "forge"
    forge.write_text("#!/bin/sh\nexit 0\n")
    forge.chmod(0o755)
    clients = {chain: ReplayRpc(fingerprint) for chain, fingerprint in fingerprints.items()}
    completed = ReplayRunner(str(forge)).run(
        artifact,
        tmp_path / "run",
        clients=clients,
    )
    assert completed.returncode == 0
