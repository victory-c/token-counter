from __future__ import annotations

from pathlib import Path

import yaml

from tokenburn.classifier.engine import classify_range
from tokenburn.classifier.taxonomy import CLASSIFIER_VERSION
from tokenburn.config import AppConfig, DEFAULT_CONFIG_TEMPLATE
from tokenburn.db import open_db


def _cfg_pointing_at_fixtures(tmp_path: Path) -> AppConfig:
    raw = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    raw["db_path"] = str(tmp_path / "tokenburn.sqlite")
    raw["timezone"] = "UTC"
    raw["providers"]["claude_code"]["paths"] = [
        str(Path(__file__).parent / "fixtures" / "claude")
    ]
    raw["providers"]["codex"]["paths"] = [
        str(Path(__file__).parent / "fixtures" / "codex" / "sessions")
    ]
    return AppConfig.model_validate(raw)


def test_classify_persists_one_row_per_session(tmp_path):
    cfg = _cfg_pointing_at_fixtures(tmp_path)
    db = open_db(tmp_path / "tokenburn.sqlite")

    report = classify_range(db, cfg)
    assert report.total() == 2  # one Claude session + one Codex session

    rows = list(db.query("SELECT * FROM session_classifications ORDER BY provider"))
    assert len(rows) == 2
    providers = {r["provider"] for r in rows}
    assert providers == {"claude_code", "codex"}
    for r in rows:
        assert r["classifier"] == "heuristic"
        assert r["classifier_version"] == CLASSIFIER_VERSION
        assert r["features_json"]  # non-empty


def test_classify_is_idempotent_without_reclassify(tmp_path):
    cfg = _cfg_pointing_at_fixtures(tmp_path)
    db = open_db(tmp_path / "tokenburn.sqlite")

    classify_range(db, cfg)
    second = classify_range(db, cfg)
    # Second run skips both already-classified sessions
    assert second.skipped_existing == 2


def test_reclassify_overwrites(tmp_path):
    cfg = _cfg_pointing_at_fixtures(tmp_path)
    db = open_db(tmp_path / "tokenburn.sqlite")

    classify_range(db, cfg)
    second = classify_range(db, cfg, reclassify=True)
    assert second.skipped_existing == 0
    rows = list(db.query("SELECT count(*) AS n FROM session_classifications"))
    assert rows[0]["n"] == 2  # still just two
