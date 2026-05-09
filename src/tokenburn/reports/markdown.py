from __future__ import annotations

import csv
import io
from typing import Any

from ..config import AppConfig
from ..models import DateRange
from ..util.paths import redact_home


def render_markdown(summary: dict[str, Any], range_: DateRange, cfg: AppConfig) -> str:
    redact = cfg.privacy.redact_home_dir
    totals = summary["totals"]
    lines: list[str] = []
    lines.append(f"# AI Coding Agent Token Burn Report — {range_.start} to {range_.end}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total tokens: {int(totals.get('total_tokens', 0)):,}")
    lines.append(f"- Input: {int(totals.get('input_tokens', 0)):,}")
    lines.append(f"- Output: {int(totals.get('output_tokens', 0)):,}")
    lines.append(f"- Cache creation: {int(totals.get('cache_creation_tokens', 0)):,}")
    lines.append(f"- Cache read: {int(totals.get('cache_read_tokens', 0)):,}")
    lines.append(f"- Reasoning: {int(totals.get('reasoning_tokens', 0)):,}")
    lines.append(f"- API-equivalent cost: ${float(totals.get('estimated_cost_usd', 0.0)):,.2f}")
    lines.append(f"- Subscription cash paid: ${summary['subscription_total_usd']:,.2f}")
    if summary["value_multiple"] is not None:
        lines.append(f"- Estimated subscription value multiple: {summary['value_multiple']:.2f}x")
    lines.append("")

    lines.append("## By Provider")
    lines.append("")
    lines.append("| Provider | Tokens | API-Equivalent Cost | Events | Confidence |")
    lines.append("|---|---:|---:|---:|---|")
    for r in summary["by_provider"]:
        lines.append(
            f"| {r['provider']} | {int(r['tokens']):,} | ${float(r['cost']):,.2f} | {int(r['events']):,} | {r.get('confidences') or ''} |"
        )
    lines.append("")

    if summary["by_model"]:
        lines.append("## By Model")
        lines.append("")
        lines.append("| Provider | Model | Input | Output | Cache R | Cache W | Reason | Tokens | Cost |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in summary["by_model"][:50]:
            lines.append(
                f"| {r['provider']} | {r['model'] or '(unknown)'} | {int(r['input_tokens']):,} | "
                f"{int(r['output_tokens']):,} | {int(r['cache_read_tokens']):,} | "
                f"{int(r['cache_creation_tokens']):,} | {int(r['reasoning_tokens']):,} | "
                f"{int(r['tokens']):,} | ${float(r['cost']):,.2f} |"
            )
        lines.append("")

    if summary["by_project"]:
        lines.append("## Top Projects")
        lines.append("")
        lines.append("| Provider | Project | Tokens | Cost | Sessions |")
        lines.append("|---|---|---:|---:|---:|")
        for r in summary["by_project"]:
            proj = redact_home(r["project_path"]) if redact else r["project_path"]
            lines.append(
                f"| {r['provider']} | {proj} | {int(r['tokens']):,} | ${float(r['cost']):,.2f} | {int(r['sessions']):,} |"
            )
        lines.append("")

    if summary["top_sessions"]:
        lines.append("## Top 10 Token-Burning Sessions")
        lines.append("")
        lines.append("| Provider | Project | Session | Tokens | Cost |")
        lines.append("|---|---|---|---:|---:|")
        for r in summary["top_sessions"]:
            proj = redact_home(r["project_path"]) if redact and r["project_path"] else (r["project_path"] or "")
            sid = (r["session_id"] or "")[:12]
            lines.append(
                f"| {r['provider']} | {proj} | `{sid}` | {int(r['tokens']):,} | ${float(r['cost']):,.2f} |"
            )
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("Confidence labels reflect data provenance:")
    lines.append("`exact_from_local_log` (Claude Code, Codex), "
                 "`exact_from_provider_log` (Gemini API metadata), "
                 "`manual_import` (Cursor / hand-imported), "
                 "`estimated_from_session_summary` (Codex cumulative-totals fallback).")
    lines.append("")
    lines.append("Subscription value multiple is the API-equivalent retail cost divided by subscription cash paid. It is **not** vendor cost.")
    return "\n".join(lines) + "\n"


def render_csv(summary: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "provider", "model", "project", "session", "day", "tokens", "cost_usd"])
    for r in summary["by_provider"]:
        writer.writerow(["provider", r["provider"], "", "", "", "", int(r["tokens"]), float(r["cost"])])
    for r in summary["by_model"]:
        writer.writerow(["model", r["provider"], r["model"] or "", "", "", "", int(r["tokens"]), float(r["cost"])])
    for r in summary["by_project"]:
        writer.writerow(["project", r["provider"], "", r["project_path"] or "", "", "", int(r["tokens"]), float(r["cost"])])
    for r in summary["top_sessions"]:
        writer.writerow(["session", r["provider"], "", r["project_path"] or "", r["session_id"] or "", "", int(r["tokens"]), float(r["cost"])])
    for r in summary["by_day"]:
        writer.writerow(["day", r["provider"], "", "", "", r["day"], int(r["tokens"]), float(r["cost"])])
    return buf.getvalue()
