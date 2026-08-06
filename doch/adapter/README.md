# Adapter Protocol

Each adapter wraps one external analysis tool and exposes it to the harness through a uniform interface. The harness never calls a tool directly — it goes through the adapter, which handles process management, chain-specific configuration, and output normalization.

For cross-chain probing, adapters receive a `chain` context with a pinned fork block. They **do not deploy contracts** — they fork mainnet at the configured block and operate on the existing contract state.

### Relay Mode

Messages relayed between forks use an explicit authenticity mode:

| Mode | Authentication Evidence | Use Case |
|---|---|---|
| **historical-authentic** | Protocol-valid proof captured from chain/indexer data | Replaying known cross-chain messages |
| **protocol-valid-synthetic** | Valid proof produced by a declared local signer/verifier fixture | Testing protocol logic with synthetic messages |
| **modeled-relay** | Verification modeled or bypassed under a recorded assumption | Exploring logic beyond the verification boundary |
| **raw-payload** | Unauthenticated payload injected after that boundary | Testing application response to arbitrary payloads |

Protocol adapters define proof formats and verifier semantics. Modeled or raw-payload findings never claim to bypass production authentication.

In `historical-authentic` mode, delivery requires an exact dataset match on message ID, payload hash, source event, and attestation. A probe-generated message without that match cannot silently borrow another proof and remains ineligible.

Tools see one chain at a time and return typed proposals or confirmations. The harness' unified causal search and canonical executor own cross-chain state; adapters never merge independent chain snapshots.

---

## Tool Capabilities

Not every adapter can do everything. Slither cannot execute. Echidna cannot symbolically prove. The harness must know what each tool can and cannot do before routing work. Every adapter declares its capabilities upfront:

```
ToolCapabilities:
    static_analysis: bool              # Slither — analyze without execution
    stateful_fuzzing: bool             # Echidna — generate and execute sequences
    symbolic_execution: bool           # bounded symbolic path exploration
    concrete_replay: bool              # Can this tool execute a fixed sequence?
    shrinking: bool                    # Can this tool minimize a counterexample?
    supported_targets: list[str]       # e.g. ["solidity", "vyper"]
    supported_artifacts: list[str]     # e.g. ["crytic-corpus", "halmos-trace"]
```

---

## Typed Outcomes

Tool results are not boolean pass/fail. Every operation returns the same typed envelope:

```
ToolRunResult[T]:
    outcome: Success | Counterexample | UnsatUnderBounds | Timeout | ToolError | Unsupported | Partial
    value: Optional[T]
    evidence: list[Evidence]
    artifacts: list[ArtifactRef]
    diagnostics: list[Diagnostic]
    bounds: Optional[Bounds]

Counterexample(model)       # concrete or symbolic counterexample found
UnsatUnderBounds(bounds)    # no violation in the explored bounded model
Timeout(elapsed, budget)
ToolError(reason)
Unsupported(reason)
Partial(partial_result)
```

`Success` means the requested operation completed; it does not mean the target is safe. Incomplete outcomes remain `inconclusive` at campaign level.

---

## Exchange Artifacts

Tools exchange structured artifacts, not raw text. Each artifact type is an explicit contract between producer and consumer:

```
StaticHint            # static analyzer → unified search engine
SeedCorpus            # unified search engine → fuzzer
CandidateTrace        # typed tool proposal; replayed by canonical executor
ConstraintSet         # extracted preconditions
BaseForkFingerprint   # deterministic base shared across tools
ChainProjection       # serializable per-chain view derived from GlobalState
ActionTrace           # portable calls + environment transitions
ReplayResult          # canonical state before/after + events
ProjectionManifest    # slots/code/environment materialized for Halmos
BoundedConfirmation   # symbolic outcome with projection and bounds
Diagnostic            # raw output, warnings, metadata
```

```
StaticHint:
    context_id: ContextId
    selector: str
    kind: ACCESS_GAP | TAINT_FLOW | EXTERNAL_CALL | STORAGE_COUPLING | OTHER
    source_locations: list[SourceLocation]
    constraints: ConstraintSet
    suspicion: float
    producer: ToolIdentity

SeedCorpus:
    chain_id: ChainId
    base_fingerprint: BaseForkFingerprint
    entries: list[CallTemplate]
    derived_hint_ids: list[str]
    format: str

CallTemplate:
    context_id: ContextId
    function_signature: str
    argument_domains: list[ValueDomain]
    value_range: Range
    allowed_actor_classes: list[str]

ProbeArtifact: StaticHint | CandidateTrace | BoundedConfirmation

CandidateTrace:
    call_sequence: list[CrossChainStep]
    actor: Actor
    constraints: ConstraintSet
    suspicion: float
    evidence: Evidence
    originating_chain: ChainId

ChainProjection:
    chain_id: ChainId
    base_fingerprint: BaseForkFingerprint
    materialized_state: StateManifest
    relevant_messages: list[MessageState]
    block_number_delta: int
    timestamp_delta: int
    assumptions: list[Assumption]

WitnessProjection:
    chain_projection: ChainProjection
    trace_segment: list[CrossChainStep]
    claim: Property | TransitionRule
    projection_manifest: ProjectionManifest
```

