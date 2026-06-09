from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from tokenburn.cli import app
from tokenburn.db import open_db, upsert_events
from tokenburn.models import Confidence, DateRange, UsageEvent
from tokenburn.reports.dashboard import build_dashboard_payload, render_dashboard_html


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


def _seed(db) -> None:
    upsert_events(
        db,
        [
            _make_event(id="a", session_id="s1"),
            _make_event(id="b", session_id="s1", input_tokens=500, total_tokens=500, estimated_cost_usd=0.03),
            _make_event(
                id="c",
                provider="cursor",
                tool="cursor",
                source_type="manual_import",
                source_parser="cursor_csv",
                confidence=Confidence.MANUAL_IMPORT,
                model="cursor-auto",
                session_id="s2",
                project_path="/Users/v/other",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                total_tokens=900_000,
                estimated_cost_usd=0.0,
            ),
        ],
    )


def _april() -> DateRange:
    return DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))


def test_payload_aggregates_sessions(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    _seed(db)
    payload = build_dashboard_payload(db, _april(), cfg)

    sessions = {s["session"]: s for s in payload["sessions"]}
    # Two events in s1 collapse into one session row with summed tokens.
    assert sessions["s1"]["total_tokens"] == 5700
    assert sessions["s1"]["events"] == 2
    assert sessions["s1"]["provider"] == "claude_code"
    assert sessions["s2"]["provider"] == "cursor"
    assert sessions["s2"]["confidence"] == "manual_import"


def test_payload_redacts_home(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config  # redact_home_dir defaults to True
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(db, [_make_event(id="a", project_path=str(Path.home() / "secret/proj"))])
    payload = build_dashboard_payload(db, _april(), cfg)
    projects = [s["project"] for s in payload["sessions"]]
    assert any(p and p.startswith("~/") for p in projects)
    assert not any(p and str(Path.home()) in p for p in projects)


def test_payload_subscription_mapping_and_warning(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    _seed(db)
    payload = build_dashboard_payload(db, _april(), cfg)

    subs = {s["name"]: s for s in payload["subscriptions"]}
    assert "claude_pro" in subs
    assert subs["claude_pro"]["monthly_cost_usd"] == 20
    # cursor session has huge token count and manual_import confidence ->
    # soft-confidence share is high, so a data-quality warning is emitted.
    assert any("estimated or manually imported" in w for w in payload["warnings"])


def test_render_html_is_self_contained(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    _seed(db)
    payload = build_dashboard_payload(db, _april(), cfg)
    html = render_dashboard_html(payload)

    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert 'id="tokenburn-data"' in html
    # No external resource loads — fully offline.
    assert "http://" not in html.replace("http://www.w3.org", "")  # svg ns is fine
    assert "src=" not in html
    # API-equivalent cost labelled as estimate.
    assert "API-equivalent estimate" in html
    # Embedded JSON must not prematurely close the script tag.
    assert "</script>" in html  # the real closer exists
    block = re.search(r'type="application/json">(.*?)</script>', html, re.DOTALL)
    assert block, "embedded JSON block missing"
    # The escaped payload should round-trip after un-escaping.
    restored = block.group(1).replace("<\\/", "</")
    data = json.loads(restored)
    assert data["sessions"]


def _init_cfg(runner: CliRunner, tmp_path: Path) -> Path:
    cfg_path = tmp_path / "tb.yaml"
    runner.invoke(app, ["init", "--path", str(cfg_path)])
    raw = yaml.safe_load(cfg_path.read_text())
    raw["db_path"] = str(tmp_path / "tb.sqlite")
    raw["timezone"] = "UTC"
    cfg_path.write_text(yaml.safe_dump(raw))
    return cfg_path


def test_cli_dashboard_writes_file(tmp_path):
    runner = CliRunner()
    cfg_path = _init_cfg(runner, tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "cursor" / "april.csv"
    runner.invoke(app, ["import", "cursor", str(fixture), "--config", str(cfg_path)])

    out = tmp_path / "dash.html"
    r = runner.invoke(
        app,
        ["dashboard", "--month", "2026-04", "--output", str(out), "--config", str(cfg_path)],
    )
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert "tokenburn-data" in out.read_text()
    assert "Generated interactive HTML dashboard" in r.output


def test_cli_export_month_writes_both(tmp_path):
    runner = CliRunner()
    cfg_path = _init_cfg(runner, tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "cursor" / "april.csv"
    runner.invoke(app, ["import", "cursor", str(fixture), "--config", str(cfg_path)])

    r = runner.invoke(
        app,
        ["export-month", "--month", "2026-04", "--output-dir", str(tmp_path), "--config", str(cfg_path)],
    )
    assert r.exit_code == 0, r.output
    md = tmp_path / "tokenburn-report-2026-04.md"
    html = tmp_path / "tokenburn-dashboard-2026-04.html"
    assert md.exists() and html.exists()
    assert "# AI Coding Agent Token Burn Report" in md.read_text()
    assert "tokenburn-data" in html.read_text()
