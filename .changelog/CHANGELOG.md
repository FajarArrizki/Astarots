# Changelog

All notable changes to the Astarots project, tracked per development phase.

---

## [0.1.0-dev] — Foundation Phase

### 2026-08-06 — Toolchain & Scaffold

#### Verified
- Python 3.14.6 — installed
- uv 0.11.32 — installed
- Slither 0.11.5 — installed (`/Users/oxastarots/.local/bin/slither`)
- Foundry (forge) 1.7.1 — installed (`/Users/oxastarots/.foundry/bin/forge`)

#### Pending
- **Echidna** — not installed. Required for concrete fuzzing adapter. Install via: `foundryup` or `crytic/echidna` binary.
- **Halmos** — not installed. Required for symbolic confirmation adapter. Install via: `foundryup` or `pip install halmos` (may need `--break-system-packages` or `uv pip install`).
- **Foundry libusb** — `forge` binary fails on Hackintosh (AMD) due to missing `libusb-1.0.0.dylib` at expected path `/usr/local/opt/libusb/lib/`. Library is installed via Homebrew but at a different location. Needs symlink or DYLD_LIBRARY_PATH fix.
- **Wormhole fork test compile** — blocked by libusb. `forge build` in `Mainet/fork-test/` pending.


### 2026-08-07 — Core Implementation

#### Completed
- [x] `devil/core/types.py` — 20 frozen dataclasses: ChainId, Outcome, Verdict, Constraint, Call, Evidence, Candidate, ForkSnapshot, GlobalState, SearchState, WitnessState, EdgeCase, Actor, Impact, BaselineResult, RelayMessage, RelayDataset, SearchResult, SlotChange
- [x] `devil/core/__init__.py` — re-exports all types
- [x] `devil/invariant/ir.py` — CrossChainInvariant IR: Context, Binding, TransitionPredicate, CorrelationExtractor, QuantifiedPredicate, ObservationPolicy, QuiescenceRule, ProxyInfo, ObservationSet
- [x] `devil/invariant/ir.py::load_invariant()` — skeleton .t.sol parser
- [x] `devil/invariant/ir.py::validate_invariant()` — IR completeness validator
- [x] `devil/adapter/protocol.py` — ToolAdapter Protocol, ToolCapabilities, ExecutionResult, ArtifactStore
- [x] `devil/adapter/slither/adapter.py` — SlitherAdapter with probe(), execute(), confirm()
- [x] `devil/adapter/slither/__init__.py` — re-exports
- [x] Package structure: per-tool subfolders (slither/, future: echidna/, halmos/)
- [x] Verified: all imports resolve, ruff clean, pytest 2/2 passed

#### Structure
```
devil/
├── core/
│   ├── __init__.py          # re-exports all types
│   └── types.py             # 20 frozen dataclasses
├── invariant/
│   ├── __init__.py          # re-exports
│   └── ir.py                # IR structs + parser skeleton
├── adapter/
│   ├── __init__.py          # protocol re-exports
│   ├── protocol.py          # ToolAdapter, ToolCapabilities, ExecutionResult
│   └── slither/
│       ├── __init__.py
│       └── adapter.py       # SlitherAdapter
├── harness/                 # (pending)
└── report/                  # (pending)
```

#### Pending
- [ ] `devil/harness/` — unified beam search engine
- [ ] `devil/report/` — console + JSON output
- [ ] Slither adapter: test against real Solidity file
- [ ] Echidna adapter: install echidna, implement adapter
- [ ] Halmos adapter: install halmos, implement adapter
- [ ] `load_invariant()`: full .t.sol NatSpec parser
- [ ] Expression engine: evaluate QuantifiedPredicate.predicate strings
- [ ] Wormhole fork test compile (blocked by libusb)
#### Next Steps
1. Fix libusb for forge (symlink or env var)
2. Install echidna + halmos
3. Compile Wormhole fork tests
4. Implement `devil/core/` dataclasses
5. Implement `devil/invariant/` IR + parser
6. Implement `devil/adapter/` base + Slither adapter

---

### Changes

#### Added
- `devil/` — application source root
  - `devil/core/` — shared kernel (dataclasses, types, config)
  - `devil/invariant/` — invariant IR + .t.sol parser
  - `devil/adapter/` — tool adapters (Slither, Echidna, Halmos)
  - `devil/harness/` — orchestrator + unified beam search
  - `devil/report/` — console + JSON output
- `.changelog/` — versioned task tracking

#### Design
- Complete design documentation in `doch/` (7 domains, 3 review rounds)
- Mainnet fork invariant testing model finalized
- Invariant IR: TransitionPredicate, CorrelationExtractor, Binding, QuantifiedPredicate
- Algorithm: unified frontier, branch-local GlobalState, WitnessState, baseline evaluation
- Adapter protocol: ToolCapabilities, typed Outcome, relay mode classification
