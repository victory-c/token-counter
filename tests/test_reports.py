from __future__ import annotations

from datetime import UTC, date, datetime

from tokenburn.db import open_db, upsert_events
from tokenburn.models import Confidence, DateRange, UsageEvent
from tokenburn.reports.markdown import render_csv, render_markdown
from tokenburn.reports.monthly import build_summary


def _make_event(**kw) -> UsageEvent:
    base = dict(
        id="evt",
        provider="claude_code",
        tool="claude_code",
        timestamp_start=datetime(2026, 4, 15, 10, tzinfo=UTC),
        timezone="UTC",
        source_type="local_jsonl",
        source_parser="claude_native_jsonl",
        confidence=Confidence.EXACT_FROM_LOCAL_LOG,
        model="claude-sonnet-4",
        session_id="s1",
        project_path="/Users/v/proj",
        input_tokens=1000,
        output_tokens=200,
        cache_creation_tokens=0,
        cache_read_tokens=4000,
        total_tokens=5200,
        estimated_cost_usd=0.12,
    )
    base.update(kw)
    return UsageEvent(**base)


def test_build_summary_aggregates_across_providers(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [
            _make_event(id="a"),
            _make_event(
                id="b",
                provider="codex",
                model="gpt-5.4",
                source_parser="codex_native_jsonl",
                input_tokens=500,
                output_tokens=80,
                cache_read_tokens=0,
                total_tokens=580,
                estimated_cost_usd=0.05,
            ),
            _make_event(
                id="c",
                timestamp_start=datetime(2026, 5, 1, 10, tzinfo=UTC),
            ),
        ],
    )
    summary = build_summary(db, DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30)), cfg)
    assert summary["totals"]["event_count"] == 2
    providers = {r["provider"]: r for r in summary["by_provider"]}
    assert providers["claude_code"]["tokens"] == 5200
    assert providers["codex"]["tokens"] == 580


def test_markdown_export_contains_headers(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(db, [_make_event(id="a")])
    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    summary = build_summary(db, rng, cfg)
    md = render_markdown(summary, rng, cfg)
    assert "# AI Coding Agent Token Burn Report" in md
    assert "## By Provider" in md
    assert "claude_code" in md


def test_csv_export_contains_provider_row(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(db, [_make_event(id="a")])
    rng = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    csv_text = render_csv(build_summary(db, rng, cfg))
    assert "section,provider" in csv_text
    assert "claude_code" in csv_text
