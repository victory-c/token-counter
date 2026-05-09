# PRD: AI Coding Agent Token Burn Auditor

## 1. Product Name

**TokenBurn Ledger**

A local-first CLI/dashboard that summarizes monthly AI coding-agent token usage across:

- Claude Code / Claude Pro
- OpenAI Codex / ChatGPT Plus
- Cursor / Cursor Pro, including model-level usage where visible
- Gemini CLI / Gemini Code Assist / Gemini API usage
- Future providers via adapters

---

## 2. Problem Statement

Power users of AI coding agents burn extremely large amounts of tokens across multiple subscriptions, but each vendor exposes usage differently.

Claude Code writes local JSONL data that tools like `ccusage` can analyze; `ccusage` supports daily, weekly, monthly, session, and 5-hour block reports from Claude Code local JSONL files.

Codex CLI also has local session logs under `CODEX_HOME`, defaulting to `~/.codex`, and `@ccusage/codex` can convert running token totals into daily/monthly deltas, but the Codex companion tool is explicitly experimental.

Cursor moved Pro toward usage-credit accounting for frontier models, with $20 of included monthly frontier model usage and model/API-price-based billing semantics; Cursor also says Auto usage is treated differently from explicit frontier model usage.

Gemini exposes official token counting through `count_tokens`, and Google AI Studio can enable/view request logs for `GenerateContent` and `StreamGenerateContent` calls.

The user problem is not just “how many tokens?” It is:

> “How much compute did I consume across all my coding agents last month, by tool, model, project, day, and approximate API-equivalent dollar value?”

---

## 3. Goals

### Primary Goals

1. Generate a monthly report of token usage across Claude Code, Codex, Cursor, and Gemini.
2. Break down usage by:
   - Provider
   - Tool
   - Model
   - Project/repository
   - Day
   - Session
   - Input tokens
   - Output tokens
   - Cache read/write tokens where available
   - Estimated API-equivalent cost
   - Subscription source: Claude Pro, ChatGPT Plus, Cursor Pro, Google Pro/API
3. Provide **confidence labels** for every number:
   - `exact_from_provider_log`
   - `exact_from_local_log`
   - `estimated_from_session_summary`
   - `estimated_from_text_retokenization`
   - `manual_import`
   - `unavailable`
4. Work locally without uploading private code or conversation logs by default.
5. Produce exportable reports:
   - Terminal table
   - JSON
   - CSV
   - Markdown monthly report
   - Optional local HTML dashboard

### Non-Goals for MVP

1. Do not scrape private dashboards with credentials by default.
2. Do not bypass vendor rate limits, CAPTCHAs, or access controls.
3. Do not claim exact token counts for Cursor unless the data came from Cursor’s own usage dashboard or a supported export.
4. Do not estimate true vendor cost from subscription usage unless clearly labeled as “API-equivalent cost,” not “what Anthropic/OpenAI/Google actually paid.”

---

## 4. Target User

### Primary Persona

**AI-native student/founder/developer using multiple coding agents heavily**

Traits:

- Uses Claude Code, Codex, Cursor, and Gemini.
- Cares about “did I make my subscription money back?”
- Wants monthly burn summaries.
- Wants to know which tools/models/projects are token black holes.
- Is comfortable running CLI scripts locally.
- May use macOS, Linux, or remote HPC/dev servers.

### Secondary Persona

**Small engineering team lead**

Wants to understand AI coding-agent usage by repo, model, engineer, and cost center.

---

## 5. Key Product Insight

This product should not force all vendors into the same fake abstraction. Token accounting varies:

| Platform | Best Data Source | Reliability | Notes |
|---|---:|---:|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, ccusage, optional statusline/telemetry | High, with caveats | `ccusage` reads Claude Code JSONL and reports daily/monthly/session/block usage. |
| Codex CLI | `~/.codex` JSONL/session logs, `@ccusage/codex` | Medium-high | Experimental parser; logs contain running token totals converted into deltas. |
| Cursor | Cursor dashboard usage/billing export or manual CSV | Medium | Cursor Pro uses monthly usage credits and model/API-price-based accounting. |
| Gemini | API responses, `count_tokens`, AI Studio logs, Cloud logs | High for API; lower for consumer app | Gemini supports token counting and AI Studio logging for generation calls. |