The unified search engine validates hint selectors against the bound ABI, intersects hint constraints with invariant and actor policies, and converts accepted hints into `SeedCorpus` entries through the destination adapter. Lossy or unsupported conversions return diagnostics and never fabricate calldata. Echidna corpus entries become `CandidateTrace` values; selected trace segments become `WitnessProjection` values for Halmos.

`CandidateWorkers.propose(...) → ToolRunResult[list[CandidateTrace]]` is the harness-level aggregation boundary. It runs enabled capable adapters, performs the validated hint/corpus conversions above, and deduplicates traces by canonical trace hash. Its outcome is `Counterexample` when at least one returned trace is already a tool counterexample and no requested path failed, `Success` for complete proposal generation, `Partial` when usable traces coexist with timeout/error/unsupported paths, and `Unsupported` only when no enabled adapter can generate a candidate. Per-adapter outcomes and diagnostics remain attached; aggregation never upgrades an incomplete run to success.

---

## Adapter Interface

All methods are capability-gated; `Unsupported` is a typed outcome and the unified engine may route the operation elsewhere.

### `probe(target, invariant, constraints, projection, chain) → ToolRunResult[list[ProbeArtifact]]`

Run one tool against a serialized `ChainProjection`. Static tools return `StaticHint`; fuzzers return `CandidateTrace`; symbolic tools may return `BoundedConfirmation`. The unified engine routes and converts artifacts by declared capability. Canonical executor handles never cross the adapter boundary.

### `execute(target, trace, base_fingerprint, chain) → ToolRunResult[ReplayResult]`

Optional tool-native replay used as supporting evidence. This method never mutates authoritative global state. The canonical fork executor independently replays the same `ActionTrace` and supplies the state diff and events used by the search.

### `confirm(target, witness_projection, chain) → ToolRunResult[BoundedConfirmation]`

Attempt to reproduce the same segment violation from a serialized `WitnessProjection`. Bounds, assumptions, omitted state, base fingerprint, and the projection manifest are mandatory evidence; backend handles never cross this boundary.

Confirmation never returns a bare boolean. `Counterexample` means the violation was reproduced under the reported model; `UnsatUnderBounds` only means it was not reproduced within those bounds. The evidence aggregator derives strength from the complete `ToolRunResult`.

---

## Per-Tool Adaptations

### Echidna Adapter

Echidna is a fuzzer. Best suited for concrete sequence exploration and boundary fuzzing of cross-chain state machines.

**Cross-chain relevance:** Echidna excels at concrete boundary sequences—signer counts at exactly $M-1$, sequence wraparound, and queue capacity—and emits candidates that can be replayed across the causal search.

**Probe mode:** Echidna supports native RPC-based state forking since v2.1.0. Configure `rpcUrl` and `rpcBlock` per chain — Echidna lazily fetches storage and bytecode from the RPC at the specified block. The contract is already deployed at its mainnet address; the adapter points Echidna's configuration at that address. Apply cross-chain constraints extracted from the forked state as seed configuration. Run fuzzing with the sub-invariant as a property check. Parse corpus for sequences that explore cross-chain-interacting functions — the search starts from the real protocol state, not an empty deployment.

One Echidna process handles one chain with one base block. Cross-chain probing requires separate Echidna instances per chain. Fork cache is saved as an artifact for reproducibility. `warp`/`roll` only simulate local time — they do not advance historical mainnet state.

