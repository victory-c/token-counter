from __future__ import annotations

from datetime import datetime

from tokenburn.models import Confidence, UsageEvent
from tokenburn.pricing import PricingTable, default_pricing_path, estimate_cost


def test_pricing_lookup_picks_latest_effective_row():
    table = PricingTable.load(default_pricing_path())
    early = table.lookup("claude_code", "claude-sonnet-4", datetime(2025, 6, 1))
    later = table.lookup("claude_code", "claude-sonnet-4", datetime(2026, 4, 15))
    assert early is not None and later is not None
    assert later.effective_date >= early.effective_date


def test_pricing_lookup_prefix_match_for_versioned_model():
    table = PricingTable.load(default_pricing_path())
    # Claude often returns versioned ids like "claude-sonnet-4-5-20251029"; prefix match should still hit.
    row = table.lookup("claude_code", "claude-sonnet-4-5-20251029", datetime(2026, 4, 15))
    assert row is not None
    assert row.model == "claude-sonnet-4"


def test_estimate_cost_handles_all_token_dimensions():
    table = PricingTable.load(default_pricing_path())
    e = UsageEvent(
        id="x",
        provider="claude_code",
        tool="claude_code",
        timestamp_start=datetime(2026, 4, 15),
        timezone="UTC",
        source_type="local_jsonl",
        source_parser="claude_native_jsonl",
        confidence=Confidence.EXACT_FROM_LOCAL_LOG,
        model="claude-sonnet-4",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    cost = estimate_cost(e, table)
    # 3 + 15 + 3.75 + 0.30 = 22.05
    assert cost is not None
    assert abs(cost - 22.05) < 1e-6


def test_estimate_cost_unknown_model_returns_none():
    table = PricingTable.load(default_pricing_path())
    e = UsageEvent(
        id="x",
        provider="claude_code",
        tool="claude_code",
        timestamp_start=datetime(2026, 4, 15),
        timezone="UTC",
        source_type="local_jsonl",
        source_parser="claude_native_jsonl",
        confidence=Confidence.EXACT_FROM_LOCAL_LOG,
        model="nonexistent-7000",
        input_tokens=10,
    )
    assert estimate_cost(e, table) is None
