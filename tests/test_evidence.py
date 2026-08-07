"""Evidence artifacts and reporting contracts."""

from devil.core import (
    Call,
    ChainId,
    EdgeCase,
    EvidenceStrength,
    ForkSnapshot,
    GlobalState,
    SearchResult,
    Verdict,
    ViolationSource,
    WitnessState,
)
from devil.evidence import EvidenceReport, assess, build_replay_pair


def _result() -> SearchResult:
    witness = WitnessState(
        snapshot=GlobalState(
            chain_snapshots={ChainId.ETHEREUM: ForkSnapshot(ChainId.ETHEREUM, 100)}
        ),
        correlation_value="0xmessage",
        chain=ChainId.ETHEREUM,
        branch_id="branch-1",
        call_sequence=(Call("submit(bytes)", ("ghp_secret",), ChainId.ETHEREUM),),
    )
    edge = EdgeCase(
        depth=1,
        witnesses=(witness,),
        evidence_strength=EvidenceStrength.REPLAYED,
        violation_source=ViolationSource.INTRODUCED_BY_TRACE,
        chains=(ChainId.ETHEREUM,),
    )
    return SearchResult(
        witnesses=(witness,),
        deepest_edge=edge,
        outcome=Verdict.VIOLATED,
        budget_used=1,
        budget_total=2,
    )


def test_replay_pair_is_redacted_and_writeable(tmp_path) -> None:
    vulnerable, regression = build_replay_pair(
        "finding-1",
        _result().witnesses[0],
        metadata={"token": "ghp_very_secret", "rpc": "https://user:pass@example"},
    )
    assert vulnerable.mode == "vulnerable"
    assert regression.mode == "regression"
    path = vulnerable.write(tmp_path / "vulnerable.json")
    content = path.read_text()
    assert "ghp_" not in content
    assert "<REDACTED>" in content


def test_report_separates_verdict_and_confidence() -> None:
    result = _result()
    report = EvidenceReport("invariant-1", result, {"run_id": "run-1"})
    payload = report.to_dict()
    assert payload["findings"][0]["aggregate_strength"] == "replayed"
    assert payload["summary"]["invariants_violated"] == 1
    assert "Verdict: violated" in report.to_console()
    confidence = assess(result)
    assert confidence.verdict is Verdict.VIOLATED
    assert confidence.score == 0.85
