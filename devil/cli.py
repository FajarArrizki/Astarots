"""Command-line entrypoints for validating mainnet-fork campaigns."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from devil.core.config import load_campaign


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astarots")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate a fork campaign TOML")
    validate.add_argument("--config", type=Path, default=Path("astarots.toml"))
    forks = subparsers.add_parser("forks", help="list pinned fork blocks from a campaign TOML")
    forks.add_argument("--config", type=Path, default=Path("astarots.toml"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_campaign(args.config)
    except (OSError, ValueError) as exc:
        print(f"astarots: {exc}", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(
            json.dumps(
                {
                    "config": str(args.config),
                    "chains": sorted(config.chains),
                    "targets": sorted(config.targets),
                    "tools": list(config.tools),
                    "max_depth": config.max_depth,
                    "max_states": config.max_states,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "forks":
        print(
            json.dumps(
                {
                    alias: {
                        "chain_id": chain.chain_id,
                        "rpc_env": chain.rpc_env,
                        "fork_block": chain.fork_block,
                        "expected_block_hash": chain.expected_block_hash,
                    }
                    for alias, chain in sorted(config.chains.items())
                },
                sort_keys=True,
            )
        )
        return 0
    return 2
