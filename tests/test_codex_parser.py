from __future__ import annotations

from datetime import date
from pathlib import Path

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.codex import CodexAdapter
from tokenburn.config import ProviderConfig
from tokenburn.models import Confidence, DateRange


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

    # First event: last_token_usage present
    assert by_ts[0].input_tokens == 1000
    assert by_ts[0].output_tokens == 100
    assert by_ts[0].cache_read_tokens == 500
    assert by_ts[0].reasoning_tokens == 50
    assert by_ts[0].confidence is Confidence.EXACT_FROM_LOCAL_LOG
    assert by_ts[0].model == "gpt-5.4"
    assert by_ts[0].project_path == "/Users/victorchun/demo"

    # Second: last_token_usage shows the per-turn delta, not cumulative
    assert by_ts[1].input_tokens == 1200
    assert by_ts[1].output_tokens == 150
    assert by_ts[1].confidence is Confidence.EXACT_FROM_LOCAL_LOG

    # Third: only total_token_usage; falls back and labels confidence
    assert by_ts[2].confidence is Confidence.ESTIMATED_FROM_SESSION_SUMMARY


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
