from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from devil.core.config import load_campaign
from devil.core.loaders import content_hash, load_actor_policy, load_relay_dataset
from devil.core.snapshot import SnapshotError, verify_campaign_snapshots
from devil.core.types import ChainId, ForkSnapshot, GlobalState, SlotChange
from devil.invariant.expression import PredicateError, evaluate_predicate, parse_expression


class FakeRpc:
    def __init__(self, chain_id: int, block: int, block_hash: str, root: str, timestamp: int):
        self.chain_id = chain_id
        self.block = block
        self.block_hash = block_hash
        self.root = root
        self.timestamp = timestamp

    def call(self, method: str, params: list[object]) -> object:
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            number = self.block if params[0] == "finalized" else int(str(params[0]), 16)
            return {
                "number": hex(max(number, self.block)),
                "hash": self.block_hash,
                "stateRoot": self.root,
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getCode":
            return "0x6000"
        if method == "web3_sha3":
            return "0xcode1" if self.chain_id == 1 else "0xcode2"
        raise AssertionError(method)


def test_global_state_is_defensively_and_deeply_immutable() -> None:
    snapshots = {ChainId.ETHEREUM: ForkSnapshot(ChainId.ETHEREUM, 100)}
    state = GlobalState(chain_snapshots=snapshots, observed_values={"nested": {"value": 1}})
    snapshots[ChainId.POLYGON] = ForkSnapshot(ChainId.POLYGON, 200)
    assert ChainId.POLYGON not in state.chain_snapshots
    assert isinstance(state.chain_snapshots, MappingProxyType)
    with pytest.raises(TypeError):
        state.chain_snapshots[ChainId.POLYGON] = ForkSnapshot(ChainId.POLYGON, 200)  # type: ignore[index]


def test_snapshot_overlay_preserves_base_and_changes_identity(snapshot_set) -> None:
    original = snapshot_set.snapshot(ChainId.ETHEREUM)
    changed = replace(
        original,
        state_diff=(SlotChange("0x1", "0x0", "0x00", "0x01"),),
        overlay_id=1,
    )
    derived = snapshot_set.with_snapshot(changed)
    assert snapshot_set.snapshot(ChainId.ETHEREUM).state_diff == ()
    assert derived.snapshot(ChainId.ETHEREUM).state_diff == changed.state_diff
    assert derived.id != snapshot_set.id


def test_typed_predicate_requires_declared_variables() -> None:
    expression = parse_expression(
        "locked == minted and total >= 0",
        {"locked": "uint256", "minted": "uint256", "total": "uint256"},
    )
    assert evaluate_predicate(expression, {"locked": 4, "minted": 4, "total": 1})
    with pytest.raises(PredicateError, match="unknown predicate variable"):
        parse_expression("undeclared == 1", {})
    with pytest.raises(PredicateError, match="unsupported"):
        parse_expression("danger()", {})


def test_fork_verifier_checks_chain_block_state_code_and_coherence(complete_config) -> None:
    config = replace(
        complete_config,
        chains={
            "ethereum": replace(
                complete_config.chains["ethereum"],
                expected_block_hash="0xblock1",
                expected_state_root="0xroot1",
            ),
            "polygon": replace(
                complete_config.chains["polygon"],
                expected_block_hash="0xblock2",
                expected_state_root="0xroot2",
            ),
        },
    )
    clients = {
        "ethereum": FakeRpc(1, 100, "0xblock1", "0xroot1", 1_000),
        "polygon": FakeRpc(137, 200, "0xblock2", "0xroot2", 1_005),
    }
    snapshots = verify_campaign_snapshots(config, clients=clients)
    assert snapshots.snapshot(ChainId.ETHEREUM).base_block_hash == "0xblock1"
    assert snapshots.coherence_checks_hash.startswith("sha256:")

    bad = replace(
        config,
        chains={
            **dict(config.chains),
            "polygon": replace(config.chains["polygon"], expected_state_root="0xwrong"),
        },
    )
    with pytest.raises(SnapshotError, match="state root mismatch"):
        verify_campaign_snapshots(bad, clients=clients)


def test_content_addressed_dataset_and_actor_loaders(tmp_path: Path) -> None:
    relay_path = tmp_path / "relay.json"
    relay_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "protocol": "generic",
                "source_block_ranges": {"ethereum": [100, 100]},
                "messages": [
                    {
                        "emitter": "0xe1",
                        "sequence": 1,
                        "source_chain": "ethereum",
                        "destination_chain": "polygon",
                        "destination_context": "polygon.bridge",
                        "message_id": "m1",
                    }
                ],
            }
        )
    )
    dataset = load_relay_dataset(relay_path, content_hash(relay_path))
    assert dataset.messages[0].identity == "m1"
    relay_path.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_relay_dataset(relay_path, dataset.dataset_hash)

    actors_path = tmp_path / "actors.toml"
    actors_path.write_text(
        'id = "permissionless"\n[[actors]]\n'
        'address = "0x00000000000000000000000000000000000000aa"\n'
        'role = "attacker"\nimpersonation_allowed = true\n'
    )
    policy = load_actor_policy(actors_path, content_hash(actors_path))
    assert policy.actors[0].impersonation_allowed