---

## 6. MVP User Stories

### US-1: Generate Monthly Token Report

As a user, I want to run:

```bash
tokenburn report --month 2026-04
```

So that I get a monthly report showing total usage across Claude Code, Codex, Cursor, and Gemini.

Acceptance criteria:

- Shows total input/output/cache tokens where available.
- Shows total API-equivalent cost.
- Shows usage by provider.
- Shows usage by model.
- Shows usage by day.
- Marks unavailable or estimated fields clearly.

---

### US-2: Inspect Claude Code Usage

As a Claude Code user, I want the tool to scan my local Claude Code logs so that I can see monthly Claude token usage without manually opening Claude.

Acceptance criteria:

- Finds default Claude Code log directory:
  - macOS/Linux: `~/.claude/projects/`
- Parses JSONL records.
- Extracts:
  - timestamp
  - model
  - project path
  - session id
  - input tokens
  - output tokens
  - cache creation tokens
  - cache read tokens
- Can optionally shell out to `ccusage` and ingest its JSON output.
- Compares native parser vs `ccusage` if both enabled.

Rationale: `ccusage` is already designed to analyze Claude Code local JSONL files and provide daily, weekly, monthly, session, block, model-specific, and cost analysis.

---

### US-3: Inspect Codex CLI Usage

As a Codex CLI user, I want the tool to scan `~/.codex` and summarize monthly token use.

Acceptance criteria:

- Finds `CODEX_HOME`; defaults to `~/.codex`.
- Parses Codex session JSONL files.
- Supports importing `@ccusage/codex monthly --json` output.
- Labels Codex data as `exact_from_local_log` only when token fields are present.
- Labels parser version and schema version because Codex CLI and ccusage Codex support are evolving.

Rationale: `@ccusage/codex` reads Codex session JSONL files under `CODEX_HOME`, defaulting to `~/.codex`, and converts running token totals into per-day/month deltas, but the project warns that the Codex companion CLI is experimental.

---

### US-4: Import Cursor Usage

As a Cursor Pro user, I want to include Cursor monthly usage in the same report.

Acceptance criteria:

- MVP supports manual CSV/JSON import from Cursor dashboard.
- Required columns:
  - timestamp
  - model
  - request type
  - input tokens
  - output tokens
  - cache read tokens, if available
  - cache write tokens, if available
  - billed cost or usage credits, if available
- If user only has Cursor billing credit usage and not tokens, show:
  - included usage consumed
  - extra usage charged
  - model-level cost if known
  - token counts as `unavailable`
- The tool must not pretend Auto routing identifies the underlying model unless the dashboard/export exposes it.

Rationale: Cursor’s pricing is now linked to model usage credits and API pricing, and Cursor’s own blog says Pro includes $20 of frontier model usage monthly, with additional usage at cost. That means the dashboard/billing layer is more authoritative than a guessed local token parser.

---

### US-5: Import Gemini Usage

As a Gemini user, I want the tool to summarize Gemini CLI/API token usage.

Acceptance criteria:

- Supports direct ingestion from:
  - Gemini API response metadata
  - local proxy logs
  - AI Studio logs export, if available
  - Google Cloud log export, if enabled
- Supports pre-flight token counting with `count_tokens`.
- Captures multimodal token counts when present.
- Distinguishes API usage from Gemini consumer/subscription usage.

Rationale: Gemini docs state that input and output are tokenized, including non-text modalities, and `count_tokens` returns input token counts before sending a request. Google AI Studio logging supports `GenerateContent` and `StreamGenerateContent` API calls.

---

## 7. Functional Requirements

### 7.1 CLI Commands

