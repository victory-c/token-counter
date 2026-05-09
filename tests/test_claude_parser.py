from __future__ import annotations

from datetime import date
from pathlib import Path

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.claude_code import ClaudeCodeAdapter
from tokenburn.config import ProviderConfig
from tokenburn.models import Confidence, DateRange


def test_claude_parser_filters_synthetic_and_out_of_range(tmp_app_config):
    cfg, _ = tmp_app_config
    fixture_root = Path(__file__).parent / "fixtures" / "claude"
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(fixture_root)])
    adapter = ClaudeCodeAdapter(cfg, pcfg)

    src = DiscoveredSource(provider="claude_code", path=fixture_root, kind="local_jsonl_dir", exists=True)
    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    events = list(adapter.parse(src, range_))

    assert len(events) == 2
    e1, e2 = sorted(events, key=lambda e: e.timestamp_start)
    assert e1.model == "claude-sonnet-4"
    assert e1.input_tokens == 1200
    assert e1.cache_read_tokens == 40000
    assert e1.confidence is Confidence.EXACT_FROM_LOCAL_LOG
    assert e1.project_path == "/Users/victorchun/demo"
    assert e2.cache_creation_tokens == 15000


def test_claude_parser_id_is_deterministic(tmp_app_config):
    cfg, _ = tmp_app_config
    fixture_root = Path(__file__).parent / "fixtures" / "claude"
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(fixture_root)])
    adapter = ClaudeCodeAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="claude_code", path=fixture_root, kind="local_jsonl_dir", exists=True)

    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    ids1 = sorted(e.id for e in adapter.parse(src, range_))
    ids2 = sorted(e.id for e in adapter.parse(src, range_))
    assert ids1 == ids2


def test_claude_parser_hashes_project_paths_without_storing_raw_path(tmp_app_config):
    cfg, _ = tmp_app_config
    cfg.privacy.hash_project_paths = True
    fixture_root = Path(__file__).parent / "fixtures" / "claude"
    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[str(fixture_root)])
    adapter = ClaudeCodeAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="claude_code", path=fixture_root, kind="local_jsonl_dir", exists=True)

    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    events = list(adapter.parse(src, range_))

    assert events
    assert {e.project_path for e in events} == {None}
    assert all(e.project_hash for e in events)
