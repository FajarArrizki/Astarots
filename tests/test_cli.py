"""CLI validation over explicit pinned fork configuration."""

import json

from devil.cli import main

_CONFIG = """
[default]
tools = ["slither"]
max_depth = 3
max_states = 20

[chains.ethereum]
chain_id = 1
rpc_env = "ETH_RPC_URL"
fork_block = 18500000

[targets."ethereum.bridge"]
address = "0x1111111111111111111111111111111111111111"
artifact = "out/Bridge.json"
"""


def test_validate_and_list_forks(tmp_path, capsys) -> None:
    path = tmp_path / "astarots.toml"
    path.write_text(_CONFIG)
    assert main(["validate", "--config", str(path)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["chains"] == ["ethereum"]
    assert validated["max_depth"] == 3

    assert main(["forks", "--config", str(path)]) == 0
    forks = json.loads(capsys.readouterr().out)
    assert forks["ethereum"]["fork_block"] == 18500000