```bash
tokenburn init
tokenburn scan
tokenburn report --month 2026-04
tokenburn report --from 2026-04-01 --to 2026-04-30
tokenburn export --month 2026-04 --format json
tokenburn export --month 2026-04 --format csv
tokenburn doctor
tokenburn providers
tokenburn import cursor ./cursor-usage-april.csv
tokenburn import gemini ./gemini-logs-april.jsonl
```

---

### 7.2 `tokenburn init`

Creates config file:

```yaml
version: 1

timezone: America/Los_Angeles

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
      - ~/.codex
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

privacy:
  store_raw_prompts: false
  hash_project_paths: false
  redact_home_dir: true
```

---

### 7.3 `tokenburn scan`

Discovers logs and reports availability:

Example output:

```text
Provider       Status       Source                         Confidence
Claude Code    Found        ~/.claude/projects             high
Codex          Found        ~/.codex                       medium
Cursor         Missing      manual import required         unavailable
Gemini         Found        ~/.tokenburn/imports/gemini    high
```

---

### 7.4 `tokenburn report`

Example output:

```text
AI Coding Agent Token Burn Report
Month: 2026-04
Timezone: America/Los_Angeles

Total tokens: 438,241,992
API-equivalent cost: $1,482.61
Subscription cash paid: $80.00
Estimated value multiple: 18.5x

By provider:
Claude Code     312,492,810 tokens   $1,021.44 API-equiv   exact/local
Codex            71,104,900 tokens   $214.88 API-equiv     local/experimental
Cursor           42,300,000 tokens   $186.29 API-equiv     dashboard import
Gemini           12,344,282 tokens   $60.00 API-equiv      API logs
```

---

## 8. Data Model

### 8.1 Core Table: `usage_events`

Each model call or reconstructed event becomes one row.

```sql
CREATE TABLE usage_events (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  tool TEXT NOT NULL,
  account_plan TEXT,
  model TEXT,
  model_alias TEXT,
  session_id TEXT,
  conversation_id TEXT,
  project_path TEXT,
  project_hash TEXT,
  repo_name TEXT,
  timestamp_start TEXT NOT NULL,
  timestamp_end TEXT,
  timezone TEXT NOT NULL,

  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_creation_tokens INTEGER,
  cache_read_tokens INTEGER,
  reasoning_tokens INTEGER,
  total_tokens INTEGER,

  request_count INTEGER DEFAULT 1,
  estimated_cost_usd REAL,
  billed_cost_usd REAL,
  included_subscription_value_usd REAL,

  source_type TEXT NOT NULL,
  source_path TEXT,
  source_parser TEXT NOT NULL,
  confidence TEXT NOT NULL,

  raw_available BOOLEAN DEFAULT FALSE,
  created_at TEXT NOT NULL
);
```

---

### 8.2 Provider Table

```sql
CREATE TABLE providers (
  provider TEXT PRIMARY KEY,
  display_name TEXT,
  default_log_path TEXT,
  parser_version TEXT,
  enabled BOOLEAN
);
```

---

### 8.3 Pricing Table

```sql
CREATE TABLE model_pricing (
  provider TEXT,
  model TEXT,
  effective_date TEXT,
  input_per_million_usd REAL,
  output_per_million_usd REAL,
  cache_write_per_million_usd REAL,
  cache_read_per_million_usd REAL,
  source_url TEXT,
  PRIMARY KEY (provider, model, effective_date)
);
```

---

## 9. Provider Adapter Design

Use an adapter interface:

```ts
interface ProviderAdapter {
  id: string;
  displayName: string;

  discover(config: AppConfig): Promise<DiscoveredSource[]>;

  parse(source: DiscoveredSource, range: DateRange): AsyncIterable<UsageEvent>;

  validate(events: UsageEvent[]): ValidationResult;

  summarize(events: UsageEvent[]): ProviderSummary;
}
```

---

## 10. Claude Code Adapter

### Sources

1. Native local JSONL parser.
2. Optional `ccusage --json` import.
3. Optional statusline snapshots in future.

### Default path

