from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from tokenburn.cli import app


def _patch_db_path(cfg_path: Path, db_path: Path) -> None:
    raw = yaml.safe_load(cfg_path.read_text())
    raw["db_path"] = str(db_path)
    raw["timezone"] = "UTC"
    cfg_path.write_text(yaml.safe_dump(raw))


def test_init_then_doctor(tmp_path):
    runner = CliRunner()
    cfg_path = tmp_path / "tb.yaml"
    r = runner.invoke(app, ["init", "--path", str(cfg_path)])
    assert r.exit_code == 0
    _patch_db_path(cfg_path, tmp_path / "tb.sqlite")
    r = runner.invoke(app, ["doctor", "--config", str(cfg_path)])
    assert r.exit_code == 0
    assert "tokencounter version" in r.output
    assert "config" in r.output


def test_import_cursor_then_report_then_export(tmp_path):
    runner = CliRunner()
    cfg_path = tmp_path / "tb.yaml"
    runner.invoke(app, ["init", "--path", str(cfg_path)])
    _patch_db_path(cfg_path, tmp_path / "tb.sqlite")

    fixture = Path(__file__).parent / "fixtures" / "cursor" / "april.csv"
    r = runner.invoke(app, ["import", "cursor", str(fixture), "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert "Imported 3 events" in r.output

    r = runner.invoke(app, ["report", "--month", "2026-04", "--provider", "cursor", "--config", str(cfg_path), "--no-scan"])
    assert r.exit_code == 0, r.output
    assert "cursor" in r.output

    out_md = tmp_path / "report.md"
    r = runner.invoke(app, ["export", "--month", "2026-04", "--format", "markdown", "--output", str(out_md), "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert out_md.exists()
    text = out_md.read_text()
    assert "# AI Coding Agent Token Burn Report" in text
    assert "cursor" in text
