"""Tests for deterministic core contracts."""

from pathlib import Path

import pytest

from devil.core import (
    ChainId,
    ForkSnapshot,
    RelayDataset,
    RelayLedger,
    RelayMessage,
    RelayMode,
    SlotChange,
    SnapshotSet,
    apply_slot_changes,
    load_campaign,
)
from devil.invariant.expression import PredicateError, evaluate_predicate
from devil.invariant.ir import load_invariant


def test_campaign_config_keeps_rpc_secret_out_of_configuration(tmp_path: Path) -> None:
    config_file = tmp_path / "astarots.toml"
    config_file.write_text(
        """
[default]
tools = ["slither"]
max_depth = 4

[chains.ethereum]
rpc_env = "ETH_RPC_URL"
chain_id = 1
fork_block = 18500000

[targets."ethereum.bridge"]
address = "0x1111111111111111111111111111111111111111"
artifact = "out/Bridge.json"
"""
    )
    config = load_campaign(config_file)
    assert config.chains["ethereum"].rpc_env == "ETH_RPC_URL"
    assert "https://" not in repr(config)
    assert config.validate() == []


def test_predicate_engine_evaluates_only_supported_pure_operations() -> None:
    assert evaluate_predicate(
        "locked == minted and total >= 0", {"locked": 4, "minted": 4, "total": 1}
    )
    with pytest.raises(PredicateError, match="unknown predicate variable"):
        evaluate_predicate("missing == 1", {})
    with pytest.raises(PredicateError, match="unsupported predicate syntax"):
        evaluate_predicate("danger()", {})


def test_historical_relay_requires_exact_provenance_and_is_idempotent() -> None:
    message = RelayMessage(
        emitter="0xemitter",
        sequence=7,
        message_id="msg-7",
        vaa_hash="vaa-hash",
        source_event_hash="event-hash",
        attestation_hash="attestation-hash",
    )
    ledger = RelayLedger.from_dataset(
        RelayDataset(source_chain=ChainId.ETHEREUM, messages=(message,)),
        RelayMode.HISTORICAL_AUTHENTIC,
    )
    delivered, result = ledger.deliver("msg-7")
    assert result.applied is True
    assert delivered.quiescent()
    repeated, second = delivered.deliver("msg-7")
    assert repeated == delivered
    assert second.reason == "already_delivered"


def test_snapshot_overlay_is_branch_local() -> None:
    base = ForkSnapshot(chain_id=ChainId.ETHEREUM, base_block=10)
    changed = apply_slot_changes(
        base,
        (SlotChange(contract="0xcontract", slot="0x01", old_value="0x00", new_value="0x02"),),
    )
    assert base.state_diff == ()
    assert changed.overlay_id == 1
    snapshot_set = SnapshotSet({ChainId.ETHEREUM: base})
    assert snapshot_set.snapshot(ChainId.ETHEREUM) == base


def test_loader_extracts_cross_chain_natspec(tmp_path: Path) -> None:
    invariant_file = tmp_path / "BridgeInvariants.t.sol"
    invariant_file.write_text(
        """
/// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
/// @transition ethereum.bridge:locked increase=[deposit(uint256)] decrease=[burn(uint256)]
/// @transition polygon.bridge:minted increase=[mint(bytes)] decrease=[withdraw(bytes)]
/// @observation AFTER_ALL_DELIVERED quiescence=NO_ELIGIBLE_MESSAGES
/// @correlation bridge_message
/// @bind locked=ethereum.bridge.totalLocked() minted=polygon.bridge.totalMinted()
/// @quantify FORALL locked,minted: locked == minted
/// @assume message_ordering: ordered_by_sequence
/// @observe touched,relay max=64
function invariant_locked_equals_minted() public {}
"""
    )
    invariant = load_invariant(str(invariant_file))
    assert invariant.id == "invariant_locked_equals_minted"
    assert len(invariant.transition_predicates) == 2
    assert invariant.property.predicate == "locked == minted"
    assert invariant.observation_set is not None
    assert invariant.observation_set.max_items == 64
