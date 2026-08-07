# Change Log

## 2026-08-07 — Design conformance implementation

- Implemented the documented typed invariant IR/NatSpec contract, strict campaign config, pinned snapshot/proxy verification, immutable global state, canonical fork executor, causal relay lifecycle, transition monitors, bounded liveness, and corrected search accounting.
- Standardized adapter results and projected-state handoff: live local forks for Echidna, explicit code/storage/environment materialization for Halmos, and verified source/proxy identity for Slither.
- Added complete evidence reports, executable vulnerable/fixed Foundry replay artifacts with fingerprint preflight, and the documented `chain`, `probe`, `replay`, `validate`, `list-tools`, `init`, and `forks` CLI surface.
- Added portable Foundry runtime loading for Homebrew `libusb` on macOS and removed the obsolete Wormhole-specific integration path.
- Verified with `uv run pytest -q` (30 passed), `uv run ruff check .` (clean), generated replay compilation, and two identical launches of pinned Ethereum block 25,700,000 plus Polygon block 91,580,000.
