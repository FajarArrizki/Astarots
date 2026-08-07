"""Wormhole fork-test integration contracts."""

from devil.integration import discover_wormhole_targets


def test_discover_wormhole_fork_targets(tmp_path) -> None:
    (tmp_path / "foundry.toml").write_text(
        '[profile.default]\ntest = "test"\n[rpc_endpoints]\neth_mainnet = "env"\n'
    )
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "Bridge.t.sol").write_text("contract BridgeTest {}")
    targets = discover_wormhole_targets(tmp_path)
    assert len(targets) == 1
    assert targets[0].rpc_alias == "eth_mainnet"
    assert targets[0].forge_command()[-1] == "test/Bridge.t.sol"