```bash
~/.claude/projects/**/*.jsonl
```

### Parser behavior

For each JSONL line:

- Parse JSON.
- Identify event type.
- Extract model.
- Extract token usage fields.
- Normalize timestamp to configured timezone.
- Map project from file path.
- Generate deterministic event id:

```text
sha256(provider + source_path + line_number + timestamp + model)
```

### Caveat

Claude Code local JSONL accounting may differ from in-session `/usage` or statusline views depending on what Claude Code writes to disk and how subagent/tool usage is recorded. Therefore, the tool should support both:

- `claude_native_jsonl`
- `ccusage_json`
- future `claude_statusline_snapshot`

The report should show parser source.

---

## 11. Codex Adapter

### Sources

1. Native `~/.codex` parser.
2. `@ccusage/codex monthly --json` import.

OpenAI Codex CLI exposes `/status`, which displays session configuration and token usage, and the official docs describe it as showing active model, approval policy, writable roots, and current token usage.

### Default path

```bash
~/.codex
```

### Parser behavior

- Walk session files.
- Extract session-level running token totals.
- Convert cumulative totals into deltas.
- Attribute deltas to the timestamp where the usage changed.
- If only session totals exist, assign them to session end time and label confidence as `estimated_from_session_summary`.

### Caveat

`@ccusage/codex` is currently marked experimental by its own docs, so schema churn must be expected.

---

## 12. Cursor Adapter

### MVP: Manual Dashboard Import

Cursor is the hardest provider. Do not build this around fragile local reverse-engineering in MVP.

### Supported imports

1. CSV export from Cursor dashboard, if available.
2. User-saved HTML table export converted to CSV.
3. Manual JSON file.
4. Future: browser extension that locally captures dashboard table rows with explicit user action.

### Cursor import schema

```csv
timestamp,model,request_type,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_usd,source
2026-04-10T12:33:00-07:00,claude-4-sonnet,agent,42000,1200,300000,0,0.92,cursor_dashboard
```

### Required handling

- If model is `Auto`, store:
  - `model = "cursor-auto"`
  - `model_alias = null`
  - `confidence = "manual_import"`
- If Cursor exposes underlying model in the dashboard row, store it.
- If only cost/credit is known, do not infer exact tokens.

### Why this design

Cursor’s pricing is now linked to model usage credits and API pricing, and Cursor’s own blog says Pro includes $20 of frontier model usage monthly, with additional usage at cost. That means the dashboard/billing layer is more authoritative than a guessed local token parser.

---

## 13. Gemini Adapter

### Sources

1. Gemini API response metadata.
2. AI Studio logs.
3. Google Cloud logs.
4. Local wrapper/proxy logs.
5. Manual import.

### Recommended approach

For your own scripts, wrap Gemini calls with a local logger:

```ts
const response = await model.generateContent(prompt);

logger.write({
  provider: "google",
  tool: "gemini",
  model: modelName,
  timestamp: new Date().toISOString(),
  usageMetadata: response.usageMetadata,
});
```

### Token counting

Gemini supports `count_tokens` for input counting before generation. Use it for:

- Pre-flight burn estimation.
- Validation.
- Cases where output usage metadata is missing.

### Logging

Google AI Studio logging supports `GenerateContent` and `StreamGenerateContent` API calls.

---

## 14. Reporting Requirements

### 14.1 Monthly Summary

Must include:

```text
Month
Total tokens
Input tokens
Output tokens
Cache read tokens
Cache write tokens
Reasoning tokens, if available
API-equivalent cost
Actual subscription spend
Estimated value multiple
```

---

### 14.2 Provider Breakdown

```text
Provider | Tokens | Cost | % of Total | Confidence | Notes
```

---

### 14.3 Model Breakdown

```text
Model | Provider | Input | Output | Cache Read | Cache Write | Cost
```

---

### 14.4 Project Breakdown

```text
Project | Provider(s) | Tokens | Cost | Top Model | Sessions
```

---

### 14.5 “Token Black Hole” Report

Detect:

