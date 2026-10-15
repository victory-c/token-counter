from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.codex import CodexAdapter
from tokenburn.config import ProviderConfig
from tokenburn.models import Confidence, DateRange
from tokenburn.pricing import PriceRow, PricingTable, estimate_cost


def test_codex_uses_last_token_usage_when_present(tmp_app_config):
    cfg, _ = tmp_app_config
    fixture_root = Path(__file__).parent / "fixtures" / "codex" / "sessions"
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(fixture_root)])
    adapter = CodexAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="codex", path=fixture_root, kind="local_rollout_dir", exists=True)
    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    events = list(adapter.parse(src, range_))

    assert len(events) == 3
    by_ts = sorted(events, key=lambda e: e.timestamp_start)

    # First event: last_token_usage present. input_tokens is the uncached
    # part: Codex's 1000 input includes the 500 cached.
    assert by_ts[0].input_tokens == 500
    assert by_ts[0].output_tokens == 100
    assert by_ts[0].cache_read_tokens == 500
    assert by_ts[0].reasoning_tokens == 50
    assert by_ts[0].confidence is Confidence.EXACT_FROM_LOCAL_LOG
    assert by_ts[0].model == "gpt-5.4"
    assert by_ts[0].project_path == "/Users/victorchun/demo"

    # Second: last_token_usage shows the per-turn delta, not cumulative
    assert by_ts[1].input_tokens == 200
    assert by_ts[1].output_tokens == 150
    assert by_ts[1].confidence is Confidence.EXACT_FROM_LOCAL_LOG

    # Third: only total_token_usage; falls back and labels confidence
    assert by_ts[2].confidence is Confidence.ESTIMATED_FROM_SESSION_SUMMARY
    assert by_ts[2].input_tokens == 100
    assert by_ts[2].output_tokens == 150
    assert by_ts[2].cache_read_tokens == 700
    assert by_ts[2].reasoning_tokens == 80
    assert by_ts[2].total_tokens == 950


def test_codex_id_is_deterministic(tmp_app_config):
    cfg, _ = tmp_app_config
    fixture_root = Path(__file__).parent / "fixtures" / "codex" / "sessions"
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(fixture_root)])
    adapter = CodexAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="codex", path=fixture_root, kind="local_rollout_dir", exists=True)
    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    a = sorted(e.id for e in adapter.parse(src, range_))
    b = sorted(e.id for e in adapter.parse(src, range_))
    assert a == b


def _write_forked_rollout(root: Path) -> Path:
    """A subagent thread: token_count events stream BEFORE the first turn_context.

    This is the real shape of a Codex Desktop thread forked into a subagent —
    it resumes the parent's context, so usage is reported long before the
    child's first turn begins. Two such logs put 336 events (42.6M tokens) on
    "no model / no project", which priced them at $0.
    """
    day = root / "2026" / "04" / "15"
    day.mkdir(parents=True, exist_ok=True)
    path = day / "rollout-2026-04-15T10-00-00-forked.jsonl"
    lines = [
        {
            "timestamp": "2026-04-15T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "forked-1",
                "cwd": "/Users/v/forked-project",
                "thread_source": "subagent",
                "model_provider": "openai",
                # Real session_meta carries cwd but NO model at all.
            },
        },
        # Usage arrives BEFORE any turn_context.
        {
            "timestamp": "2026-04-15T10:01:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 700,
                        "cached_input_tokens": 200,
                        "output_tokens": 70,
                        "reasoning_output_tokens": 7,
                        "total_tokens": 777,
                    }
                },
            },
        },
        # The model is first *mentioned* only here, after usage was reported.
        {
            "timestamp": "2026-04-15T10:01:30.000Z",
            "type": "event_msg",
            "payload": {"type": "thread_meta", "thread_settings": {"model": "gpt-5.4"}},
        },
        # ...and only later does the child's first turn start.
        {
            "timestamp": "2026-04-15T10:02:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4-mini", "cwd": "/Users/v/later-cwd"},
        },
        {
            "timestamp": "2026-04-15T10:03:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 110,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return root


def test_forked_subagent_usage_is_attributed_before_first_turn_context(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    root = _write_forked_rollout(tmp_path / "sessions")
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(root)])
    adapter = CodexAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="codex", path=root, kind="local_rollout_dir", exists=True)
    events = sorted(
        adapter.parse(src, DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))),
        key=lambda e: e.timestamp_start,
    )
    assert len(events) == 2

    # The pre-turn_context event is the regression: it used to have model=None
    # and project=None, so it was priced at $0 and grouped as "(unknown)".
    early = events[0]
    assert early.model == "gpt-5.4", "usage before the first turn_context lost its model"
    assert early.project_path == "/Users/v/forked-project"
    assert early.total_tokens == 777

    # turn_context still wins once it appears — seeding must not override it.
    late = events[1]
    assert late.model == "gpt-5.4-mini"
    assert late.project_path == "/Users/v/later-cwd"


