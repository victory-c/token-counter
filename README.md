# TokenBurn Ledger

Local-first CLI that audits monthly AI coding-agent token usage across Claude Code, OpenAI Codex CLI, Cursor, and Gemini.

## Install

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Quick start

```bash
uv run tokenburn init
uv run tokenburn doctor
uv run tokenburn scan
uv run tokenburn report --month 2026-04
uv run tokenburn export --month 2026-04 --format markdown --output report.md
```

## Providers

| Provider | Mode | Source |
|---|---|---|
| Claude Code | automatic | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | automatic | `${CODEX_HOME:-~/.codex}/sessions/**/rollout-*.jsonl` |
| Cursor | manual import | CSV/JSON dropped under `~/.tokenburn/imports/cursor/` |
| Gemini | manual import | JSONL dropped under `~/.tokenburn/imports/gemini/` |

### Cursor CSV schema

Required columns: `timestamp,model`. Recognized optional columns: `request_type, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, source, session_id, project_path`. If `model == "Auto"` the row is stored as `cursor-auto` with no underlying-model inference.

### Gemini JSONL schema

One record per line with `timestamp`, `model`, and `usageMetadata` shaped like the Gemini API response: `promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `totalTokenCount`. See `examples/gemini_sample.jsonl`.

## Pricing

`pricing.yaml` ships with public list prices keyed by `(provider, model, effective_date)`. Lookup picks the latest row whose `effective_date <= event timestamp`. Update this file when a vendor changes prices; each row carries a `source_url` for auditability.

## Privacy

Default posture: local-only, no raw prompts/code stored, home-dir redaction in displayed paths, log files read-only. See `privacy:` in the generated config.

## Reconcile against ccusage

If `ccusage` is on PATH:

```bash
uv run tokenburn reconcile --month 2026-04
```

Compares native-parser totals to `ccusage daily --json` and warns if delta > 5%.
