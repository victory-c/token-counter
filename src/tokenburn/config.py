from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_DIR = Path.home() / ".tokenburn"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_DB_PATH = DEFAULT_CONFIG_DIR / "tokenburn.sqlite"


class ProviderConfig(BaseModel):
    enabled: bool = True
    source: str
    paths: list[str] = Field(default_factory=list)
    import_dir: str | None = None
    use_ccusage_if_available: bool = False
    use_ccusage_codex_if_available: bool = False


class PricingConfig(BaseModel):
    mode: str = "api_equivalent"
    currency: str = "USD"
    pricing_cache_ttl_days: int = 7
    # Explicit pricing table location. Unset, the table is resolved from the
    # repo checkout first — which makes every cost a function of whichever
    # branch happens to be checked out, and silently re-prices the database on
    # the next scan. Set this to pin one table regardless of branch.
    path: str | None = None


class PrivacyConfig(BaseModel):
    store_raw_prompts: bool = False
    store_raw_messages: bool = False
    hash_project_paths: bool = False
    redact_home_dir: bool = True
    redact_project_paths: bool = False


class SubscriptionConfig(BaseModel):
    monthly_cost_usd: float
    providers: list[str]


class DashboardConfig(BaseModel):
    enabled: bool = True
    auto_open: bool = False
    default_metric: str = "total_tokens"
    include_session_table: bool = True
    max_table_rows: int = 5000
    # Share of usage in estimated/manual/unavailable confidence buckets above
    # which the dashboard shows a data-quality warning.
    confidence_warning_threshold: float = 0.25
    # Share of total tokens held by the single top provider above which the
    # dashboard flags provider concentration.
    provider_concentration_threshold: float = 0.70


class ExportsConfig(BaseModel):
    default_output_dir: str = "~/Downloads"
    markdown_filename_pattern: str = "tokenburn-report-{label}.md"
    dashboard_filename_pattern: str = "tokenburn-dashboard-{label}.html"


class AppConfig(BaseModel):
    version: int = 1
    timezone: str = "America/Los_Angeles"
    db_path: str = str(DEFAULT_DB_PATH)
    providers: dict[str, ProviderConfig]
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    subscriptions: dict[str, SubscriptionConfig] = Field(default_factory=dict)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    exports: ExportsConfig = Field(default_factory=ExportsConfig)


DEFAULT_CONFIG_TEMPLATE = """\
version: 1
timezone: America/Los_Angeles
db_path: ~/.tokenburn/tokenburn.sqlite

providers:
  claude_code:
    enabled: true
    source: local_jsonl
    paths:
      - ~/.claude/projects
    use_ccusage_if_available: true

  codex:
    enabled: true
    source: local_jsonl
    paths:
      - ~/.codex/sessions
    use_ccusage_codex_if_available: true

  cursor:
    enabled: true
    source: manual_import
    import_dir: ~/.tokenburn/imports/cursor

  gemini:
    enabled: true
    source: local_or_imported_logs
    import_dir: ~/.tokenburn/imports/gemini

pricing:
  mode: api_equivalent
  currency: USD
  pricing_cache_ttl_days: 7
  # path: ~/.tokenburn/pricing.yaml   # pin the table; ignores the checkout

privacy:
  store_raw_prompts: false
  store_raw_messages: false
  hash_project_paths: false
  redact_home_dir: true
  redact_project_paths: false

subscriptions:
  claude_pro:
    monthly_cost_usd: 20
    providers: [claude_code]
  chatgpt_plus:
    monthly_cost_usd: 20
    providers: [codex]
  cursor_pro:
    monthly_cost_usd: 20
    providers: [cursor]
  google_pro:
    monthly_cost_usd: 20
    providers: [gemini]

dashboard:
  enabled: true
  auto_open: false
  default_metric: total_tokens
  include_session_table: true
  max_table_rows: 5000
  confidence_warning_threshold: 0.25
  provider_concentration_threshold: 0.70

exports:
  default_output_dir: ~/Downloads
  markdown_filename_pattern: tokenburn-report-{label}.md
  dashboard_filename_pattern: tokenburn-dashboard-{label}.html
"""


def expand(p: str | Path) -> Path:
    return Path(str(p)).expanduser().resolve()


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No config at {cfg_path}. Run `tokencounter init` to create one."
        )
    with cfg_path.open() as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def write_default_config(path: Path | None = None) -> Path:
    cfg_path = path or DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(DEFAULT_CONFIG_TEMPLATE)
    return cfg_path
