from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

import yaml
from typer.testing import CliRunner

from tokenburn.cli import app
from tokenburn.db import open_db, upsert_events
from tokenburn.models import Confidence, DateRange, UsageEvent
from tokenburn.reports.dashboard import (
    _HTML_TEMPLATE,
    build_dashboard_payload,
    render_dashboard_html,
    render_static_fallback,
)


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


# A string that, if it ever reached the page unescaped/un-neutralized, would
# both break out of the JSON <script> island and fire an XSS payload.
_XSS = "</script><img src=x onerror=alert(1)>'\"&"


class _TagCollector(HTMLParser):
    """Collects every element the browser would actually construct.

    `HTMLParser` treats <script> content as raw text, so tags that appear only
    inside the JSON island are correctly *not* reported — which is the point:
    this asserts on the real element tree, not on byte presence.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D102
        self.tags.append(tag)

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        self.tags.append(tag)


def test_render_neutralizes_script_breakout_from_user_data(tmp_path, tmp_app_config):
    # Log-derived fields (project path, model, session id) are attacker-
    # influenceable, and now reach the page through TWO sinks: the <script
    # type="application/json"> island (defended by the "</" -> "<\/"
    # neutralization) and the server-rendered static fallback (defended by
    # _esc/html.escape). Lock both down.
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [_make_event(id="x", model=_XSS, session_id=_XSS, project_path="/tmp/" + _XSS)],
    )
    payload = build_dashboard_payload(db, _april(), cfg)
    html = render_dashboard_html(payload)

    block = re.search(r'type="application/json">(.*?)</script>', html, re.DOTALL)
    assert block, "embedded JSON block missing"
    island = block.group(1)
    # Every "</" inside the island is neutralized, so the hostile data cannot
    # prematurely close the script tag; the island still parses as JSON.
    assert "</" not in island
    assert json.loads(island.replace("<\\/", "</"))["sessions"]

    # The hostile payload must never become a real element. Parsing the whole
    # document is stricter than substring checks: it proves no <img> (or any
    # other injected tag) is constructed anywhere outside the script islands.
    collector = _TagCollector()
    collector.feed(html)
    assert "img" not in collector.tags
    assert "iframe" not in collector.tags
    # Exactly the two script islands the template declares — no third one was
    # smuggled in by breaking out of an attribute or text node.
    assert collector.tags.count("script") == 2

    # The static fallback renders the hostile text, but inert: angle brackets
    # and quotes are entity-escaped rather than dropped (dropping would hide
    # data; escaping keeps it readable and safe).
    outside = html[: block.start(1)] + html[block.end(1) :]
    assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in outside
    assert "<img" not in outside
    assert "</script><img" not in outside


def test_static_fallback_carries_real_numbers_without_js(tmp_path, tmp_app_config):
    # The regression this guards: every panel used to be an empty div filled in
    # by JS, so any environment that does not run scripts (preview panes,
    # snapshot renderers, email clients, CSP-restricted viewers) showed a page
    # of headings with no data at all.
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    _seed(db)
    payload = build_dashboard_payload(db, _april(), cfg)
    static = render_static_fallback(payload)

    # Real aggregates, server-rendered.
    assert "5,700" in static  # summed s1 tokens
    assert "claude_code" in static and "cursor" in static
    assert "$0.15" in static  # 0.12 + 0.03 API-equiv cost
    # Session-level rows are present without any scripting.
    assert "s1" in static and "s2" in static
    # And it explains itself rather than looking broken.
    assert "Static view" in static


def test_static_view_visible_and_app_hidden_until_js_boots(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    _seed(db)
    html = render_dashboard_html(build_dashboard_payload(db, _april(), cfg))

    # Shipped state: static visible, interactive hidden.
    assert re.search(r'<div id="tb-app" hidden>', html)
    assert re.search(r'<div id="tb-static">', html)
    # [hidden] must win against .cards/.grid2 display rules.
    assert "[hidden]{display:none !important}" in html
    # The swap happens only after init() completes, and a throw restores the
    # static view instead of leaving a blank page.
    assert "document.getElementById('tb-app').hidden = false;" in html
    assert "document.getElementById('tb-static').hidden = true;" in html
    assert "catch (err)" in html


def test_static_fallback_escapes_log_derived_fields(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    upsert_events(
        db,
        [_make_event(id="x", model=_XSS, session_id=_XSS, project_path="/tmp/" + _XSS)],
    )
    static = render_static_fallback(build_dashboard_payload(db, _april(), cfg))
    # No live markup survives; the raw text is preserved but entity-escaped.
    assert "<img" not in static
    assert "<script" not in static
    assert "&lt;img src=x onerror=alert(1)&gt;" in static
    assert "&quot;" in static and "&#x27;" in static


def test_static_fallback_handles_empty_range(tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    db = open_db(tmp_path / "t.sqlite")
    static = render_static_fallback(build_dashboard_payload(db, _april(), cfg))
    assert "No usage events in this range." in static
    assert "Static view" in static


def test_user_controlled_fields_are_escaped_in_dashboard_js():
    # All rendering happens client-side via innerHTML, so every interpolation
    # of an attacker-influenceable field must pass through esc(). This catches
    # a future field being wired into innerHTML without escaping (the residual
    # XSS risk for this dashboard). Static, app-controlled values (column
    # labels, numeric formatters) are intentionally not required to be escaped.
    #
    # These accessors are user/log-controlled text that only ever reaches the
    # page through an innerHTML template literal (never textContent), so every
    # interpolation referencing one must wrap it in esc().
    user_field_tokens = (
        "p[0]",  # project name (top-projects + donut)
        "p.model",
        "p.provider",
        "p.source_url",
        "pv",  # provider (composition chart)
        "d.name",  # subscription name
        "r.model",
        "r.provider",
        "r.project",
        "r.session",
    )
    interpolations = re.findall(r"\$\{([^{}]+)\}", _HTML_TEMPLATE)
    offenders = [
        expr
        for expr in interpolations
        if any(tok in expr for tok in user_field_tokens) and "esc(" not in expr
    ]
    assert not offenders, f"un-escaped user-controlled interpolations: {offenders}"
    # Fields escaped at their innerHTML site, asserted directly: the metadata
    # line (generated_at/timezone), caveat keys/values (may echo provider/model
    # names), and warnings. `k`/`v`/`w` are too generic to token-match safely,
    # and DATA.meta.timezone also appears in a textContent sink (safe) above.
    for site in ("esc(DATA.meta.generated_at)", "esc(DATA.meta.timezone)",
                 "esc(k)", "esc(v)", "esc(w)"):
        assert site in _HTML_TEMPLATE, f"missing expected escaping site: {site}"


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
        ["dashboard", "--month", "2026-04", "--output", str(out), "--config", str(cfg_path), "--no-scan"],
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
        ["export-month", "--month", "2026-04", "--output-dir", str(tmp_path), "--config", str(cfg_path), "--no-scan"],
    )
    assert r.exit_code == 0, r.output
    md = tmp_path / "tokenburn-report-2026-04.md"
    html = tmp_path / "tokenburn-dashboard-2026-04.html"
    assert md.exists() and html.exists()
    assert "# AI Coding Agent Token Burn Report" in md.read_text()
    assert "tokenburn-data" in html.read_text()


def _init_cfg_claude_fixture(runner: CliRunner, tmp_path: Path) -> Path:
    """Config whose only enabled provider points at the bundled Claude fixture,
    so a default-scan stays hermetic (no real ~/.claude access)."""
    cfg_path = tmp_path / "tb.yaml"
    runner.invoke(app, ["init", "--path", str(cfg_path)])
    raw = yaml.safe_load(cfg_path.read_text())
    raw["db_path"] = str(tmp_path / "tb.sqlite")
    raw["timezone"] = "UTC"
    claude_dir = Path(__file__).parent / "fixtures" / "claude"
    raw["providers"]["claude_code"]["paths"] = [str(claude_dir)]
    for name in ("codex", "cursor", "gemini"):
        raw["providers"][name]["enabled"] = False
    cfg_path.write_text(yaml.safe_dump(raw))
    return cfg_path


def test_cli_dashboard_scans_by_default(tmp_path):
    runner = CliRunner()
    cfg_path = _init_cfg_claude_fixture(runner, tmp_path)

    out = tmp_path / "dash.html"
    r = runner.invoke(
        app,
        ["dashboard", "--month", "2026-04", "--output", str(out), "--config", str(cfg_path)],
    )
    assert r.exit_code == 0, r.output
    assert "No usage events found" not in r.output
    # The fixture's tokens made it into the embedded payload via the auto-scan.
    data = json.loads(re.search(r'type="application/json">(.*?)</script>', out.read_text(), re.S).group(1).replace("<\\/", "</"))
    assert data["sessions"], "auto-scan should have ingested the Claude fixture"


def test_cli_dashboard_no_scan_warns_when_empty(tmp_path):
    runner = CliRunner()
    cfg_path = _init_cfg(runner, tmp_path)

    out = tmp_path / "dash.html"
    r = runner.invoke(
        app,
        ["dashboard", "--month", "2026-04", "--output", str(out), "--config", str(cfg_path), "--no-scan"],
    )
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert "No usage events found" in r.output
    assert "Sources checked" in r.output
