from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tokenburn.config import DEFAULT_CONFIG_TEMPLATE, AppConfig


@pytest.fixture
def tmp_app_config(tmp_path: Path) -> tuple[AppConfig, Path]:
    cfg_path = tmp_path / "config.yaml"
    raw = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    raw["db_path"] = str(tmp_path / "tokenburn.sqlite")
    raw["timezone"] = "UTC"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = AppConfig.model_validate(raw)
    return cfg, cfg_path
