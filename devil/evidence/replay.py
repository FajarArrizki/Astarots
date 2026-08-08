"""Executable twin-state Foundry replay generation and verification."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devil.core.runtime import foundry_environment
from devil.core.snapshot import BaseForkFingerprint, RpcClient, keccak_hex
from devil.core.types import Call, EnvironmentTransition, RelayTransition, WitnessState

_SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]+|https?://[^\s/@:]+:[^\s/@]+@)")


@dataclass(frozen=True)
class ViolationCheck:
    context_id: str
    calldata: str
    expected_violation: bool = True


@dataclass(frozen=True)
class ReplayArtifact:
    finding_id: str
    mode: str
    contract_name: str
    source: str
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "finding_id": self.finding_id,
            "mode": self.mode,
            "contract_name": self.contract_name,
            "source_hash": _digest(self.source),
            "metadata": _redact(dict(self.metadata)),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def write(self, directory: str | Path) -> tuple[Path, Path]:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        source_path = destination / f"{self.contract_name}.t.sol"
        metadata_path = destination / f"{self.contract_name}.json"
        source_path.write_text(self.source)
        metadata_path.write_text(json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n")
        return source_path, metadata_path


def build_replay_artifact(
    finding_id: str,
    mode: str,
    witness: WitnessState,
    *,
    base_fingerprints: Mapping[str, BaseForkFingerprint],
    rpc_env: Mapping[str, str],
    relay: Mapping[str, Any],
    actor_policy_hash: str,
    violation_check: ViolationCheck,
    replacement_targets: Mapping[str, str] | None = None,
    replacement_code_hashes: Mapping[str, str] | None = None,
    expected_failure_step: int | None = None,
) -> ReplayArtifact:
    if mode not in {"vulnerable", "fixed-regression"}:
        raise ValueError("replay mode must be vulnerable or fixed-regression")
    if not witness.call_sequence and mode != "vulnerable":
        raise ValueError("fixed regression requires a non-empty recorded trace")
    if mode == "fixed-regression" and not replacement_targets:
        raise ValueError("fixed regression requires an explicit replacement target")
    for chain in base_fingerprints:
        if chain not in rpc_env:
            raise ValueError(f"missing RPC environment reference for {chain}")
    if not actor_policy_hash or not relay.get("dataset_hash") or not relay.get("policy_hash"):
        raise ValueError("replay requires actor and relay policy fingerprints")
    replacement_targets = dict(replacement_targets or {})
    replacement_code_hashes = dict(replacement_code_hashes or {})
    contract_name = (
        "VulnerableReproducer_" if mode == "vulnerable" else "FixedRegression_"
    ) + _solidity_name(finding_id)
    metadata = {
        "base_fingerprints": {
            chain: _fingerprint_dict(fingerprint)
            for chain, fingerprint in sorted(base_fingerprints.items())
        },
        "rpc_env": dict(sorted(rpc_env.items())),
        "relay": dict(relay),
        "actor_policy_hash": actor_policy_hash,
        "action_trace_hash": _digest([_step_dict(step) for step in witness.call_sequence]),
        "replacement_targets": replacement_targets,
        "replacement_code_hashes": replacement_code_hashes,
        "violation_check": vars(violation_check),
        "expected_failure_step": expected_failure_step,
    }
    source = _render_contract(
        contract_name,
        mode,
        witness,
        base_fingerprints,
        rpc_env,
        relay,
        actor_policy_hash,
        violation_check,
        replacement_targets,
        replacement_code_hashes,
        expected_failure_step,
    )
    return ReplayArtifact(finding_id, mode, contract_name, source, metadata)


def build_replay_pair(
    finding_id: str,
    witness: WitnessState,
    *,
    base_fingerprints: Mapping[str, BaseForkFingerprint],
    rpc_env: Mapping[str, str],
    relay: Mapping[str, Any],
    actor_policy_hash: str,
    violation_check: ViolationCheck,
    replacement_targets: Mapping[str, str],
    replacement_code_hashes: Mapping[str, str],
    expected_failure_step: int | None = None,
) -> tuple[ReplayArtifact, ReplayArtifact]:
    vulnerable = build_replay_artifact(
        finding_id,
        "vulnerable",
        witness,
        base_fingerprints=base_fingerprints,
        rpc_env=rpc_env,
        relay=relay,
        actor_policy_hash=actor_policy_hash,
        violation_check=violation_check,
    )
    fixed = build_replay_artifact(
        finding_id,
        "fixed-regression",
        witness,
        base_fingerprints=base_fingerprints,
        rpc_env=rpc_env,
        relay=relay,
        actor_policy_hash=actor_policy_hash,
        violation_check=violation_check,
        replacement_targets=replacement_targets,
        replacement_code_hashes=replacement_code_hashes,
        expected_failure_step=expected_failure_step,
    )
    return vulnerable, fixed


class ReplayRunner:
    """Verify recorded bases, then compile and execute a generated Foundry replay."""

    def __init__(self, forge_binary: str = "forge", *, timeout: int = 600) -> None:
        self.forge_binary = forge_binary
        self.timeout = timeout

    def verify_fingerprints(
        self, artifact: ReplayArtifact, clients: Mapping[str, RpcClient]
    ) -> None:
        fingerprints = artifact.metadata["base_fingerprints"]
        for chain, fingerprint in fingerprints.items():
            client = clients.get(chain)
            if client is None:
                raise ValueError(f"missing replay RPC client for {chain}")
            block = client.call("eth_getBlockByNumber", [hex(fingerprint["block_number"]), False])
            if str(block.get("hash", "")).lower() != str(fingerprint["block_hash"]).lower():
                raise ValueError(f"{chain}: replay block hash mismatch")
            if str(block.get("stateRoot", "")).lower() != str(fingerprint["state_root"]).lower():
                raise ValueError(f"{chain}: replay state root mismatch")
            for target in fingerprint["targets"].values():
                code = client.call(
                    "eth_getCode", [target["address"], hex(fingerprint["block_number"])]
                )
                code_hash = keccak_hex(client, code)
                if str(code_hash).lower() != str(target["runtime_code_hash"]).lower():
                    raise ValueError(f"{chain}: replay target code hash mismatch")

    def run(
        self,
        artifact: ReplayArtifact,
        directory: str | Path,
        *,
        clients: Mapping[str, RpcClient],
    ) -> subprocess.CompletedProcess[str]:
        self.verify_fingerprints(artifact, clients)
        source_path, _ = artifact.write(directory)
        root = Path(directory)
        (root / "foundry.toml").write_text('[profile.default]\ntest = "."\nsrc = "src"\n')
        (root / "src").mkdir(exist_ok=True)
        completed = subprocess.run(
            [
                self.forge_binary,
                "test",
                "--root",
                str(Path(directory)),
                "--match-path",
                source_path.name,
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=foundry_environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"replay {artifact.contract_name} failed: "
                f"{_redact((completed.stdout or '') + (completed.stderr or ''))}"
            )
        return completed


def _render_contract(
    contract_name: str,
    mode: str,
    witness: WitnessState,
    fingerprints: Mapping[str, BaseForkFingerprint],
    rpc_env: Mapping[str, str],
    relay: Mapping[str, Any],
    actor_policy_hash: str,
    violation_check: ViolationCheck,
    replacements: Mapping[str, str],
    replacement_hashes: Mapping[str, str],
    expected_failure_step: int | None,
) -> str:
    fork_variables = {chain: f"fork_{_solidity_name(chain)}" for chain in fingerprints}
    declarations = "\n    ".join(f"uint256 internal {name};" for name in fork_variables.values())
    setup_lines: list[str] = []
    target_addresses: dict[str, str] = {}
    for chain, fingerprint in sorted(fingerprints.items()):
        variable = fork_variables[chain]
        setup_lines.append(
            f"{variable} = vm.createSelectFork("
            f'vm.envString("{rpc_env[chain]}"), {fingerprint.block_number});'
        )
        setup_lines.append(f'require(block.number == {fingerprint.block_number}, "fork block");')
        for context, target in sorted(fingerprint.targets.items()):
            address = replacements.get(context, target.address)
            target_addresses[context] = address
            expected_hash = replacement_hashes.get(context, target.runtime_code_hash)
            setup_lines.append(
                f"require({_address(address)}.codehash == "
                f'{_bytes32(expected_hash)}, "code hash {context}");'
            )
    trace_lines: list[str] = []
    for index, step in enumerate(witness.call_sequence):
        if isinstance(step, Call):
            if step.chain is None or step.context_id not in target_addresses:
                raise ValueError("replay call has no chain or verified context")
            trace_lines.append(f"vm.selectFork({fork_variables[step.chain.value]});")
            if step.actor:
                trace_lines.append(f"vm.prank({_address(step.actor.address)});")
            target = target_addresses[step.context_id]
            calldata = step.calldata.removeprefix("0x")
            trace_lines.append(
                f"(bool ok{index}, bytes memory data{index}) = "
                f"{_address(target)}.call{{value: {step.value}}}"
                f'(hex"{calldata}");'
            )
            if mode == "fixed-regression" and expected_failure_step == index:
                trace_lines.append(f'require(!ok{index}, "patched call unexpectedly succeeded");')
            else:
                trace_lines.append(f"require(ok{index}, string(data{index}));")
        elif isinstance(step, EnvironmentTransition):
            trace_lines.append(f"vm.selectFork({fork_variables[step.chain.value]});")
            trace_lines.append(f"vm.roll({step.target_block});")
            trace_lines.append(f"vm.warp({step.target_timestamp});")
        elif isinstance(step, RelayTransition):
            trace_lines.append(
                f"_recordRelay({_bytes32(step.message_id)}, {_bytes32(step.policy_ref)});"
            )
    check_target = target_addresses.get(violation_check.context_id)
    if check_target is None:
        raise ValueError("violation check references an unknown context")
    check_chain = violation_check.context_id.partition(".")[0]
    check_data = violation_check.calldata.removeprefix("0x")
    expected = "true" if mode == "vulnerable" and violation_check.expected_violation else "false"
    setup = "\n        ".join(setup_lines)
    trace = "\n        ".join(trace_lines)
    relay_dataset = _bytes32(str(relay["dataset_hash"]))
    actor_hash = _bytes32(actor_policy_hash)
    return f'''// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.20;

interface Vm {{
    function createSelectFork(
        string calldata rpcUrl,
        uint256 blockNumber
    ) external returns (uint256);
    function selectFork(uint256 forkId) external;
    function envString(string calldata name) external returns (string memory);
    function prank(address sender) external;
    function roll(uint256 blockNumber) external;
    function warp(uint256 timestamp) external;
}}

contract {contract_name} {{
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    {declarations}
    bytes32 internal constant RELAY_DATASET_HASH = {relay_dataset};
    bytes32 internal constant ACTOR_POLICY_HASH = {actor_hash};

    function setUp() public {{
        {setup}
        require(RELAY_DATASET_HASH != bytes32(0), "relay dataset hash");
        require(ACTOR_POLICY_HASH != bytes32(0), "actor policy hash");
    }}

    function test_replay() public {{
        {trace}
        vm.selectFork({fork_variables[check_chain]});
        (bool checkOk, bytes memory checkData) =
            {_address(check_target)}.staticcall(hex"{check_data}");
        require(checkOk && checkData.length >= 32, "violation check failed");
        bool violated = abi.decode(checkData, (bool));
        require(violated == {expected}, "unexpected invariant result");
    }}

    function _recordRelay(bytes32 messageId, bytes32 policyHash) internal pure {{
        require(messageId != bytes32(0) && policyHash != bytes32(0), "relay identity");
    }}
}}
'''


def _fingerprint_dict(fingerprint: BaseForkFingerprint) -> dict[str, Any]:
    return {
        "chain_id": fingerprint.chain_id,
        "block_number": fingerprint.block_number,
        "block_hash": fingerprint.block_hash,
        "state_root": fingerprint.state_root,
        "timestamp": fingerprint.timestamp,
        "digest": fingerprint.digest,
        "targets": {
            context: vars(target) for context, target in sorted(fingerprint.targets.items())
        },
    }


def _step_dict(step: Any) -> dict[str, Any]:
    return {
        "kind": type(step).__name__,
        **{
            key: value.value if hasattr(value, "value") else value
            for key, value in vars(step).items()
        },
    }


def _bytes32(value: str) -> str:
    cleaned = value.removeprefix("sha256:").removeprefix("0x")
    if not re.fullmatch(r"[0-9a-fA-F]{1,64}", cleaned):
        cleaned = hashlib.sha256(value.encode()).hexdigest()
    return "bytes32(0x" + cleaned.rjust(64, "0") + ")"


def _address(value: str) -> str:
    cleaned = value.removeprefix("0x")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", cleaned):
        raise ValueError(f"invalid replay address: {value}")
    return f'address(bytes20(hex"{cleaned}"))'


def _solidity_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return cleaned if cleaned and not cleaned[0].isdigit() else "Replay_" + cleaned


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("<redacted>", value)
    if isinstance(value, Mapping):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value
