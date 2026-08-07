from __future__ import annotations

import json
from pathlib import Path

from devil.cli import build_parser, main


def test_cli_exposes_documented_command_surface() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for command in ("chain", "probe", "replay", "validate", "list-tools", "init"):
        assert command in help_text
    args = parser.parse_args(
        [
            "probe",
            "--config",
            "campaign.toml",
            "--target",
            "ethereum.bridge=0x0000000000000000000000000000000000000011",
            "--relay-mode",
            "historical-authentic",
            "--max-depth",
            "5",
            "--tool",
            "echidna",
            "--dry-run",
        ]
    )
    assert args.max_depth == 5
    assert args.tools == ["echidna"]


def test_chain_add_preserves_toml_structure(tmp_path: Path, capsys) -> None:
    config = tmp_path / "astarots.toml"
    assert (
        main(
            [
                "chain",
                "add",
                "ethereum",
                "--rpc-env",
                "ETH_RPC_URL",
                "--chain-id",
                "1",
                "--fork-block",
                "100",
                "--expected-block-hash",
                "0xblock",
                "--expected-state-root",
                "0xroot",
                "--config",
                str(config),
            ]
        )
        == 0
    )
    rendered = config.read_text()
    assert "[chains.ethereum]" in rendered
    assert 'rpc_env = "ETH_RPC_URL"' in rendered
    assert "fork_block = 100" in rendered
    assert json.loads(capsys.readouterr().out)["chain"] == "ethereum"


def test_init_creates_non_overwriting_cross_chain_templates(tmp_path: Path, capsys) -> None:
    config = tmp_path / "astarots.toml"
    invariant = tmp_path / "test" / "invariants" / "Invariant.t.sol"
    assert (
        main(
            [
                "init",
                "--template",
                "lock-mint",
                "--targets",
                "2",
                "--config",
                str(config),
                "--invariant",
                str(invariant),
            ]
        )
        == 0
    )
    assert "[chains.chain1]" in config.read_text()
    source = invariant.read_text()
    assert "@crosschain contexts=ethereum.bridge,polygon.bridge" in source
    assert "@correlation bridge_message" in source
    assert "assert(totalLocked() == totalMinted())" in source
    capsys.readouterr()
    assert main(["init", "--config", str(config), "--invariant", str(invariant)]) == 2
    assert "refuses to overwrite" in capsys.readouterr().err


def test_list_tools_reports_availability_as_json(capsys) -> None:
    assert main(["list-tools", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in rows} == {
        "anvil",
        "forge",
        "echidna",
        "halmos",
        "slither",
    }
    assert all("version" in row and "path" in row for row in rows)
