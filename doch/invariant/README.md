# Invariant Specification

Invariants are properties that must hold for all reachable states of a contract. They are the "fence posts" that the harness probes against — when an invariant breaks, an edge case has been found.

Invariants are defined in `.t.sol` files using standard Foundry test conventions. The harness reads these files, extracts invariant signatures, and translates them into tool-specific formats. No custom DSL or annotation system is required beyond what Foundry already provides.

---

## Invariant File Convention

An invariant file lives under `test/invariants/` and follows this structure:

```solidity
// test/invariants/VaultInvariants.t.sol
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {Vault} from "../src/Vault.sol";

contract VaultInvariants is Test {
    Vault vault;

    function setUp() public {
        vault = new Vault();
        // Optional: initialize state common to all invariants
    }

    // Invariant: no user can withdraw more than their deposit
    function invariant_no_overdraft() public {
        // The harness provides initial state.
        // This function is called by tools after each
        // transaction sequence.
        for (uint i = 0; i < vault.userCount(); i++) {
            address user = vault.userAt(i);
            assert(vault.balanceOf(user) >= vault.totalWithdrawn(user));
        }
    }

    // Invariant: total supply never exceeds the cap
    function invariant_supply_under_cap() public {
        assert(vault.totalSupply() <= vault.CAP());
    }

    // Invariant: deposits always equal shares
    function invariant_deposit_equals_shares() public {
        assert(vault.totalDeposits() == vault.totalShares());
    }
}
```

The harness identifies invariant functions by the `invariant_` prefix — matching Foundry's convention. Functions with other prefixes (`test_`, `setUp`, helpers) are ignored.

---

## What Makes a Good Invariant

An invariant should be:

**Stateful, not stateless.** It checks a relationship that must hold across all contract states, not just the result of a single call. `assert(token.totalSupply() <= cap)` is an invariant. `assert(token.transfer(a, b, 100))` is a test.

**Deterministic given state.** The invariant function reads contract state and asserts. It does not modify state, call external contracts, or depend on `block.timestamp` directly (the harness controls time as a constraint).

**Narrow enough to be breakable.** An invariant that never breaks provides no signal. "A contract cannot self-destruct" on a contract without `selfdestruct` is a tautology — the harness will explore and find nothing, wasting cycles.

**Broad enough to be meaningful.** An invariant that only checks `1 == 1` is trivially satisfied but useless. Each invariant should encode a security property that, if violated, indicates a real vulnerability.

---

## Invariant Scope

### Single-Contract Invariants

Most invariants apply to a single target contract. The harness deploys the contract, sets up initial state via `setUp()`, then probes.

### Cross-Contract Invariants

Some invariants span multiple contracts. For example, a token + vault system where "vault shares must always be redeemable for the underlying token." These invariants reference state from two contracts.

The harness handles these by deploying both contracts and providing both as targets. Invariant functions receive references to all deployed contracts through `setUp()`.

### Cross-Chain Invariants

Properties that span multiple chains — for example, a bridge where "locked tokens on chain A must equal minted tokens on chain B." These are the most complex invariants.

Cross-chain invariants are annotated with a NatSpec tag so the harness knows to decompose them:

```solidity
/// @crosschain src=Ethereum dst=Polygon
function invariant_bridge_locked_equals_minted() public {
    // BridgeEth on Ethereum, BridgePoly on Polygon
    // The harness deploys both, mocks the relayer,
    // and checks this across mock chain states.
}
```

The scheduler decomposes a cross-chain invariant into per-chain sub-invariants, probes each independently, and recombines results at the report layer.

---

## Invariant Types

The harness classifies invariants by the kind of property they encode. This classification guides tool selection and ranking:

| Type | Example | Best Tools |
|---|---|---|
| **Numeric bound** | `totalSupply ≤ CAP` | Echidna (fuzz extremes), Halmos (prove bound) |
| **Balance conservation** | `sum(balances) == totalSupply` | Halmos (symbolic equality) |
| **Access control** | `only owner can call pause()` | Slither (detect missing modifiers) |
| **State machine** | `status never goes from CLOSED to OPEN` | Echidna (sequence fuzzing) |
| **Reentrancy safety** | `no reentrant call alters critical state` | Slither (pattern), Echidna (trigger) |
| **Cross-chain equality** | `locked_A == minted_B` | Harness (decomposition), Halmos (per-chain) |

The harness uses this classification to decide which tools to run and how to interpret results. Access control invariants get Slither first. Balance conservation invariants get Halmos first. State machine invariants get Echidna first.

---

## Constraints from Invariants

Invariants themselves can generate constraints for the search. When an invariant checks a numeric bound like `totalSupply ≤ CAP`, the harness extracts:

```
Constraint(
    kind=STATE_VAR,
    target="totalSupply",
    value=Range(0, CAP),
    source="invariant:supply_under_cap"
)
```

This constraint is fed to the probe step — tools are told "explore paths where `totalSupply` is near `CAP`" because those are the most likely to break the bound.

---

## Invariant Metadata

Invariant files may include NatSpec tags for harness-specific configuration:

```solidity
/// @tools echidna, halmos
/// @severity CRITICAL
/// @timeout 300
function invariant_no_unauthorized_transfer() public { ... }
```

| Tag | Effect |
|---|---|
| `@tools` | Restrict which tools probe this invariant (default: all) |
| `@severity` | Override the confidence/impact display label |
| `@timeout` | Per-invariant timeout in seconds |
| `@crosschain` | Mark as cross-chain, specify source and destination chains |

Tags are optional. The harness works without any annotations — defaults are sensible for single-contract, single-chain invariants.