def _write_cached_rollout(root: Path) -> Path:
    """A gpt-5.5 thread whose turns mostly hit the prompt cache.

    Codex reports usage in the OpenAI shape: `cached_input_tokens` is a
    *subset* of `input_tokens` (total_tokens == input + output; upstream
    Codex's own `non_cached_input()` is `input - cached`). The second turn has
    only `total_token_usage`, so it exercises the cumulative-fallback path.
    """
    day = root / "2026" / "04" / "20"
    day.mkdir(parents=True, exist_ok=True)
    path = day / "rollout-2026-04-20T09-00-00-cached.jsonl"
    first = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 900_000,
        "output_tokens": 10_000,
        "reasoning_output_tokens": 0,
        "total_tokens": 1_010_000,
    }
    lines = [
        {
            "timestamp": "2026-04-20T09:00:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5", "cwd": "/Users/v/cached-project"},
        },
        {
            "timestamp": "2026-04-20T09:01:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": first, "last_token_usage": first},
            },
        },
        {
            "timestamp": "2026-04-20T09:02:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1_600_000,
                        "cached_input_tokens": 1_400_000,
                        "output_tokens": 30_000,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 1_630_000,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return root


def test_cached_input_is_billed_once_at_the_cache_rate(tmp_path, tmp_app_config):
    """Cached tokens used to be billed at the full input rate *and* the cache rate.

    The adapter stored Codex's whole `input_tokens` as input and the same
    cached tokens again as cache_read, so estimate_cost charged every cached
    token twice — about 6.4x the real Codex cost on real rollouts.
    """
    cfg, _ = tmp_app_config
    root = _write_cached_rollout(tmp_path / "sessions")
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(root)])
    adapter = CodexAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="codex", path=root, kind="local_rollout_dir", exists=True)
    events = sorted(
        adapter.parse(src, DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))),
        key=lambda e: e.timestamp_start,
    )
    assert len(events) == 2
    exact, fallback = events

    # gpt-5.5 standard tier: $5 input / $30 output / $0.50 cache read per M.
    table = PricingTable(
        [
            PriceRow(
                provider="codex",
                model="gpt-5.5",
                effective_date=date(2026, 3, 1),
                input_per_million_usd=5.0,
                output_per_million_usd=30.0,
                cache_write_per_million_usd=0.0,
                cache_read_per_million_usd=0.50,
                source_url="",
            )
        ]
    )

    # last_token_usage path: 100k uncached x $5 + 900k cached x $0.50
    # + 10k output x $30 = $0.50 + $0.45 + $0.30 = $1.25 (was $5.75).
    assert exact.confidence is Confidence.EXACT_FROM_LOCAL_LOG
    assert exact.input_tokens == 100_000
    assert exact.cache_read_tokens == 900_000
    assert exact.output_tokens == 10_000
    assert exact.total_tokens == 1_010_000
    assert estimate_cost(exact, table) == pytest.approx(1.25)

    # Cumulative-fallback path: deltas are input 600k / cached 500k / output
    # 20k, so 100k uncached x $5 + 500k x $0.50 + 20k x $30 = $1.35 (was $3.85).
    assert fallback.confidence is Confidence.ESTIMATED_FROM_SESSION_SUMMARY
    assert fallback.input_tokens == 100_000
    assert fallback.cache_read_tokens == 500_000
    assert fallback.output_tokens == 20_000
    assert fallback.total_tokens == 620_000
    assert estimate_cost(fallback, table) == pytest.approx(1.35)

    # Input and cache read are now disjoint, like every other adapter's.
    for ev in events:
        assert ev.input_tokens + ev.cache_read_tokens + ev.output_tokens == ev.total_tokens
