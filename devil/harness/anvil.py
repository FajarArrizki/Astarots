"""Lifecycle management for one secret-safe local Anvil fork per campaign chain."""

from __future__ import annotations

import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from devil.core.config import ChainConfig
from devil.core.runtime import foundry_environment
from devil.core.snapshot import JsonRpcClient, SnapshotError
from devil.core.types import ChainId


class AnvilFleet:
    """Start fresh pinned forks without expanding upstream RPC secrets in argv."""

    def __init__(
        self,
        chains: Mapping[str, ChainConfig],
        *,
        binary: str = "anvil",
        startup_timeout: float = 30.0,
    ) -> None:
        self.chains = dict(chains)
        self.binary = binary
        self.startup_timeout = startup_timeout
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._processes: dict[ChainId, subprocess.Popen[str]] = {}
        self.clients: dict[ChainId, JsonRpcClient] = {}
        self.endpoints: dict[ChainId, str] = {}

    def __enter__(self) -> AnvilFleet:
        self._temporary = tempfile.TemporaryDirectory(prefix="astarots-anvil-")
        root = Path(self._temporary.name)
        endpoints = "\n".join(
            f'{alias} = "${{{chain.rpc_env}}}"' for alias, chain in sorted(self.chains.items())
        )
        (root / "foundry.toml").write_text(f"[rpc_endpoints]\n{endpoints}\n")
        try:
            for alias, chain in sorted(self.chains.items()):
                chain_id = ChainId(alias)
                port = _free_port()
                process = subprocess.Popen(
                    [
                        self.binary,
                        "--fork-url",
                        alias,
                        "--fork-block-number",
                        str(chain.fork_block),
                        "--chain-id",
                        str(chain.chain_id),
                        "--port",
                        str(port),
                        "--silent",
                    ],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=foundry_environment(),
                )
                self._processes[chain_id] = process
                endpoint = f"http://127.0.0.1:{port}"
                client = JsonRpcClient(endpoint, timeout=2)
                self._wait_ready(chain_id, chain, process, client)
                self.endpoints[chain_id] = endpoint
                self.clients[chain_id] = client
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        for process in self._processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self._processes.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self._processes.clear()
        self.clients.clear()
        self.endpoints.clear()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _wait_ready(
        self,
        chain_id: ChainId,
        config: ChainConfig,
        process: subprocess.Popen[str],
        client: JsonRpcClient,
    ) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = (process.stderr.read() if process.stderr else "").strip()
                raise RuntimeError(
                    f"Anvil fork for {chain_id.value} exited during startup: {stderr}"
                )
            try:
                observed = int(client.call("eth_chainId", []), 16)
                if observed != config.chain_id:
                    raise RuntimeError(f"Anvil chain ID mismatch for {chain_id.value}: {observed}")
                block = client.call("eth_blockNumber", [])
                if int(block, 16) < config.fork_block:
                    raise RuntimeError(f"Anvil fork block mismatch for {chain_id.value}")
                return
            except SnapshotError:
                time.sleep(0.05)
        raise TimeoutError(f"Anvil fork for {chain_id.value} did not become ready")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])
