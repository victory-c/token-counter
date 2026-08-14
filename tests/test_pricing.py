from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import tokenburn
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


def test_match_reports_whether_the_rate_was_exact_or_guessed():
    table = PricingTable.load(default_pricing_path())
    when = datetime(2026, 8, 15)

    exact = table.match("claude_code", "claude-opus-5", when)
    assert exact is not None
    assert exact.kind == "exact"
    assert not exact.is_approximate
    assert exact.matched_model == "claude-opus-5"

    guessed = table.match("claude_code", "claude-sonnet-4-5-20251029", when)
    assert guessed is not None
    assert guessed.kind == "prefix"
    assert guessed.is_approximate
    # Names the row the rate actually came from, so the report can say so.
    assert guessed.matched_model == "claude-sonnet-4"

    assert table.match("claude_code", "totally-made-up-model", when) is None


def test_lookup_still_returns_the_row_for_existing_callers():
    # estimate_cost and by_task depend on lookup()'s shape; match() is additive.
    table = PricingTable.load(default_pricing_path())
    when = datetime(2026, 8, 15)
    for model in ("claude-opus-5", "claude-sonnet-4-5-20251029"):
        assert table.lookup("claude_code", model, when) == table.match("claude_code", model, when).row
    assert table.lookup("claude_code", "totally-made-up-model", when) is None


def test_prefix_fallback_understates_a_model_priced_off_a_shorter_row():
    """The gpt-5.6-sol regression, as data rather than prose.

    A model with no exact row silently inherits a shorter key's rate. That is
    the right default (better than $0) but it is a guess, and it was wrong by
    4x on input here — so `match` must flag it rather than let a report present
    the number as a quoted price.
    """
    table = PricingTable.load(default_pricing_path())
    when = datetime(2026, 8, 15)

    sol = table.match("codex", "gpt-5.6-sol", when)
    assert sol is not None and sol.kind == "exact", (
        "gpt-5.6-sol now has an exact row; if this fails the row was removed"
    )

    # An unknown sibling variant still falls back, and must be flagged.
    unknown = table.match("codex", "gpt-5.6-nova", when)
    assert unknown is not None, "prefix fallback should still price unknown variants"
    assert unknown.is_approximate
    assert unknown.matched_model != "gpt-5.6-nova"
    # And the guess is materially wrong — the whole reason it must be surfaced.
    assert unknown.row.input_per_million_usd < sol.row.input_per_million_usd, (
        "expected the shorter-prefix row to be cheaper than the real gpt-5.6 rate; "
        f"guessed {unknown.matched_model} at ${unknown.row.input_per_million_usd}/M "
        f"vs exact ${sol.row.input_per_million_usd}/M"
    )


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("claude_code", "claude-opus-5", (5.0, 25.0, 6.25, 0.5)),
        ("claude_code", "gpt-5.5", (5.0, 30.0, 0.0, 0.5)),
        ("codex", "gpt-5.3-codex", (1.75, 14.0, 0.0, 0.175)),
        ("codex", "gpt-5.4", (2.5, 15.0, 0.0, 0.25)),
        ("codex", "gpt-5.4-mini", (0.75, 4.5, 0.0, 0.075)),
        ("codex", "gpt-5.5", (5.0, 30.0, 0.0, 0.5)),
        ("codex", "gpt-5.6-sol", (5.0, 30.0, 6.25, 0.5)),
        ("codex", "gpt-5.6-terra", (2.5, 15.0, 3.125, 0.25)),
    ],
)
def test_corrected_model_rates(provider: str, model: str, expected: tuple[float, ...]):
    table = PricingTable.load(default_pricing_path())

    row = table.lookup(provider, model, datetime(2026, 8, 9))

    assert row is not None
    assert (
        row.input_per_million_usd,
        row.output_per_million_usd,
        row.cache_write_per_million_usd,
        row.cache_read_per_million_usd,
    ) == expected


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


def test_estimate_cost_does_not_double_charge_reasoning_tokens():
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
        output_tokens=1_000_000,
        reasoning_tokens=1_000_000,
    )
    assert estimate_cost(e, table) == 15.0


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


def test_packaged_pricing_file_exists():
    assert default_pricing_path().exists()
    assert (Path(tokenburn.__file__).with_name("pricing.yaml")).exists()
