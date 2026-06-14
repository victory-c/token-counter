# TokenCounter

Local-first CLI that audits monthly AI coding-agent token usage across Claude Code, OpenAI Codex CLI, Cursor, and Gemini. Built to answer one question: **where is my AI spend actually going, and where am I paying for the wrong model?**

![Right-sizing savings report](https://raw.githubusercontent.com/victory-c/token-counter/main/assets/screenshot-savings.svg)

- **Per-task spend breakdown** — extraction, code review, feature work, debugging, … not just per-model totals.
- **Right-sizing recommendations** — flags the Opus runs that would have been fine on Sonnet, the Sonnet runs that would have been fine on Haiku, and prices the delta against real list prices.
- **No API keys, no proxy, no cloud** — reads the JSONL logs the CLIs already write to disk. Cursor and Gemini come in via dropped CSV/JSONL.

## Install

```bash
pipx install tokencounter
```

Or with `pip` / `uv`:

```bash
pip install tokencounter
# or
uv pip install tokencounter
```

> The legacy binary name `tokenburn` is kept as an alias — either command works.

## Quick start

```bash
tokencounter init
tokencounter doctor
tokencounter report --month 2026-04
tokencounter export --month 2026-04 --format markdown --output report.md
```

## Reports & interactive dashboard

The Markdown report stays the canonical, version-controllable document. Alongside
it, `dashboard` generates a **standalone interactive HTML dashboard** — same data,
visual and explorable. It is a single self-contained file: works offline, no
backend, no external requests, and never embeds raw prompts/code.

```bash
# Markdown report (stdout, or --output FILE)
tokencounter export --month 2026-04 --format markdown --output report.md

# Interactive HTML dashboard → ~/Downloads/tokenburn-dashboard-2026-04.html
tokencounter dashboard --month 2026-04
tokencounter dashboard --month 2026-04 --open          # also open in browser
tokencounter dashboard --month 2026-04 --output ~/my-dash.html

# Both at once → ~/Downloads/ (override with --output-dir)
tokencounter export-month --month 2026-04
tokencounter export-month --month 2026-04 --open-dashboard
```

The dashboard has client-side filters (provider / model / project / confidence /
metric), donut + bar + daily-trend charts, a sortable / searchable / paginated
session table with CSV export and column toggles, a token-black-holes ranking,
data-quality (confidence) breakdown, pricing assumptions, subscription
value-multiples, and provider caveats. All dollar figures are labelled
**API-equivalent estimates**, not vendor cost. The default output directory
(`~/Downloads`) and filename patterns are configurable under `exports:` /
`dashboard:` in the config.

## Task classification & right-sizing

Dollar totals hide the real waste: most token spend goes to jobs that didn't need
the strongest model. TokenCounter classifies each session into a task category
(extraction, summarization, code-review, feature-implementation, debugging, …)
and tells you which categories are over-served.

```bash
# Classify Claude + Codex sessions (heuristic, no API calls).
tokencounter classify --month 2026-04

# By-task breakdown alongside the regular report.
tokencounter report --month 2026-04 --by-task

# Right-sizing recommendations: where Opus runs do work Sonnet would handle, etc.
tokencounter savings --month 2026-04

# Classifier health dashboard: coverage, confidence, override patterns.
tokencounter classifier-stats

# Explain how one session was classified.
tokencounter task-detail --session SESS_ID

# Override a misclassified session.
tokencounter override --session SESS_ID --provider claude_code --category extraction
```

The classifier is a heuristic decision tree (no LLM calls, no API keys, fully
local). It reads the same `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions`
files the parsers do, extracts session-level signals (tool-call counts, file
extensions touched, message patterns), and scores each task category. Ties
break in favor of explainability — when a session looks misclassified, run
`tokencounter task-detail` to see the exact signals and rules that fired, edit
`src/tokenburn/classifier/heuristic.py`, and re-run.

Cursor and Gemini events stay `unclassified` because their dashboard CSV
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
tokencounter reconcile --month 2026-04
```

Compares native-parser totals to `ccusage daily --json` and warns if delta > 5%.

## Development

```bash
git clone https://github.com/victory-c/token-counter
cd token-counter
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest                            # 57 tests
uv run tokencounter --help
```

The internal Python package name is still `tokenburn` (no breaking refactor) — the CLI command and PyPI distribution are `tokencounter`.

## License

MIT. See [LICENSE](LICENSE).
