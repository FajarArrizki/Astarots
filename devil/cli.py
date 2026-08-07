"""Astarots command-line interface for pinned cross-chain campaigns."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import tomlkit

from devil.core.config import CampaignConfig, load_campaign
from devil.core.loaders import content_hash
from devil.core.runtime import foundry_environment
from devil.core.snapshot import JsonRpcClient, verify_campaign_snapshots
from devil.core.types import RelayMode
from devil.evidence import ReplayArtifact, ReplayRunner
from devil.harness.campaign import CampaignRuntime
from devil.invariant.ir import load_invariant


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astarots")
    parser.add_argument("--version", action="version", version="astarots 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    chain = commands.add_parser("chain", help="manage pinned chain configuration")
    chain_commands = chain.add_subparsers(dest="chain_command", required=True)
    add = chain_commands.add_parser("add", help="add or replace one [chains] entry")
    add.add_argument("alias")
    add.add_argument("--rpc-env", required=True)
    add.add_argument("--chain-id", type=int, required=True)
    add.add_argument("--fork-block", type=int, required=True)
    add.add_argument("--expected-block-hash", default="")
    add.add_argument("--expected-state-root", default="")
    add.add_argument("--config", type=Path, default=Path("astarots.toml"))
    rm = chain_commands.add_parser("rm", help="remove a chain entry")
    rm.add_argument("alias")
    rm.add_argument("--config", type=Path, default=Path("astarots.toml"))
    chain_commands.add_parser("list", help="list configured chains").add_argument(
        "--config", type=Path, default=Path("astarots.toml")
    )

    probe = commands.add_parser("probe", help="run a cross-chain campaign")
    probe.add_argument("--invariant", type=Path)
    probe.add_argument("--config", type=Path, default=Path("astarots.toml"))
    probe.add_argument("--target", action="append", default=[])
    probe.add_argument("--artifact", action="append", default=[])
    probe.add_argument("--source", action="append", default=[])
    probe.add_argument("--relay-mode", choices=[mode.value for mode in RelayMode])
    probe.add_argument("--relay-dataset", type=Path)
    probe.add_argument("--relay-config", type=Path)
    probe.add_argument("--actor-policy", type=Path)
    probe.add_argument("--max-depth", type=int)
    probe.add_argument("--max-states", type=int)
    probe.add_argument("--timeout", type=int)
    probe.add_argument("--tool", action="append", dest="tools")
    probe.add_argument("--output", type=Path)
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--dry-run", action="store_true")

    replay = commands.add_parser("replay", help="run generated Foundry replay artifacts")
    replay.add_argument("path", type=Path)
    replay.add_argument("--config", type=Path, default=Path("astarots.toml"))
    replay.add_argument("--replacement", action="append", default=[])
    replay.add_argument("--json", action="store_true")

    validate = commands.add_parser("validate", help="validate config, IR, and fork identities")
    validate.add_argument("--config", type=Path, default=Path("astarots.toml"))
    validate.add_argument("--invariant", type=Path)
    validate.add_argument("--json", action="store_true")

    tools = commands.add_parser("list-tools", help="show tool availability")
    tools.add_argument("--json", action="store_true")

    init = commands.add_parser("init", help="create campaign and invariant templates")
    init.add_argument("--template", choices=("lock-mint", "generic"), default="generic")
    init.add_argument("--targets", type=int, default=2)
    init.add_argument("--config", type=Path, default=Path("astarots.toml"))
    init.add_argument("--invariant", type=Path, default=Path("test/invariants/Invariant.t.sol"))

    forks = commands.add_parser("forks", help="print verified snapshot fingerprints")
    forks.add_argument("--config", type=Path, default=Path("astarots.toml"))
    forks.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "chain": _chain,
        "probe": _probe,
        "replay": _replay,
        "validate": _validate,
        "list-tools": _list_tools,
        "init": _init,
        "forks": _forks,
    }
    try:
        return handlers[args.command](args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


def _chain(args: argparse.Namespace) -> int:
    if args.chain_command == "add":
        document = _toml_document(args.config)
        chains = document.setdefault("chains", tomlkit.table())
        entry = tomlkit.table()
        entry.add("rpc_env", args.rpc_env)
        entry.add("chain_id", args.chain_id)
        entry.add("fork_block", args.fork_block)
        if args.expected_block_hash:
            entry.add("expected_block_hash", args.expected_block_hash)
        if args.expected_state_root:
            entry.add("expected_state_root", args.expected_state_root)
        chains[args.alias] = entry
        args.config.write_text(tomlkit.dumps(document), encoding="utf-8")
        _emit({"chain": args.alias, "config": str(args.config)}, pretty=False)
        return 0
    if args.chain_command == "rm":
        document = _toml_document(args.config)
        chains = document.get("chains", {})
        if args.alias not in chains:
            raise KeyError(f"unknown chain {args.alias!r}")
        del chains[args.alias]
        if not chains:
            document.remove("chains")
        args.config.write_text(tomlkit.dumps(document), encoding="utf-8")
        _emit({"removed": args.alias, "config": str(args.config)}, pretty=False)
        return 0
    config = load_campaign(args.config)
    _emit(
        [
            {
                "alias": alias,
                "chain_id": chain.chain_id,
                "rpc_env": chain.rpc_env,
                "fork_block": chain.fork_block,
                "expected_block_hash": chain.expected_block_hash,
                "expected_state_root": chain.expected_state_root,
            }
            for alias, chain in sorted(config.chains.items())
        ],
        pretty=True,
    )
    return 0


def _probe(args: argparse.Namespace) -> int:
    config = _probe_config(load_campaign(args.config), args)
    runtime = CampaignRuntime(config)
    snapshot_set = runtime.validate()
    plan = {
        "status": "validated",
        "snapshot_set_id": snapshot_set.id,
        "chains": [chain.value for chain in snapshot_set.snapshots],
        "invariants": [invariant.id for invariant in runtime.invariants],
        "tools": list(config.tools),
        "relay_mode": config.relay.mode.value if config.relay else None,
        "max_depth": config.max_depth,
        "max_states": config.max_states,
    }
    if args.dry_run:
        _emit(plan, pretty=not args.json)
        return 0
    results = runtime.run()
    reports = [runtime.report(result, snapshot_set).to_dict() for result in results]
    output = args.output or config.resolve_path(config.output)
    output.mkdir(parents=True, exist_ok=True)
    for report in reports:
        run_id = str(report["run_id"])
        (output / f"{run_id}.json").write_text(
            json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
        )
    _emit(reports, pretty=not args.json)
    return (
        1 if any(item["invariant_results"][0]["verdict"] == "violated" for item in reports) else 0
    )


def _replay(args: argparse.Namespace) -> int:
    config = load_campaign(args.config)
    artifacts = _load_replay_artifacts(args.path)
    replacements = _pairs(args.replacement, "replacement")
    clients = {alias: JsonRpcClient(chain.rpc_url()) for alias, chain in config.chains.items()}
    runner = ReplayRunner()
    rows = []
    for artifact in artifacts:
        declared = artifact.metadata.get("replacement_targets", {})
        if replacements and dict(declared) != replacements:
            raise ValueError("CLI replacements differ from the content-addressed replay artifact")
        with tempfile.TemporaryDirectory(prefix="astarots-replay-") as directory:
            completed = runner.run(artifact, directory, clients=clients)
        rows.append(
            {
                "finding_id": artifact.finding_id,
                "mode": artifact.mode,
                "outcome": "success",
                "returncode": completed.returncode,
            }
        )
    _emit(rows, pretty=not args.json)
    return 0


def _validate(args: argparse.Namespace) -> int:
    config = load_campaign(args.config)
    runtime = CampaignRuntime(config)
    if args.invariant:
        load_invariant(
            args.invariant,
            correlations=runtime.correlations,
            default_tools=config.tools,
            default_timeout=config.timeout,
        )
    snapshot_set = runtime.validate()
    _emit(
        {
            "valid": True,
            "snapshot_set_id": snapshot_set.id,
            "invariants": [item.id for item in runtime.invariants],
        },
        pretty=not args.json,
    )
    return 0


def _list_tools(args: argparse.Namespace) -> int:
    commands = {
        "anvil": ("anvil", "--version"),
        "forge": ("forge", "--version"),
        "echidna": ("echidna", "--version"),
        "halmos": ("halmos", "--version"),
        "slither": ("slither", "--version"),
    }
    rows = []
    for name, command in commands.items():
        binary = shutil.which(command[0])
        tool_version = "unavailable"
        if binary:
            environment = foundry_environment() if name in {"anvil", "forge"} else None
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            output = (completed.stdout or completed.stderr or "").strip().splitlines()
            tool_version = output[0] if output else "unknown"
        rows.append({"name": name, "path": binary, "version": tool_version})
    _emit(rows, pretty=not args.json)
    return 0


def _init(args: argparse.Namespace) -> int:
    if args.config.exists() or args.invariant.exists():
        raise ValueError("init refuses to overwrite an existing config or invariant")
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.invariant.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(_config_template(args.targets), encoding="utf-8")
    args.invariant.write_text(_invariant_template(args.template), encoding="utf-8")
    _emit({"config": str(args.config), "invariant": str(args.invariant)}, pretty=True)
    return 0


def _forks(args: argparse.Namespace) -> int:
    snapshot_set = verify_campaign_snapshots(load_campaign(args.config))
    _emit(
        {
            "snapshot_set_id": snapshot_set.id,
            "coherence_checks_hash": snapshot_set.coherence_checks_hash,
            "chains": {
                chain.value: {
                    "chain_id": fingerprint.chain_id,
                    "block": fingerprint.block_number,
                    "block_hash": fingerprint.block_hash,
                    "state_root": fingerprint.state_root,
                    "fork_cache_hash": fingerprint.fork_cache_hash,
                }
                for chain, fingerprint in snapshot_set.base_fingerprints.items()
            },
        },
        pretty=not args.json,
    )
    return 0


def _probe_config(config: CampaignConfig, args: argparse.Namespace) -> CampaignConfig:
    targets = dict(config.targets)
    for field_name, values in (
        ("address", _pairs(args.target, "target")),
        ("artifact", _pairs(args.artifact, "artifact")),
        ("source", _pairs(args.source, "source")),
    ):
        for context, value in values.items():
            if context not in targets:
                raise ValueError(f"unknown target context {context!r}")
            targets[context] = replace(targets[context], **{field_name: value})
    relay = config.relay
    if relay and args.relay_mode:
        relay = replace(relay, mode=RelayMode(args.relay_mode))
    if relay and args.relay_dataset:
        relay = replace(
            relay,
            dataset=str(args.relay_dataset),
            dataset_hash=content_hash(args.relay_dataset),
        )
    if relay and args.relay_config:
        relay = replace(
            relay,
            adapter_config=str(args.relay_config),
            adapter_config_hash=content_hash(args.relay_config),
        )
    actors = config.actors
    if actors and args.actor_policy:
        actors = replace(
            actors,
            policy=str(args.actor_policy),
            policy_hash=content_hash(args.actor_policy),
        )
    return replace(
        config,
        invariants=str(args.invariant) if args.invariant else config.invariants,
        targets=targets,
        relay=relay,
        actors=actors,
        max_depth=args.max_depth or config.max_depth,
        max_states=args.max_states or config.max_states,
        timeout=args.timeout or config.timeout,
        tools=tuple(args.tools) if args.tools else config.tools,
        output=str(args.output) if args.output else config.output,
    )


def _load_replay_artifacts(path: Path) -> tuple[ReplayArtifact, ...]:
    metadata_paths = tuple(sorted(path.glob("*.json"))) if path.is_dir() else (path,)
    result: list[ReplayArtifact] = []
    for metadata_path in metadata_paths:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or "mode" not in metadata:
            continue
        source_path = metadata_path.with_suffix(".t.sol")
        if not source_path.exists():
            raise ValueError(f"missing replay source {source_path}")
        result.append(
            ReplayArtifact(
                str(metadata["finding_id"]),
                str(metadata["mode"]),
                str(metadata["contract_name"]),
                source_path.read_text(encoding="utf-8"),
                metadata.get("metadata", {}),
            )
        )
    if not result:
        raise ValueError(f"no replay metadata found at {path}")
    return tuple(result)


def _pairs(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ValueError(f"--{label} requires context=value")
        result[key] = item
    return result


def _toml_document(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _emit(value: Any, *, pretty: bool) -> None:
    print(json.dumps(value, sort_keys=True, indent=2 if pretty else None, default=str))


def _config_template(targets: int) -> str:
    sections = []
    for index in range(targets):
        number = index + 1
        sections.append(
            f"[chains.chain{number}]\n"
            f'rpc_env = "CHAIN{number}_RPC_URL"\n'
            f"chain_id = {number}\n"
            "fork_block = 0\n"
            'expected_block_hash = "0x..."\n'
            'expected_state_root = "0x..."\n'
        )
    return (
        "[default]\n"
        'invariants = "test/invariants"\n'
        'output = ".astarots/output"\n'
        'tools = ["echidna", "halmos", "slither"]\n\n'
        + "\n".join(sections)
        + "\n# Add targets, correlations, relay, snapshot, actors, tools, and bounds.\n"
    )


def _invariant_template(template: str) -> str:
    if template == "lock-mint":
        name = "LockMintInvariant"
        first, second = "locked", "minted"
    else:
        name = "CrossChainInvariant"
        first, second = "sourceValue", "destinationValue"
    return f"""// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.20;

contract {name} {{
    /// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
    /// @transition ethereum.bridge:{first} increase=["deposit(bytes32,uint256)"]
    /// @transition polygon.bridge:{second} increase=["consume(bytes32,uint256)"]
    /// @observation AFTER_ALL_DELIVERED quiescence=NO_PENDING_MESSAGES exclude=expired,rejected
    /// @correlation bridge_message
    /// @bind {first}=ethereum.bridge.totalLocked() {second}=polygon.bridge.totalMinted()
    /// @quantify FORALL messageHash: {first} == {second}
    /// @observe touched,relay max=128
    /// @assume signer_honesty: true
    function invariant_cross_chain() public view {{
        assert(totalLocked() == totalMinted());
    }}

    function totalLocked() internal pure returns (uint256) {{ return 0; }}
    function totalMinted() internal pure returns (uint256) {{ return 0; }}
}}
"""


if __name__ == "__main__":
    raise SystemExit(main())
