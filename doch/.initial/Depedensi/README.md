# Dependencies — Astarots

This document records the dependencies and external tools required to develop and run Astarots. Python packages are project-local and resolved by `uv.lock`; native binaries are kept outside the Python lockfile and are either installed under `.tools/` or supplied by the host toolchain.

---

## Runtime

| # | Name | Version | Description | Link |
|---|---|---|---|---|
| 1 | Python | 3.12+ | Language runtime required by the project; verified locally with Python 3.12.13 | [python.org](https://www.python.org/) |
| 2 | uv | 0.11.32 | Project manager, virtual environment, dependency resolver, and lockfile manager | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |

## Python Development Dependencies

These packages are declared in `pyproject.toml`, resolved in `uv.lock`, and run through the project environment with `uv run`. They MUST NOT be installed globally for this project.

| # | Name | Resolved version | Purpose | Link |
|---|---|---:|---|---|
| 1 | pytest | 9.1.1 | Python test runner | [pytest.org](https://pytest.org/) |
| 2 | ruff | 0.16.1 | Linter and formatter | [docs.astral.sh/ruff](https://docs.astral.sh/ruff/) |
| 3 | slither-analyzer | 0.11.6 | Static Solidity analysis and seed-hint generation | [github.com/crytic/slither](https://github.com/crytic/slither) |
| 4 | halmos | 0.3.3 | Symbolic execution and bounded confirmation | [github.com/a16z/halmos](https://github.com/a16z/halmos) |

The transitive dependencies of Slither and Halmos are also captured by `uv.lock`. The lockfile is the source of reproducibility for the Python toolchain.

## Native Analysis Tools

Native binaries are not Python packages and MUST NOT be added to `uv.lock`. They are versioned here with their installation and verification commands.

| # | Name | Version | Install location | Purpose | Link |
|---|---|---:|---|---|---|
| 1 | Echidna | 2.3.3 | `.tools/bin/echidna` | Stateful fuzzing and counterexample generation | [github.com/crytic/echidna](https://github.com/crytic/echidna) |
| 2 | Foundry `forge` | 1.7.1 | Host Foundry installation | Solidity compilation, fork tests, and replay | [getfoundry.sh](https://getfoundry.sh/) |

Echidna is kept project-local and `.tools/` is ignored by Git. The macOS x86_64 release is verified with SHA-256 before extraction:

```bash
mkdir -p .tools/bin .tools/cache
curl -fL --retry 3 \
  -o .tools/cache/echidna-2.3.3-x86_64-macos.tar.gz \
  https://github.com/crytic/echidna/releases/download/v2.3.3/echidna-2.3.3-x86_64-macos.tar.gz
printf '%s  %s\n' \
  8fa994e6589ce00b548b0aa183e60b5e123e9d9ce7aae6a6228ea667eb5c0194 \
  .tools/cache/echidna-2.3.3-x86_64-macos.tar.gz | shasum -a 256 -c -
tar -xzf .tools/cache/echidna-2.3.3-x86_64-macos.tar.gz -C .tools/bin
chmod +x .tools/bin/echidna
```

## Installation and Verification

From the Astarots repository root:

```bash
uv sync --dev
uv run slither --version
uv run halmos --version
.tools/bin/echidna --version
uv run pytest
uv run ruff check .
```

The current verified output is Slither `0.11.6`, Halmos `0.3.3`, Echidna `2.3.3`, and pytest `2 passed`.

Foundry is invoked with the Homebrew libusb directory on this AMD Hackintosh. This does not modify the global installation or the project lockfile:

```bash
cd /Volumes/Disk\ \(256GB\)/Oxastarots/Wormhole/Mainet/fork-test
DYLD_LIBRARY_PATH="$(brew --prefix libusb)/lib" forge build
```

The fork test compiles successfully with Foundry `1.7.1`. Existing warnings are Solidity mutability and unsafe-typecast warnings in the Wormhole test sources; they do not prevent compilation.

## Dependency Policy

- Add Python packages with `uv add --dev <package>` or `uv add <package>`, never with global `pip install`.
- Commit both `pyproject.toml` and `uv.lock` for every Python dependency change.
- Use `uv run <command>` so the command resolves from the project environment.
- Keep native binaries out of `uv.lock`; install them under `.tools/` when a verified release exists.
- Record native version, platform, checksum, and verification command in this document.
- Keep `.tools/`, `.venv/`, and caches out of Git.

## Current Status

| Component | Status | Notes |
|---|---|---|
| Python + uv | Ready | Project requires Python `>=3.12` |
| pytest + ruff | Ready | Locked and project-local |
| Slither | Ready | Locked and project-local via `uv run` |
| Halmos | Ready | Locked and project-local via `uv run` |
| Echidna | Ready | Local verified binary under `.tools/bin/` |
| Foundry forge | Ready | Host installation works with `DYLD_LIBRARY_PATH` |
| Wormhole fork tests | Compiles | `forge build` succeeds; source warnings remain |
