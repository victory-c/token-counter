from __future__ import annotations

from datetime import date
from pathlib import Path

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.cursor import CursorAdapter
from tokenburn.config import ProviderConfig
from tokenburn.models import Confidence, DateRange


def test_cursor_csv_import_handles_auto_and_cost_only(tmp_app_config):
    cfg, _ = tmp_app_config
    csv_path = Path(__file__).parent / "fixtures" / "cursor" / "april.csv"
    pcfg = ProviderConfig(enabled=True, source="manual_import", import_dir=str(csv_path.parent))
    adapter = CursorAdapter(cfg, pcfg)
    src = DiscoveredSource(provider="cursor", path=csv_path, kind="manual_import", exists=True)
    range_ = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
    events = list(adapter.parse(src, range_))

    assert len(events) == 3
    by_session = {e.session_id: e for e in events}

    s1 = by_session["s1"]
    assert s1.model == "claude-sonnet-4"
    assert s1.input_tokens == 1000
    assert s1.cache_read_tokens == 5000
    assert s1.billed_cost_usd == 0.10
    assert s1.confidence is Confidence.MANUAL_IMPORT

    s2 = by_session["s2"]
    assert s2.model == "cursor-auto"
    assert s2.model_alias is None

    s3 = by_session["s3"]
    assert s3.model == "cursor-auto"
    assert s3.input_tokens is None
    assert s3.output_tokens is None
    assert s3.billed_cost_usd == 0.40
    assert s3.confidence is Confidence.MANUAL_IMPORT
