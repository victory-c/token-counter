from __future__ import annotations

from datetime import date
from pathlib import Path

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.gemini import GeminiAdapter
from tokenburn.config import ProviderConfig
from tokenburn.models import Confidence, DateRange


def test_gemini_jsonl_import_maps_usage_metadata(tmp_app_config):
    cfg, _ = tmp_app_config
    path = Path(__file__).parent / "fixtures" / "gemini" / "april.jsonl"
    pcfg = ProviderConfig(enabled=True, source="local_or_imported_logs", import_dir=str(path.parent))
    adapter = GeminiAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="gemini", path=path, kind="manual_import", exists=True)
    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    events = list(adapter.parse(src, range_))

    assert len(events) == 2  # third row is out of range
    e = sorted(events, key=lambda x: x.timestamp_start)[0]
    assert e.model == "gemini-2.5-pro"
    assert e.input_tokens == 15000
    assert e.output_tokens == 1200
    assert e.cache_read_tokens == 0
    assert e.total_tokens == 16200
    assert e.confidence is Confidence.EXACT_FROM_PROVIDER_LOG