- Long-running sessions with high context reuse.
- Cursor MAX-mode-like spikes.
- Claude subagent-heavy runs.
- Codex sessions with runaway loops.
- Gemini loops with file reads/shell calls causing context growth.

Output:

```text
Top 10 most expensive sessions:
1. Claude Code | nutriscan-stage2 | 42.1M tokens | $138 API-equiv
2. Cursor Agent | dashboard MVP | 18.4M tokens | $61 API-equiv
...
```

---

## 15. Cost Calculation

### 15.1 API-Equivalent Cost

Formula:

```text
cost =
  input_tokens / 1_000_000 * input_price +
  output_tokens / 1_000_000 * output_price +
  cache_creation_tokens / 1_000_000 * cache_write_price +
  cache_read_tokens / 1_000_000 * cache_read_price
```

### 15.2 Subscription Value Multiple

```text
value_multiple = api_equivalent_cost / monthly_subscription_cash_paid
```

Example:

```text
Claude Pro: $20
ChatGPT Plus: $20
Cursor Pro: $20
Google Pro: $20
Total subscription spend: $80

API-equivalent usage: $1,482.61
Value multiple: 18.5x
```

Important: this is **not vendor cost**. It is the retail API-equivalent cost of similar usage.

---

## 16. Configuration

Example config:

```yaml
subscriptions:
  claude_pro:
    monthly_cost_usd: 20
    providers:
      - claude_code

  chatgpt_plus:
    monthly_cost_usd: 20
    providers:
      - codex

  cursor_pro:
    monthly_cost_usd: 20
    providers:
      - cursor

  google_pro:
    monthly_cost_usd: 20
    providers:
      - gemini
```

---

## 17. Privacy Requirements

### Default privacy posture

- Local-only.
- No raw prompts stored in database.
- No code contents stored.
- Project paths can be redacted or hashed.
- Raw logs are read-only.
- User must explicitly enable cloud sync.

### Redaction

```yaml
privacy:
  redact_home_dir: true
  redact_project_paths: false
  hash_project_paths: false
  store_raw_messages: false
```

---

## 18. Technical Architecture

### Recommended stack

For this use case, build this as a **Python CLI first**, not a full web app.

Reason:

- Easy filesystem scanning.
- Easy JSONL/CSV parsing.
- Easy SQLite.
- Easy pandas reports.
- Easy packaging with `uv`.
- Can later add a local dashboard.

### MVP stack

```text
Language: Python 3.11+
Package manager: uv
CLI: Typer
Database: SQLite
ORM/light wrapper: SQLModel or raw sqlite-utils
Data processing: pandas or polars
Config: pydantic-settings + YAML
Reports: rich tables + markdown export
Tests: pytest
```

### Future dashboard

```text
Backend: FastAPI
Frontend: React / Next.js
Charts: Recharts
Local DB: SQLite
```

---

## 19. Directory Structure

```text
tokenburn-ledger/
  pyproject.toml
  README.md
  src/
    tokenburn/
      __init__.py
      cli.py
      config.py
      db.py
      models.py

      adapters/
        base.py
        claude_code.py
        codex.py
        cursor.py
        gemini.py

      parsers/
        jsonl.py
        csv_import.py
        pricing.py

      reports/
        monthly.py
        daily.py
        models.py
        projects.py
        markdown.py

      privacy/
        redaction.py

      util/
        dates.py
        hashing.py
        paths.py

  tests/
    fixtures/
      claude/
      codex/
      cursor/
      gemini/
    test_claude_parser.py
    test_codex_parser.py
    test_cursor_import.py
    test_gemini_import.py
```

---

## 20. MVP Implementation Plan

### Phase 1: Foundation

Build:

- CLI skeleton
- YAML config
- SQLite schema
- `usage_events` model
- date range utilities
- report renderer

Deliverable:

```bash
tokenburn init
tokenburn doctor
```

---

### Phase 2: Claude Code

Build:

- Claude log discovery
- Claude JSONL parser
- ccusage JSON importer
- monthly Claude report