def test_config_loader_reads_all_sections_and_rejects_unknown(tmp_path: Path) -> None:
    config_path = tmp_path / "astarots.toml"
    config_path.write_text(
        """
[default]
invariants = "invariants"
output = "out"
tools = ["echidna", "halmos", "slither"]
max_depth = 5
branching_caps = [4, 2, 1]
max_consecutive_expansions_per_chain = 2
max_states = 77
timeout = 900
[chains.ethereum]
rpc_env = "ETH_RPC_URL"
chain_id = 1
fork_block = 100
[chains.polygon]
rpc_env = "POLY_RPC_URL"
chain_id = 137
fork_block = 200
[targets."ethereum.bridge"]
address = "0x0000000000000000000000000000000000000011"
artifact = "eth.json"
expected_code_hash = "0xcode1"
[targets."polygon.bridge"]
address = "0x0000000000000000000000000000000000000022"
artifact = "poly.json"
expected_code_hash = "0xcode2"
[correlations.bridge_message]
source_context = "ethereum.bridge"
source_event = "Locked(bytes32,uint256)"
source_fields = ["messageHash"]
destination_context = "polygon.bridge"
destination_event = "Minted(bytes32,uint256)"
destination_fields = ["messageHash"]
normalize = "identity@1"
[relay]
dataset = "relay.json"
mode = "historical-authentic"
protocol_adapter = "generic"
delay_model = "bounded"
dataset_hash = "sha256:dataset"
adapter_config = "relay.toml"
adapter_config_hash = "sha256:adapter"
ordering = "fifo_per_emitter"
duplicate_delivery = "reject"
reorg_assumption = "no_reorg_after_finality"
finality_blocks = { ethereum = 12 }
min_delay_seconds = { polygon = 5 }
max_delay_seconds = { polygon = 20 }
[relay.delivery_deadline]
value = 128
unit = "blocks"
chain_id = "polygon"
[snapshot]
max_timestamp_delta = 12
require_finalized = true
protocol_epochs = { guardian_epoch = "4" }
message_cutoffs = { "0xe1" = 7 }
[actors]
policy = "actors.toml"
policy_hash = "sha256:actors"
[tools.echidna]
test_limit = 123
[tools.halmos]
loop_bound = 4
[tools.slither]
exclude = ["naming-convention"]
"""
    )
    config = load_campaign(config_path)
    assert config.output == "out"
    assert config.max_consecutive_expansions_per_chain == 2
    assert config.relay is not None and config.relay.delivery_deadline.value == 128
    assert config.snapshot.protocol_epochs["guardian_epoch"] == "4"
    assert config.tool_config["echidna"].options["test_limit"] == 123

    config_path.write_text(config_path.read_text() + "\nunknown = 1\n")
    with pytest.raises(ValueError, match="unknown tools.slither keys|unknown root keys"):
        load_campaign(config_path)
