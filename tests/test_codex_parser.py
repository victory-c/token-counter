from __future__ import annotations

import json
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
    assert by_ts[2].input_tokens == 800
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