Deliverable:

```bash
tokenburn report --provider claude_code --month 2026-04
```

---

### Phase 3: Codex

Build:

- Codex log discovery
- Codex JSONL parser
- `@ccusage/codex` JSON importer
- schema-version detection

Deliverable:

```bash
tokenburn report --provider codex --month 2026-04
```

---

### Phase 4: Manual Cursor Import

Build:

- Cursor CSV schema
- Import validator
- Cursor usage summary
- Warnings for missing token columns

Deliverable:

```bash
tokenburn import cursor ./cursor_april.csv
tokenburn report --provider cursor --month 2026-04
```

---

### Phase 5: Gemini Import

Build:

- Gemini JSONL import
- usage metadata parser
- AI Studio/Cloud log import format support
- optional `count_tokens` validation hook

Deliverable:

```bash
tokenburn import gemini ./gemini_april.jsonl
tokenburn report --provider gemini --month 2026-04
```

---

### Phase 6: Unified Monthly Report

Build:

- Cross-provider aggregation
- cost calculation
- confidence annotations
- Markdown export

Deliverable:

```bash
tokenburn report --month 2026-04
tokenburn export --month 2026-04 --format markdown
```

---

## 21. Edge Cases

### 21.1 Token counter mismatch

Different sources may disagree. The product should show reconciliation:

```text
Claude Code:
Native parser: 312.4M tokens
ccusage:       309.9M tokens
delta:         0.8%
status:        acceptable
```

If delta > 5%, warn.

---

### 21.2 Cumulative totals vs event totals

Codex-style logs may store running totals. The parser must convert:

```text
delta = current_total - previous_total
```

If negative delta appears, assume new session/window.

---

### 21.3 Cache tokens

Cache tokens should not be collapsed into generic input tokens. Store separately:

- `input_tokens`
- `output_tokens`
- `cache_creation_tokens`
- `cache_read_tokens`

Reason: cache read tokens can dominate apparent token volume but cost much less than fresh input.

---

### 21.4 Cursor Auto

Cursor Auto may not expose the underlying model. Store as:

```text
provider: cursor
model: cursor-auto
confidence: manual_import
```

Do not guess.

---

### 21.5 Subscription vs API-equivalent accounting

The report must separate:

```text
Actual cash paid: $80
API-equivalent value: $1,482.61
Vendor internal cost: unknown
```

---

## 22. Example Markdown Report

```markdown
# AI Coding Agent Token Burn Report — April 2026

## Summary

- Total tokens: 438,241,992
- API-equivalent cost: $1,482.61
- Subscription cash paid: $80.00
- Estimated subscription value multiple: 18.5x

## By Provider

| Provider | Tokens | API-Equivalent Cost | Confidence |
|---|---:|---:|---|
| Claude Code | 312,492,810 | $1,021.44 | exact/local |
| Codex | 71,104,900 | $214.88 | local/experimental |
| Cursor | 42,300,000 | $186.29 | dashboard import |
| Gemini | 12,344,282 | $60.00 | API logs |

## Notes

Cursor usage is based on manual dashboard import. Cursor Auto requests are not assigned to underlying models unless the import exposes them.

Codex parsing is marked experimental because the Codex log parser depends on evolving Codex CLI session formats.
```

---

## 23. Risks

### Risk 1: Cursor data is not programmatically accessible

Mitigation:

- MVP uses manual import.
- Later add explicit browser-extension-assisted export.
- Never rely on unstable private endpoints as core architecture.

### Risk 2: Vendor log schema changes

Mitigation:

- Parser versioning.
- Fixture-based tests.
- Graceful failure with `tokenburn doctor`.

### Risk 3: Token counts differ from dashboard

Mitigation:

- Store source and confidence.
- Add reconciliation report.
- Never present estimated numbers as exact.

### Risk 4: Privacy leakage

Mitigation:

- Local-only.
- No raw prompts by default.
- Redact paths.
- No cloud analytics.

---

## 24. Success Metrics

