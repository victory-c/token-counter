from __future__ import annotations

from datetime import UTC, date, datetime

from tokenburn.classifier.fitness import load_default as load_fitness
from tokenburn.db import open_db, upsert_events
from tokenburn.models import Confidence, DateRange, UsageEvent
from tokenburn.pricing import PricingTable, default_pricing_path
from tokenburn.reports.by_task import build_savings, build_task_summary


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
        input_tokens=1_000_000,
        output_tokens=100_000,
        total_tokens=1_100_000,
    )
    base.update(kw)
    return UsageEvent(**base)


def _seed_classification(db, session_id: str, provider: str, category: str) -> None:
    db["session_classifications"].insert(
        {
            "session_id": session_id,
            "provider": provider,
            "task_category": category,
            "confidence": 0.9,
            "classifier": "heuristic",
            "classifier_version": "heuristic-v1",
            "features_json": "{}",
            "classified_at": "2026-04-15T10:00:00Z",
        },
        pk=("session_id", "provider"),
        replace=True,
    )


def test_fitness_table_classes_known_models():
    fit = load_fitness()
    assert fit.class_of("claude-opus-4-7") == "heavy"
    assert fit.class_of("claude-sonnet-4") == "medium"
    assert fit.class_of("claude-haiku-4-5-20251001") == "light"
    assert fit.class_of("gpt-5.4-mini") == "light"
    assert fit.class_of("gpt-5.5") == "medium"
    assert fit.class_of("nonexistent-model") is None


def test_fitness_overshoot_flags_opus_for_extraction():
    fit = load_fitness()
    assert fit.is_overshooting("claude-opus-4-7", "extraction") is True
    assert fit.is_overshooting("claude-sonnet-4", "feature_implementation") is False
    assert fit.is_overshooting("claude-haiku-4-5", "extraction") is False


def test_savings_recommends_haiku_for_extraction(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    pricing = PricingTable.load(default_pricing_path())

    # An extraction session that used Opus.
    ev = _ev(id="a", session_id="s1")
    ev.estimated_cost_usd = 90.0  # 1M input ($15) + 100k output ($7.5) ≈ $22.5; bumped for clarity
    upsert_events(db, [ev])
    _seed_classification(db, "s1", "claude_code", "extraction")

    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    savings = build_savings(db, rng, pricing=pricing)

    # Category aggregation should include extraction with > $0 savings
    cat_row = next((r for r in savings["category_rows"] if r["task_category"] == "extraction"), None)
    assert cat_row is not None
    assert cat_row["savings_usd"] > 0
    # Minimum class should be light
    assert cat_row["minimum_class"] == "light"
    assert savings["total_savings_usd"] > 0


def test_savings_skips_already_right_sized_tasks(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    pricing = PricingTable.load(default_pricing_path())

    # Sonnet for feature_implementation — minimum class is medium, no overshoot.
    ev = _ev(id="b", session_id="s2", model="claude-sonnet-4")
    ev.estimated_cost_usd = 18.0
    upsert_events(db, [ev])
    _seed_classification(db, "s2", "claude_code", "feature_implementation")

    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    savings = build_savings(db, rng, pricing=pricing)
    # No overshoot → no opportunity row
    assert savings["category_rows"] == []
    assert savings["total_savings_usd"] == 0


def test_unclassified_does_not_trigger_savings(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    pricing = PricingTable.load(default_pricing_path())

    ev = _ev(id="c", session_id="s3")
    ev.estimated_cost_usd = 50.0
    upsert_events(db, [ev])
    # No classification row at all → falls into unclassified
    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    summary = build_task_summary(db, rng)
    assert any(r["task_category"] == "unclassified" for r in summary["by_task"])

    savings = build_savings(db, rng, pricing=pricing)
    assert savings["total_savings_usd"] == 0


def test_task_summary_respects_provider_filter(tmp_path):
    db = open_db(tmp_path / "t.sqlite")

    claude_ev = _ev(id="claude", session_id="s4")
    claude_ev.estimated_cost_usd = 10.0
    codex_ev = _ev(
        id="codex",
        provider="codex",
        tool="codex",
        model="gpt-5.4",
        session_id="s5",
    )
    codex_ev.estimated_cost_usd = 5.0
    upsert_events(db, [claude_ev, codex_ev])
    _seed_classification(db, "s4", "claude_code", "extraction")
    _seed_classification(db, "s5", "codex", "summarization")

    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    summary = build_task_summary(db, rng, provider_filter="codex")

    assert {r["provider"] for r in summary["raw_rows"]} == {"codex"}
    assert {r["task_category"] for r in summary["by_task"]} == {"summarization"}


def test_override_takes_precedence_over_classification(tmp_path):
    db = open_db(tmp_path / "t.sqlite")
    ev = _ev(id="d", session_id="s4")
    ev.estimated_cost_usd = 10.0
    upsert_events(db, [ev])
    # Classifier said feature_implementation, user said extraction.
    _seed_classification(db, "s4", "claude_code", "feature_implementation")
    db["session_overrides"].insert(
        {
            "session_id": "s4",
            "provider": "claude_code",
            "task_category": "extraction",
            "note": "manual",
            "set_at": "2026-04-15T10:00:00Z",
        },
        pk=("session_id", "provider"),
        replace=True,
    )

    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    summary = build_task_summary(db, rng)
    cats = {r["task_category"] for r in summary["by_task"]}
    assert "extraction" in cats
    assert "feature_implementation" not in cats