*Reference: [Echidna state network forking](https://secure-contracts.com/program-analysis/echidna/advanced/state-network-forking.html)*

**Execute mode:** Optional tool-native replay verifies that Echidna can reproduce its candidate. Cross-chain events are exported in the `CandidateTrace`; only the canonical executor applies relay transitions to another chain.

**Confirm mode:** Echidna cannot independently confirm its own findings. Its confirm operation is deterministic replay only; independent confirmation requires a different method, such as Halmos projected-state analysis. The unified search engine routes the witness according to capabilities.

*This is an explicit instance of the adapter protocol rule: confirmation must use a different analysis method than the original probe.*

### Halmos Adapter

Halmos is a symbolic execution engine. Best suited for verifying threshold logic, signature verification, and numeric bounds in cross-chain invariants — under the bounds and assumptions of the selected model.

**Cross-chain relevance:** Halmos is useful for bounded confirmation of numeric thresholds such as signer quorum, message count, and fee boundaries, with every solver and loop bound recorded.

**Probe mode:** Halmos does **not** support full RPC-based fork like Echidna or Foundry. The official Halmos fork example uses `vm.etch` to install bytecode and `vm.store` to set specific storage slots — it does not connect to a live RPC or enumerate full contract storage.

For mainnet fork probing, the adapter uses **bounded state projection**: extract only the code + storage slots + environment values that a witness requires from the forked state, materialize them into Halmos via `vm.etch` and `vm.store`, and run symbolic execution against that projected state. The projection is always documented with a manifest of which slots were included and which were omitted.

When Halmos confirms a counterexample under projected state, the evidence is labeled **`symbolically-confirmed-under-projected-state`** — not as if the full mainnet fork was verified. Until Halmos supports native RPC forking, it is limited to confirmation of per-witness projections.

*Reference: [Halmos fork example](https://github.com/a16z/halmos/blob/main/examples/simple/test/Fork.t.sol)*

**Execute mode:** Symbolic execution is expensive for concrete replay. Verify path reachability symbolically, but pair with Echidna or a lightweight executor for the actual state diff.

**Confirm mode:** Given a witness from Echidna, project the witness's relevant code + storage into Halmos. **SAT** produces a model/counterexample under the projected state and bounds — evidence strength: `symbolically-confirmed-under-projected-state`. **UNSAT** means no counterexample was found in the projected state space under those assumptions — it does not mean the full mainnet fork is safe.


### Slither Adapter

Slither is a static analysis framework. Best suited for detecting structural vulnerabilities in cross-chain entry points — missing access control, unchecked return values from cross-chain calls, reentrancy paths that span the bridge.

**Cross-chain relevance:** Slither identifies code patterns that create cross-chain attack surface: functions callable by the relayer without proper authentication, storage variables writable from cross-chain messages without validation, missing checks on message origin. These become seed targets for Echidna's dynamic exploration, delivered as `StaticHint` artifacts.

**Probe mode:** Run static analysis on the specified chain's contract. Filter detectors to cross-chain-relevant patterns: access control on relayer-callable functions, taint from message payloads to storage, missing validation on cross-chain events.

**Execute mode:** Slither cannot execute and returns `Unsupported`. The unified search engine sends the `ActionTrace` to the canonical fork executor; execution is not hidden inside the Slither adapter.

**Confirm mode:** Slither's findings serve as corroborating evidence, not independent confirmation. If Echidna found a counterexample that exploits a pattern Slither flagged, the finding is strengthened but not symbolically confirmed without verification from Halmos.

---

## Fallback & Error Handling

When a tool cannot handle a request, it returns `Unsupported` or `ToolError` rather than failing silently. The unified search engine handles these outcomes:

- `Unsupported` → route to a tool with different capabilities.
- `Timeout` → mark the operation inconclusive; a configured policy may reduce later branching or depth.
- `ToolError` → log, skip this candidate, continue with other branches.
- `Partial` → use available results, mark the branch as incomplete in the report.

The unified search engine never waits indefinitely for a tool; every adapter call has a timeout, and stale processes are terminated with their partial evidence marked inconclusive.

---

## Adding a New Adapter

To add support for a new tool:

1. Create `devil/adapter/<tool_name>.py` implementing the capability-gated adapter interface with `chain` context support.
2. Declare `ToolCapabilities` so the unified search engine can route operations.
3. Register it in the adapter registry with a unique name and the path to the tool binary.
4. Add a `ToolConfig` entry specifying CLI flags, timeout defaults, chain-specific options, and output format.
5. Add a section to this document describing the tool's cross-chain strengths, capabilities, and limitations.

No harness code outside the adapter directory should change.

---

## Output Normalization

All adapters produce the same internal structs regardless of the tool's native format. The normalization layer handles:

- Parsing tool-specific output (JSON, text, SARIF).
- Mapping tool-specific outcomes to the harness' `Outcome` types.
- Converting call representations into harness `Call` and `Sequence` types, tagged with the `chain` they belong to.
- Stripping tool-specific noise from evidence.

Raw tool output is preserved in `Evidence.raw` for debugging and audit, tagged with the chain it came from.


---

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution engine used for bounded formal verification
- [Echidna](https://github.com/crytic/echidna) — fuzzer used for concrete sequence exploration
- [Foundry](https://getfoundry.sh/) — Solidity development framework; invariant files follow Foundry test conventions
