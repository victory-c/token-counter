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

## Task classification & right-sizing (v0.2)

Dollar totals hide the real waste: most token spend goes to jobs that didn't need
the strongest model. TokenBurn classifies each session into a task category
(extraction, summarization, code-review, feature-implementation, debugging, …)
and tells you which categories are over-served.

```bash
# Classify Claude + Codex sessions (heuristic, no API calls).
uv run tokenburn classify --month 2026-04

# By-task breakdown alongside the regular report.
uv run tokenburn report --month 2026-04 --by-task

# Right-sizing recommendations: where Opus runs do work Sonnet would handle, etc.
uv run tokenburn savings --month 2026-04

# Explain how one session was classified.
uv run tokenburn task-detail --session SESS_ID

# Override a misclassified session.
uv run tokenburn override --session SESS_ID --provider claude_code --category extraction
```

The classifier is a heuristic decision tree (no LLM calls, no API keys, fully
local). It reads the same `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions`
files the parsers do, extracts session-level signals (tool-call counts, file
extensions touched, message patterns), and scores each task category. Ties
break in favor of explainability — when a session looks misclassified, run
`tokenburn task-detail` to see the exact signals and rules that fired, edit
`src/tokenburn/classifier/heuristic.py`, and re-run.

Cursor and Gemini events appear as `unclassified` because the dashboard CSV
and API metadata don't carry conversation content; without that signal,
classification would be guessing.

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