### MVP Success

- User can generate a monthly report in under 30 seconds.
- Claude Code and Codex are auto-detected.
- Cursor and Gemini can be imported manually.
- Report clearly distinguishes exact vs estimated usage.

### Product Success

User can answer:

- “How many tokens did I burn last month?”
- “Which agent burned the most?”
- “Which project burned the most?”
- “Which model was most expensive?”
- “Did I get more API-equivalent value than I paid in subscriptions?”
- “Which sessions were runaway token disasters?”

---

## 25. Recommended MVP Scope

Build in this order:

1. **Claude Code first**, because the data source is strongest.
2. **Codex second**, because `@ccusage/codex` already exists but is experimental.
3. **Cursor manual import third**, because reliable automated Cursor extraction is the trap.
4. **Gemini fourth**, unless Gemini is already being used through API scripts.
5. Add dashboard only after the CLI report is correct.

The architecture should be brutally honest:

> Claude and Codex can be mostly automated; Gemini can be automated if API/logging is enabled; Cursor should start as dashboard import unless Cursor exposes a stable export/API.

---

## 26. Cursor/Coding Agent Implementation Prompt

Use the following as a direct implementation prompt for a coding agent:

```text
Build a Python 3.11+ CLI application called TokenBurn Ledger.

The app summarizes monthly AI coding-agent token usage across Claude Code, OpenAI Codex CLI, Cursor, and Gemini.

Use:
- uv for package management
- Typer for CLI
- SQLite for local storage
- Pydantic for config models
- Rich for terminal tables
- pytest for tests

Core commands:
- tokenburn init
- tokenburn scan
- tokenburn doctor
- tokenburn report --month YYYY-MM
- tokenburn report --provider PROVIDER --month YYYY-MM
- tokenburn import cursor PATH
- tokenburn import gemini PATH
- tokenburn export --month YYYY-MM --format markdown|json|csv

Implement provider adapters:
1. Claude Code adapter:
   - Discover ~/.claude/projects/**/*.jsonl
   - Parse JSONL files
   - Extract timestamp, model, session_id, project_path, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens where available
   - Label source_parser as claude_native_jsonl

2. Codex adapter:
   - Discover CODEX_HOME or ~/.codex
   - Parse JSONL/session logs
   - Support cumulative token totals by converting them to deltas
   - Label confidence as exact_from_local_log when event-level token fields are present, otherwise estimated_from_session_summary

3. Cursor adapter:
   - Do not scrape private Cursor APIs
   - Support manual CSV/JSON import
   - Validate required fields when present
   - If only cost/credit is provided, store cost and mark tokens unavailable
   - If model is Auto, store model as cursor-auto and do not infer underlying model

4. Gemini adapter:
   - Support JSONL import of Gemini API logs
   - Parse usageMetadata-like fields
   - Store input, output, cache, and total tokens when present

Data model:
Create a usage_events SQLite table with provider, tool, model, session_id, project_path, timestamp_start, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, total_tokens, estimated_cost_usd, billed_cost_usd, source_type, source_path, source_parser, and confidence.

Reporting:
- Monthly summary
- Provider breakdown
- Model breakdown
- Project breakdown
- Top token-burning sessions
- API-equivalent cost
- Subscription value multiple

Privacy:
- Local-only by default
- Do not store raw prompts or raw code
- Redact home directory in displayed paths
- Read logs without modifying them

Testing:
- Add fixtures for Claude, Codex, Cursor, and Gemini sample logs
- Test parsing, aggregation, import validation, and report generation

Do not invent exact Cursor token counts when unavailable. Always display confidence labels.
```

---

## 27. Final Architectural Recommendation

The MVP should be a **local Python CLI with pluggable provider adapters**.

Do not start with a web dashboard. Do not start with scraping. Do not start with perfect Cursor support. Start with trustworthy local accounting for Claude Code and Codex, then add import-based Cursor and Gemini support.

The winning product principle is:

> Exact where possible. Estimated where necessary. Honest everywhere.
