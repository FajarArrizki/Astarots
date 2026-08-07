"""Discovery and command planning for the external Wormhole fork-test tree."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WormholeForkTarget:
    """A Solidity fork test selected from the pinned local Wormhole checkout."""

    project_root: Path
    test_path: Path
    rpc_alias: str
    fork_block: int | None = None

    def forge_command(self) -> tuple[str, ...]:
        command = (
            "forge",
            "test",
            "--root",
            str(self.project_root),
            "--match-path",
            str(self.test_path),
        )
        if self.fork_block is not None:
            command += ("--fork-block-number", str(self.fork_block))
        return command


def discover_wormhole_targets(project_root: str | Path) -> tuple[WormholeForkTarget, ...]:
    """Discover tests without mutating or compiling the external checkout."""
    root = Path(project_root).resolve()
    config_path = root / "foundry.toml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Wormhole fork-test config not found: {config_path}")
    config = tomllib.loads(config_path.read_text())
    rpc_endpoints = config.get("rpc_endpoints", {})
    if not isinstance(rpc_endpoints, Mapping) or not rpc_endpoints:
        raise ValueError("Wormhole foundry.toml must declare [rpc_endpoints]")
    rpc_alias = sorted(str(alias) for alias in rpc_endpoints)[0]
    profile = config.get("profile", {}).get("default", {})
    test_dir = root / str(profile.get("test", "test"))
    paths = sorted(path.relative_to(root) for path in test_dir.rglob("*.t.sol"))
    return tuple(WormholeForkTarget(root, path, rpc_alias) for path in paths)
