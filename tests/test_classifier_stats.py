from __future__ import annotations

from datetime import UTC, date, datetime

from tokenburn.db import open_db, upsert_events
from tokenburn.models import Confidence, DateRange, UsageEvent
from tokenburn.reports.classifier_stats import build_classifier_stats


def _ev(**kw) -> UsageEvent:
    base = dict(
        id="evt",
        provider="claude_code",
        tool="claude_code",
        timestamp_start=datetime(2026, 4, 15, 10, tzinfo=UTC),
        timezone="UTC",
        source_type="local_jsonl",
        source_parser="claude_native_jsonl",
        confidence=Confidence.EXACT_FROM_LOCAL_LOG,
        model="claude-opus-4-7",
        session_id="s1",
        input_tokens=1000,
        output_tokens=100,
        total_tokens=1100,
    )
    base.update(kw)
    return UsageEvent(**base)


def _seed_class(db, session_id: str, provider: str, category: str, confidence: float = 0.9) -> None:
    db["session_classifications"].insert(
        {
            "session_id": session_id,
            "provider": provider,
            "task_category": category,
            "confidence": confidence,
            "classifier": "heuristic",
            "classifier_version": "heuristic-v1",
            "features_json": "{}",
            "classified_at": "2026-04-15T10:00:00Z",
        },
        pk=("session_id", "provider"),
        replace=True,
    )


def _seed_override(db, session_id: str, provider: str, category: str) -> None:
    db["session_overrides"].insert(
        {
            "session_id": session_id,
            "provider": provider,
            "task_category": category,
            "note": "test",
            "set_at": "2026-04-15T10:00:00Z",
        },
        pk=("session_id", "provider"),
        replace=True,
    )


def test_empty_db_returns_zeroes(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    s = build_classifier_stats(db, range_=None)
    assert s["coverage"] == []
    assert s["confidence_total"] == 0
    assert s["override_total"] == 0
    assert s["override_pairs"] == []
    assert s["unclassified_eligible"] == []
    assert s["unclassified_ineligible"] == []


def test_coverage_math_for_mixed_classified_and_unclassified(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    # Three claude_code sessions, only two classified.
    upsert_events(
        db,
        [
            _ev(id="a", session_id="s1"),
            _ev(id="b", session_id="s2"),
            _ev(id="c", session_id="s3"),
        ],
    )
    _seed_class(db, "s1", "claude_code", "extraction")
    _seed_class(db, "s2", "claude_code", "summarization")

    s = build_classifier_stats(db, range_=None)
    cov = {r["provider"]: r for r in s["coverage"]}
    assert cov["claude_code"]["total"] == 3
    assert cov["claude_code"]["classified"] == 2
    assert abs(cov["claude_code"]["pct"] - (2 / 3)) < 1e-9
    assert cov["claude_code"]["eligible"] is True


def test_ineligible_providers_marked_not_eligible(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [
            _ev(id="cu", provider="cursor", tool="cursor", session_id="cs1"),
            _ev(id="ge", provider="gemini", tool="gemini", session_id="gs1"),
        ],
    )
    s = build_classifier_stats(db, range_=None)
    cov = {r["provider"]: r for r in s["coverage"]}
    assert cov["cursor"]["eligible"] is False
    assert cov["cursor"]["pct"] is None
    assert cov["gemini"]["eligible"] is False
    # And they show up as "ineligible" in the unclassified composition.
    inelig_providers = {r["provider"] for r in s["unclassified_ineligible"]}
    assert inelig_providers == {"cursor", "gemini"}
    assert s["unclassified_eligible"] == []


def test_confidence_histogram_buckets_correctly(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [
            _ev(id="a", session_id="s1"),
            _ev(id="b", session_id="s2"),
            _ev(id="c", session_id="s3"),
            _ev(id="d", session_id="s4"),
        ],
    )
    _seed_class(db, "s1", "claude_code", "extraction", confidence=0.95)  # 0.80–1.00
    _seed_class(db, "s2", "claude_code", "extraction", confidence=0.65)  # 0.60–0.79
    _seed_class(db, "s3", "claude_code", "extraction", confidence=0.45)  # 0.40–0.59
    _seed_class(db, "s4", "claude_code", "extraction", confidence=0.20)  # < 0.40

    s = build_classifier_stats(db, range_=None)
    assert s["confidence_total"] == 4
    assert s["confidence"]["0.80–1.00"] == 1
    assert s["confidence"]["0.60–0.79"] == 1
    assert s["confidence"]["0.40–0.59"] == 1
    assert s["confidence"]["< 0.40"] == 1


def test_override_pairs_exclude_same_category_overrides(tmp_path):
    """A user overriding to the same label the classifier picked isn't a
    disagreement — it shouldn't appear in the pairs table, even though it
    still counts toward the total override count."""
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [
            _ev(id="a", session_id="s1"),  # heuristic: extraction, override: research (disagree)
            _ev(id="b", session_id="s2"),  # heuristic: extraction, override: research (disagree)
            _ev(id="c", session_id="s3"),  # heuristic: summarization, override: summarization (same)
        ],
    )
    _seed_class(db, "s1", "claude_code", "extraction")
    _seed_class(db, "s2", "claude_code", "extraction")
    _seed_class(db, "s3", "claude_code", "summarization")
    _seed_override(db, "s1", "claude_code", "research")
    _seed_override(db, "s2", "claude_code", "research")
    _seed_override(db, "s3", "claude_code", "summarization")  # confirmation, not disagreement

    s = build_classifier_stats(db, range_=None)
    # Total includes all overrides — 3.
    assert s["override_total"] == 3
    # Pairs table only shows the disagreement: 2× (extraction → research).
    assert len(s["override_pairs"]) == 1
    pair = s["override_pairs"][0]
    assert pair["heuristic"] == "extraction"
    assert pair["override"] == "research"
    assert pair["n"] == 2


def test_range_filter_excludes_out_of_range_events(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    in_range = _ev(id="in", session_id="s_in", timestamp_start=datetime(2026, 4, 15, tzinfo=UTC))
    in_range.local_date = date(2026, 4, 15)
    out_range = _ev(id="out", session_id="s_out", timestamp_start=datetime(2026, 3, 15, tzinfo=UTC))
    out_range.local_date = date(2026, 3, 15)
    upsert_events(db, [in_range, out_range])
    _seed_class(db, "s_in", "claude_code", "extraction")
    _seed_class(db, "s_out", "claude_code", "extraction")

    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    s = build_classifier_stats(db, range_=rng)

    # Coverage only counts April session.
    cov = {r["provider"]: r for r in s["coverage"]}
    assert cov["claude_code"]["total"] == 1
    assert cov["claude_code"]["classified"] == 1
    # Confidence histogram only counts April-classified session.
    assert s["confidence_total"] == 1

    # All-time counts both.
    s_all = build_classifier_stats(db, range_=None)
    cov_all = {r["provider"]: r for r in s_all["coverage"]}
    assert cov_all["claude_code"]["total"] == 2
    assert s_all["confidence_total"] == 2
