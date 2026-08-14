"""Interactive standalone HTML dashboard export.

This complements (never replaces) the Markdown report. It builds a single
JSON payload from the same SQLite store the Markdown/CSV/JSON exports use,
embeds it in a self-contained HTML file, and ships a vanilla-JS front-end
that re-aggregates client-side so every filter updates the cards, charts,
and tables. No backend, no external network calls, no charting dependency —
the file works offline when opened in any modern browser.

Design notes
------------
* The embedded *fact table* is one row per (provider, session). All charts
  and summary cards are recomputed in the browser from these rows, so the
  filters genuinely drive the visualizations. Daily trend attributes a
  session to its first local date — an acceptable approximation for a
  monthly view (a session rarely straddles days), surfaced as a caveat.
* Privacy: we never persist raw prompts/completions/code (see PrivacyConfig),
  so the payload can't leak them. Project paths honour `redact_home_dir`.
* Progressive enhancement: the interactive view is JS-rendered, but the file
  must never look empty when scripts don't run (preview panes and snapshot
  renderers routinely disable them, and a JS error would blank it too). So we
  *also* server-render a static view that is visible by default; the script
  only hides it after `init()` completes. Any failure leaves real data on
  screen. See `render_static_fallback`.
"""

from __future__ import annotations

import html as _html
import json
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..config import AppConfig
from ..models import DateRange
from ..pricing import PricingTable
from ..util.paths import redact_home

# Confidence buckets that are NOT exact provider/local logs. A high share of
# these means the headline totals are directional rather than precise.
_SOFT_CONFIDENCE = {
    "estimated_from_session_summary",
    "estimated_from_text_retokenization",
    "manual_import",
    "unavailable",
}

# Pricing rows sort worst-first so the rows needing attention lead the table.
_STATUS_RANK = {"missing": 0, "approximate": 1, "exact": 2}

_PROVIDER_CAVEATS = {
    "claude_code": (
        "Claude Code local JSONL is the strongest source. Numbers come from the "
        "native parser; run `tokencounter reconcile` to diff against ccusage."
    ),
    "codex": (
        "Codex parsing depends on evolving session-log formats. Sessions that only "
        "expose cumulative totals are reconstructed into deltas and labelled "
        "estimated_from_session_summary."
    ),
    "cursor": (
        "Cursor data is a manual dashboard import. Cursor Auto is not mapped to an "
        "underlying model unless the export reveals it; when only cost/credit is "
        "present, token counts are unavailable."
    ),
    "gemini": (
        "Gemini API usage is exact when usageMetadata is present. Consumer Gemini "
        "usage may not expose token data; count_tokens validates input only."
    ),
}


def _redact(cfg: AppConfig, value: str | None) -> str | None:
    if value is None:
        return None
    return redact_home(value) if cfg.privacy.redact_home_dir else value


def _project_label(project_path: str | None, project_hash: str | None) -> str | None:
    if project_path:
        return project_path
    if project_hash:
        return f"hash:{project_hash}"
    return None


def build_dashboard_payload(
    db,
    range_: DateRange,
    cfg: AppConfig,
    *,
    pricing: PricingTable | None = None,
    provider_filter: str | None = None,
    max_table_rows: int | None = None,
) -> dict[str, Any]:
    """Build the JSON payload embedded in the dashboard HTML."""
    max_rows = max_table_rows if max_table_rows is not None else cfg.dashboard.max_table_rows

    where_provider = "AND provider = :p" if provider_filter else ""
    params: dict[str, Any] = {"s": range_.start.isoformat(), "e": range_.end.isoformat()}
    if provider_filter:
        params["p"] = provider_filter
    base_where = "local_date BETWEEN :s AND :e " + where_provider

    rows = list(
        db.query(
            f"""
            SELECT provider, tool, session_id, model,
                   project_path, project_hash, local_date,
                   source_type, confidence,
                   COALESCE(input_tokens,0) AS input_tokens,
                   COALESCE(output_tokens,0) AS output_tokens,
                   COALESCE(cache_creation_tokens,0) AS cache_creation_tokens,
                   COALESCE(cache_read_tokens,0) AS cache_read_tokens,
                   COALESCE(reasoning_tokens,0) AS reasoning_tokens,
                   COALESCE(total_tokens,0) AS total_tokens,
                   COALESCE(estimated_cost_usd,0) AS cost
            FROM usage_events
            WHERE {base_where}
            """,
            params,
        )
    )

    # --- Aggregate events into per-session rows (the embedded fact table). ---
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    # Track which (model / project / confidence) dominates each session by tokens.
    dom_model: dict[tuple[str, str], dict[str | None, int]] = {}
    dom_project: dict[tuple[str, str], dict[str | None, int]] = {}
    dom_conf: dict[tuple[str, str], dict[str, int]] = {}

    for r in rows:
        key = (r["provider"], r["session_id"] or "")
        s = sessions.get(key)
        if s is None:
            s = {
                "provider": r["provider"],
                "tool": r["tool"],
                "session": r["session_id"] or "",
                "model": None,
                "project": None,
                "date": r["local_date"],
                "source": r["source_type"],
                "confidence": r["confidence"],
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
                "events": 0,
            }
            sessions[key] = s
            dom_model[key] = {}
            dom_project[key] = {}
            dom_conf[key] = {}

        s["input_tokens"] += int(r["input_tokens"])
        s["output_tokens"] += int(r["output_tokens"])
        s["cache_creation_tokens"] += int(r["cache_creation_tokens"])
        s["cache_read_tokens"] += int(r["cache_read_tokens"])
        s["reasoning_tokens"] += int(r["reasoning_tokens"])
        s["total_tokens"] += int(r["total_tokens"])
        s["cost"] += float(r["cost"])
        s["events"] += 1
        if r["local_date"] and (s["date"] is None or r["local_date"] < s["date"]):
            s["date"] = r["local_date"]

        tok = int(r["total_tokens"]) or 1
        dom_model[key][r["model"]] = dom_model[key].get(r["model"], 0) + tok
        proj = _project_label(r["project_path"], r["project_hash"])
        dom_project[key][proj] = dom_project[key].get(proj, 0) + tok
        dom_conf[key][r["confidence"]] = dom_conf[key].get(r["confidence"], 0) + tok

    for key, s in sessions.items():
        if dom_model[key]:
            s["model"] = max(dom_model[key].items(), key=lambda kv: kv[1])[0]
        if dom_project[key]:
            proj = max(dom_project[key].items(), key=lambda kv: kv[1])[0]
            s["project"] = _redact(cfg, proj)
        if dom_conf[key]:
            s["confidence"] = max(dom_conf[key].items(), key=lambda kv: kv[1])[0]

    session_rows = sorted(sessions.values(), key=lambda x: x["total_tokens"], reverse=True)

    truncated = False
    if max_rows and len(session_rows) > max_rows:
        session_rows = session_rows[:max_rows]
        truncated = True

    # --- Pricing assumptions for the (provider, model) pairs present. ---
    pricing_rows: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    when = datetime.combine(range_.end, datetime.min.time())
    for s in session_rows:
        model = s["model"]
        pair = (s["provider"], model or "")
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        found = pricing.match(s["provider"], model, when) if pricing else None
        if found is not None:
            price = found.row
            pricing_rows.append(
                {
                    "provider": s["provider"],
                    "model": model or "(unknown)",
                    "input_per_million_usd": price.input_per_million_usd,
                    "output_per_million_usd": price.output_per_million_usd,
                    "cache_write_per_million_usd": price.cache_write_per_million_usd,
                    "cache_read_per_million_usd": price.cache_read_per_million_usd,
                    "effective_date": price.effective_date.isoformat(),
                    "source_url": price.source_url,
                    "status": "approximate" if found.is_approximate else "exact",
                    # Which table row actually supplied the rate. Only differs
                    # from `model` when the prefix fallback fired.
                    "priced_as": found.matched_model,
                }
            )
        else:
            pricing_rows.append(
                {
                    "provider": s["provider"],
                    "model": model or "(unknown)",
                    "input_per_million_usd": None,
                    "output_per_million_usd": None,
                    "cache_write_per_million_usd": None,
                    "cache_read_per_million_usd": None,
                    "effective_date": None,
                    "source_url": "",
                    "status": "missing",
                    "priced_as": None,
                }
            )
    # Worst-first: missing, then guessed, then quoted.
    pricing_rows.sort(
        key=lambda r: (_STATUS_RANK.get(r["status"], 3), r["provider"], r["model"])
    )

    # --- Subscription mapping (API-equivalent value vs cash paid). ---
    provider_cost: dict[str, float] = {}
    for s in session_rows:
        provider_cost[s["provider"]] = provider_cost.get(s["provider"], 0.0) + s["cost"]

    subscriptions: list[dict[str, Any]] = []
    subscription_total = 0.0
    for name, sub in cfg.subscriptions.items():
        if provider_filter and provider_filter not in sub.providers:
            continue
        value = sum(provider_cost.get(p, 0.0) for p in sub.providers)
        subscription_total += sub.monthly_cost_usd
        subscriptions.append(
            {
                "name": name,
                "monthly_cost_usd": sub.monthly_cost_usd,
                "providers": sub.providers,
                "api_equiv_value_usd": round(value, 2),
                "value_multiple": round(value / sub.monthly_cost_usd, 2)
                if sub.monthly_cost_usd
                else None,
            }
        )

    # --- Warnings (computed unfiltered; the headline caveats). ---
    warnings: list[str] = []
    total_tokens = sum(s["total_tokens"] for s in session_rows)
    if total_tokens:
        soft = sum(s["total_tokens"] for s in session_rows if s["confidence"] in _SOFT_CONFIDENCE)
        soft_share = soft / total_tokens
        if soft_share >= cfg.dashboard.confidence_warning_threshold:
            warnings.append(
                f"{soft_share * 100:.0f}% of this period's usage is estimated or "
                "manually imported. Treat totals as directional, not exact."
            )
        provider_tokens = _by(session_rows, "provider")
        if provider_tokens:
            top_provider = max(provider_tokens, key=provider_tokens.get)
            share = provider_tokens[top_provider] / total_tokens
            if share >= cfg.dashboard.provider_concentration_threshold:
                warnings.append(
                    f"{top_provider} accounts for {share * 100:.0f}% of all "
                    "tokens — usage is highly concentrated in one provider."
                )
    missing_priced = sorted(
        {f"{r['provider']}/{r['model']}" for r in pricing_rows if r["status"] == "missing"}
    )
    if missing_priced:
        shown = ", ".join(missing_priced[:5])
        more = f" (+{len(missing_priced) - 5} more)" if len(missing_priced) > 5 else ""
        warnings.append(
            f"No list price for {len(missing_priced)} model(s): {shown}{more}. "
            "Their API-equivalent cost is counted as $0."
        )

    # A guessed rate is not a quoted one. Without this the prefix fallback is
    # invisible: gpt-5.6-sol priced off the generic gpt-5 row for a month and
    # understated it by ~$640, with nothing on the page hinting the number was
    # anything but exact.
    approx = sorted(
        {
            f"{r['provider']}/{r['model']} → priced as {r['priced_as']}"
            for r in pricing_rows
            if r["status"] == "approximate"
        }
    )
    if approx:
        shown = "; ".join(approx[:3])
        more = f" (+{len(approx) - 3} more)" if len(approx) > 3 else ""
        approx_cost = sum(
            s["cost"]
            for s in session_rows
            if any(
                r["status"] == "approximate"
                and r["provider"] == s["provider"]
                and r["model"] == (s["model"] or "(unknown)")
                for r in pricing_rows
            )
        )
        warnings.append(
            f"{len(approx)} model(s) have no exact price row and were charged at a "
            f"shorter-prefix rate: {shown}{more}. "
            f"{_fmt_money(approx_cost)} of the total is estimated this way and can be "
            "wrong in either direction — add an exact row to pricing.yaml to fix it."
        )
    if truncated:
        warnings.append(
            f"Session table capped at {max_rows:,} rows (dashboard.max_table_rows); "
            "charts reflect the displayed rows only."
        )

    return {
        "meta": {
            "label": _range_label_from(range_),
            "range": {"start": range_.start.isoformat(), "end": range_.end.isoformat()},
            "timezone": cfg.timezone,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider_filter": provider_filter,
            "redacted_home": cfg.privacy.redact_home_dir,
        },
        "sessions": session_rows,
        "pricing": pricing_rows,
        "subscriptions": subscriptions,
        "subscription_total_usd": subscription_total,
        "warnings": warnings,
        "caveats": _PROVIDER_CAVEATS,
        "privacy": {
            "store_raw_prompts": cfg.privacy.store_raw_prompts,
            "store_raw_messages": cfg.privacy.store_raw_messages,
            "redact_home_dir": cfg.privacy.redact_home_dir,
            "hash_project_paths": cfg.privacy.hash_project_paths,
        },
        "config": {"default_metric": cfg.dashboard.default_metric},
    }


def _by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r[field]] = out.get(r[field], 0) + int(r["total_tokens"])
    return out


def _range_label_from(range_: DateRange) -> str:
    if range_.start.year == range_.end.year and range_.start.month == range_.end.month:
        # Whole calendar month? Use YYYY-MM; else a from_to label.
        from calendar import monthrange

        last = monthrange(range_.start.year, range_.start.month)[1]
        if range_.start.day == 1 and range_.end.day == last:
            return f"{range_.start.year:04d}-{range_.start.month:02d}"
    return f"{range_.start.isoformat()}_to_{range_.end.isoformat()}"


# ---------------------------------------------------------------------------
# Static (no-JavaScript) fallback rendering.
#
# Everything below emits plain HTML from the same payload the front-end uses.
# Every value that originates from a log (project path, model, session id,
# provider, subscription name, pricing URL) is attacker-influenceable and MUST
# go through `_esc`. Bars are inline-width spans, so they need no scripting.
# ---------------------------------------------------------------------------

_PALETTE = (
    "#5b9cff", "#ff8a5b", "#36d399", "#f7c948",
    "#b072ff", "#ff6b9d", "#56d4dd", "#9aa3b2",
)

# Server-rendered session rows are capped independently of the (much larger)
# payload cap: the static view is a legibility net, not a data export, and the
# fact table is still embedded in full for the interactive view and CSV export.
_STATIC_MAX_ROWS = 250


def _esc(value: Any) -> str:
    """HTML-escape a value for text/attribute context; blanks render as an em dash."""
    if value is None or value == "":
        return "&mdash;"
    return _html.escape(str(value), quote=True)


_METRIC_LABEL = {
    "total_tokens": "Total tokens",
    "input_tokens": "Input",
    "output_tokens": "Output",
    "cache_read_tokens": "Cache read",
    "cost": "API-equiv $",
    "sessions": "Sessions",
}


def _fixed(value: Any, places: int) -> str:
    """Format like JS `toFixed`/`toLocaleString`: ties round away from zero.

    Python's `%.2f` rounds ties to even, so `3.125` renders as `$3.12` here and
    `$3.13` in the JS view — two numbers for one value in one file. Quantizing
    the *exact* binary value (`Decimal(float)`, not `Decimal(repr(float))`)
    reproduces `toFixed` semantics, including the cases where the stored double
    is really 1.00499… and JS also rounds down.
    """
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(float(value or 0)).quantize(quantum, rounding="ROUND_HALF_UP"))


def _fmt_int(n: Any) -> str:
    # JS `Math.round` rounds .5 toward +Infinity; Python's `round` is half-even.
    return f"{math.floor(float(n or 0) + 0.5):,}"


def _fmt_money(n: Any) -> str:
    whole, _, frac = _fixed(n, 2).partition(".")
    sign = "-" if whole.startswith("-") else ""
    return f"{sign}${abs(int(whole)):,}.{frac}"


def _fmt_compact(n: Any) -> str:
    n = float(n or 0)
    a = abs(n)
    if a >= 1e9:
        return _fixed(n / 1e9, 2) + "B"
    if a >= 1e6:
        return _fixed(n / 1e6, 2) + "M"
    if a >= 1e3:
        return _fixed(n / 1e3, 1) + "k"
    return f"{math.floor(n + 0.5):d}"


def _metric_of(payload: dict[str, Any]) -> str:
    """The metric the interactive view boots with — the static view must match."""
    metric = (payload.get("config") or {}).get("default_metric") or "total_tokens"
    return metric if metric in _METRIC_LABEL else "total_tokens"


def _metric_val(row: dict[str, Any], metric: str) -> float:
    return 1.0 if metric == "sessions" else float(row.get(metric) or 0)


def _fmt_metric(value: float, metric: str) -> str:
    if metric == "cost":
        return _fmt_money(value)
    return _fmt_int(value) if metric == "sessions" else _fmt_compact(value)


def _group(rows: list[dict[str, Any]], key: str, metric: str) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    fallback = "(unknown)" if key == "model" else "(none)"
    for r in rows:
        k = r.get(key) or fallback
        out[k] = out.get(k, 0.0) + _metric_val(r, metric)
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def _bars(pairs: list[tuple[str, float]], fmt, limit: int = 15) -> str:
    top = pairs[:limit]
    if not top:
        return '<div class="muted">No data.</div>'
    # Do NOT assume `pairs` is sorted: callers that pass payload-order lists
    # (subscriptions) would otherwise scale every bar to a non-maximal peak and
    # render widths past 100%, which `overflow:hidden` silently clips to "full".
    peak = max((v for _, v in top), default=0.0) or 1
    out = []
    for i, (label, val) in enumerate(top):
        width = max(1.0, val / peak * 100) if peak else 0.0
        colour = _PALETTE[i % len(_PALETTE)]
        out.append(
            f'<div class="bar-row"><span class="lab" title="{_esc(label)}">{_esc(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{width:.2f}%;'
            f'background:{colour}"></span></span>'
            f'<span class="val">{fmt(val)}</span></div>'
        )
    return "".join(out)


def _panel(title: str, body: str, hint: str = "") -> str:
    hint_html = f' <span class="hint">{_esc(hint)}</span>' if hint else ""
    return f'<div class="panel"><h2>{_esc(title)}{hint_html}</h2>{body}</div>'


def _table(headers: list[tuple[str, bool]], body_rows: list[str]) -> str:
    """Render a table. `headers` is (label, is_numeric); `body_rows` are <tr> strings."""
    head = "".join(
        f'<th class="{"num" if num else ""}">{_esc(label)}</th>' for label, num in headers
    )
    if not body_rows:
        body_rows = [f'<tr><td colspan="{len(headers)}" class="muted">No rows.</td></tr>']
    return (
        '<div class="tbl-scroll"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _static_cards(sessions: list[dict[str, Any]], payload: dict[str, Any]) -> str:
    def total(field: str) -> float:
        return sum(float(s.get(field) or 0) for s in sessions)

    cost = total("cost")
    sub_total = float(payload.get("subscription_total_usd") or 0)
    top_provider = _group(sessions, "provider", "total_tokens")
    cards = [
        ("Total tokens", _fmt_int(total("total_tokens"))),
        ("Input", _fmt_compact(total("input_tokens"))),
        ("Output", _fmt_compact(total("output_tokens"))),
        ("Cache read", _fmt_compact(total("cache_read_tokens"))),
        ("API-equiv cost", _fmt_money(cost)),
        ("Cash paid", _fmt_money(sub_total)),
        ("Value multiple", _fixed(cost / sub_total, 2) + "x" if sub_total else "&mdash;"),
        ("Sessions", _fmt_int(len(sessions))),
        ("Providers", _fmt_int(len({s.get("provider") for s in sessions}))),
        ("Models", _fmt_int(len({s.get("model") or "(unknown)" for s in sessions}))),
        ("Projects", _fmt_int(len({s.get("project") or "(none)" for s in sessions}))),
        ("Top provider", _esc(top_provider[0][0]) if top_provider else "&mdash;"),
    ]
    # Card labels and formatted numbers are app-controlled; only the top-provider
    # value is log-derived and it is escaped above.
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="k">{_html.escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in cards
    ) + "</div>"


def _static_daily(sessions: list[dict[str, Any]], metric: str = "total_tokens") -> str:
    by_day: dict[str, float] = {}
    for s in sessions:
        day = s.get("date") or "?"
        by_day[day] = by_day.get(day, 0.0) + _metric_val(s, metric)
    if not by_day:
        return '<div class="muted">No data.</div>'
    peak = max(by_day.values()) or 1
    out = []
    for day in sorted(by_day):
        val = by_day[day]
        width = max(1.0, val / peak * 100)
        out.append(
            f'<div class="bar-row"><span class="lab">{_esc(day)}</span>'
            f'<span class="track"><span class="fill" style="width:{width:.2f}%;'
            f'background:var(--accent)"></span></span>'
            f'<span class="val">{_fmt_metric(val, metric)}</span></div>'
        )
    return "".join(out)


def _static_sessions(sessions: list[dict[str, Any]]) -> str:
    headers = [
        ("Date", False), ("Provider", False), ("Model", False), ("Project", False),
        ("Session", False), ("Input", True), ("Output", True), ("Cache R", True),
        ("Cache W", True), ("Total", True), ("Cost $", True), ("Confidence", False),
    ]
    shown = sessions[:_STATIC_MAX_ROWS]
    rows = []
    for s in shown:
        rows.append(
            "<tr>"
            f"<td>{_esc(s.get('date'))}</td>"
            f"<td>{_esc(s.get('provider'))}</td>"
            f"<td>{_esc(s.get('model'))}</td>"
            f"<td>{_esc(s.get('project'))}</td>"
            f'<td><span class="tag">{_esc((s.get("session") or "")[:12])}</span></td>'
            f'<td class="num">{_fmt_int(s.get("input_tokens"))}</td>'
            f'<td class="num">{_fmt_int(s.get("output_tokens"))}</td>'
            f'<td class="num">{_fmt_int(s.get("cache_read_tokens"))}</td>'
            f'<td class="num">{_fmt_int(s.get("cache_creation_tokens"))}</td>'
            f'<td class="num">{_fmt_int(s.get("total_tokens"))}</td>'
            f'<td class="num">{_fmt_money(s.get("cost"))}</td>'
            f"<td>{_esc(s.get('confidence'))}</td>"
            "</tr>"
        )
    table = _table(headers, rows)
    if len(sessions) > len(shown):
        table += (
            f'<div class="muted" style="margin-top:8px">Showing the top {len(shown):,} of '
            f"{len(sessions):,} sessions. Enable JavaScript for the full sortable, "
            "searchable table and CSV export.</div>"
        )
    return table


def _static_pricing(payload: dict[str, Any]) -> str:
    headers = [
        ("Provider", False), ("Model", False), ("Input /M", True), ("Output /M", True),
        ("Cache W /M", True), ("Cache R /M", True), ("Effective", False), ("Status", False),
    ]

    def money(x: Any) -> str:
        return "&mdash;" if x is None else _fmt_money(x)

    rows = []
    for p in payload.get("pricing", []):
        if p.get("status") == "missing":
            status = '<span class="tag" style="color:#f7c948;border-color:#f7c948">missing</span>'
        elif p.get("status") == "approximate":
            status = (
                '<span class="tag" style="color:#ff8a5b;border-color:#ff8a5b" '
                f'title="no exact row; rate taken from {_esc(p.get("priced_as"))}">'
                f'~ priced as {_esc(p.get("priced_as"))}</span>'
            )
        else:
            status = '<span class="tag">exact</span>'
        rows.append(
            "<tr>"
            f"<td>{_esc(p.get('provider'))}</td>"
            f"<td>{_esc(p.get('model'))}</td>"
            f'<td class="num">{money(p.get("input_per_million_usd"))}</td>'
            f'<td class="num">{money(p.get("output_per_million_usd"))}</td>'
            f'<td class="num">{money(p.get("cache_write_per_million_usd"))}</td>'
            f'<td class="num">{money(p.get("cache_read_per_million_usd"))}</td>'
            f"<td>{_esc(p.get('effective_date'))}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )
    return _table(headers, rows)


def render_static_fallback(payload: dict[str, Any]) -> str:
    """Server-rendered no-JavaScript view of the same payload.

    Visible by default; the front-end hides it once `init()` succeeds. This is
    what makes the artifact readable in preview panes, snapshot renderers, and
    any environment where the script does not run or throws.
    """
    sessions = payload.get("sessions", [])
    meta = payload.get("meta", {})
    rng = meta.get("range", {})

    parts: list[str] = [
        '<div class="static-note" id="tb-static-note">'
        "<b>Static view.</b> The interactive charts, filters, and CSV export need "
        "JavaScript — this page is showing the same numbers as plain HTML instead. "
        "Open this file directly in a browser to get the full dashboard."
        "</div>",
        f'<div class="sub" style="margin:8px 0 4px">{_esc(rng.get("start"))} &rarr; '
        f'{_esc(rng.get("end"))} &middot; {_esc(meta.get("timezone"))}'
        + (
            f' &middot; provider: {_esc(meta.get("provider_filter"))}'
            if meta.get("provider_filter")
            else ""
        )
        + "</div>",
    ]

    warnings = payload.get("warnings") or []
    if warnings:
        parts.append(
            '<div class="warnings">'
            + "".join(f'<div class="warn">&#9888; {_esc(w)}</div>' for w in warnings)
            + "</div>"
        )

    parts.append(_static_cards(sessions, payload))

    if not sessions:
        parts.append(
            _panel("Sessions", '<div class="muted">No usage events in this range.</div>')
        )
        return "".join(parts)

    # Mirror the metric the interactive view boots with, so the same file does
    # not rank models by tokens with scripts off and by cost with them on.
    metric = _metric_of(payload)
    label = _METRIC_LABEL[metric]

    def as_metric(v: float) -> str:
        return _fmt_metric(v, metric)

    parts.append(
        '<div class="grid2">'
        + _panel("Provider share", _bars(_group(sessions, "provider", metric), as_metric, 8), label)
        + _panel("Cost by provider", _bars(_group(sessions, "provider", "cost"), _fmt_money, 8), "API-equiv $")
        + "</div>"
    )
    parts.append(_panel("Daily usage trend", _static_daily(sessions, metric), label))
    parts.append(
        '<div class="grid2">'
        + _panel("Top models", _bars(_group(sessions, "model", metric), as_metric), label)
        + _panel("Top projects", _bars(_group(sessions, "project", metric), as_metric), label)
        + "</div>"
    )

    black_holes = sorted(sessions, key=lambda s: float(s.get("cost") or 0), reverse=True)[:10]
    parts.append(
        _panel(
            "Token black holes",
            _bars(
                [
                    (f"{s.get('provider')} · {s.get('project') or '(none)'}", float(s.get("cost") or 0))
                    for s in black_holes
                ],
                _fmt_money,
                10,
            ),
            "most expensive sessions",
        )
    )

    subs = payload.get("subscriptions") or []
    if subs:
        # Sorted high-to-low and never truncated, matching subsChart in the JS.
        sub_pairs = sorted(
            (
                (
                    f"{s.get('name')} ({_fmt_money(s.get('api_equiv_value_usd'))}"
                    f" value / {_fmt_money(s.get('monthly_cost_usd'))} paid)",
                    float(s.get("value_multiple") or 0),
                )
                for s in subs
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        parts.append(
            _panel(
                "Subscription value",
                _bars(sub_pairs, lambda v: _fixed(v, 1) + "x", len(sub_pairs)),
                "API-equiv value ÷ cash paid",
            )
        )

    parts.append(_panel("Sessions", _static_sessions(sessions)))
    parts.append(_panel("Pricing assumptions", _static_pricing(payload)))

    present = {s.get("provider") for s in sessions}
    caveats = "".join(
        f'<div class="caveat"><b>{_esc(k)}:</b> {_esc(v)}</div>'
        for k, v in (payload.get("caveats") or {}).items()
        if k in present
    )
    parts.append(_panel("Provider caveats & data provenance", caveats or '<div class="muted">None.</div>'))

    priv = payload.get("privacy") or {}
    bits = [
        "Generated locally — no data left this machine.",
        "&#9888; raw prompts stored" if priv.get("store_raw_prompts") else "Raw prompts excluded",
        "&#9888; raw messages stored" if priv.get("store_raw_messages") else "Raw messages excluded",
        "Source code &amp; file contents excluded",
        "Home directory redacted" if priv.get("redact_home_dir") else "Home directory NOT redacted",
        "Project paths hashed" if priv.get("hash_project_paths") else "Project paths shown",
    ]
    parts.append(
        '<div class="foot">Privacy: '
        + " &middot; ".join(bits)
        + f'<br>Generated {_esc(meta.get("generated_at"))} &middot; timezone '
        + f'{_esc(meta.get("timezone"))} &middot; TokenBurn dashboard (static view).</div>'
    )
    return "".join(parts)


def render_dashboard_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, default=str)
    # Prevent the embedded JSON from prematurely closing the <script> block.
    data_json = data_json.replace("</", "<\\/")
    static_html = render_static_fallback(payload)

    # Single pass so a placeholder-looking string inside one substitution can
    # never be rescanned and replaced by the other.
    subs = {"__TOKENBURN_DATA__": data_json, "__TOKENBURN_STATIC__": static_html}
    # A function replacement is used verbatim — no backreference expansion —
    # so payload text containing "\1" or "\g<0>" stays literal.
    return re.sub(
        "__TOKENBURN_DATA__|__TOKENBURN_STATIC__",
        lambda m: subs[m.group(0)],
        _HTML_TEMPLATE,
    )


# ---------------------------------------------------------------------------
# Standalone HTML template. Single string; everything inlined for offline use.
# `.replace` (not f-strings) so literal CSS/JS braces need no escaping.
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TokenBurn Dashboard</title>
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
  --fg:#e6e9ef; --muted:#9aa3b2; --accent:#5b9cff;
  --c0:#5b9cff;--c1:#ff8a5b;--c2:#36d399;--c3:#f7c948;--c4:#b072ff;--c5:#ff6b9d;--c6:#56d4dd;--c7:#9aa3b2;
}
@media (prefers-color-scheme: light){
  :root{--bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--line:#e2e6ec;--fg:#1a1d23;--muted:#5b6472;}
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent)}
header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px}
header h1{font-size:20px;margin:0}
header .sub{color:var(--muted);font-size:13px}
.wrap{padding:20px 24px;max-width:1280px;margin:0 auto}
.estimate-note{color:var(--muted);font-size:12px;margin-left:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
.card .v small{font-size:13px;color:var(--muted);font-weight:400}
.warnings{margin:8px 0}
.warn{background:rgba(247,201,72,.12);border:1px solid rgba(247,201,72,.4);color:var(--fg);border-radius:8px;padding:10px 14px;margin:6px 0;font-size:13px}
.controls{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:16px 0;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end}
.controls .group{display:flex;flex-direction:column;gap:4px}
.controls label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.controls select,.controls input{background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px}
.multi{position:relative}
.multi-btn{cursor:pointer;min-width:140px;text-align:left}
.multi-pop{display:none;position:absolute;z-index:20;top:100%;left:0;margin-top:4px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px;max-height:240px;overflow:auto;min-width:200px;box-shadow:0 8px 24px rgba(0,0,0,.3)}
.multi.open .multi-pop{display:block}
.multi-pop label{display:flex;gap:8px;align-items:center;text-transform:none;letter-spacing:0;font-size:13px;color:var(--fg);padding:3px 2px;cursor:pointer}
.btn{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:7px 12px;font-size:13px;cursor:pointer}
.btn.secondary{background:var(--panel2);color:var(--fg);border:1px solid var(--line)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}
.panel h2{font-size:15px;margin:0 0 12px}
.panel h2 .hint{font-weight:400;color:var(--muted);font-size:12px;margin-left:6px}
.bar-row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:13px}
.bar-row .lab{width:38%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-row .track{flex:1;background:var(--panel2);border-radius:4px;height:16px;overflow:hidden}
.bar-row .fill{display:block;height:100%;border-radius:4px;flex-shrink:0}
.bar-row .val{width:84px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.legend{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px;font-size:12px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.donut-wrap{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{cursor:pointer;user-select:none;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
.tbl-scroll{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.tbl-tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px}
.pager{display:flex;gap:8px;align-items:center;margin-top:10px;color:var(--muted);font-size:13px}
.colpick{position:relative}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;border:1px solid var(--line);color:var(--muted)}
.foot{color:var(--muted);font-size:12px;margin:24px 0 40px}
.caveat{font-size:12.5px;color:var(--muted);margin:6px 0}
.caveat b{color:var(--fg)}
.muted{color:var(--muted)}
svg{display:block}
/* Progressive enhancement: [hidden] must beat the display rules above, so the
   static view and the interactive view can never both be on screen. */
[hidden]{display:none !important}
.static-note{background:rgba(91,156,255,.12);border:1px solid rgba(91,156,255,.4);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:13px}
</style>
</head>
<body>
<header>
  <h1>TokenBurn Dashboard</h1>
  <span class="sub" id="subtitle"></span>
  <span class="estimate-note">All dollar figures are <b>API-equivalent estimates</b>, not vendor cost.</span>
</header>
<div class="wrap">
  <!-- Server-rendered, visible by default. Hidden by init() once the
       interactive view is fully built, so a blocked or broken script always
       leaves real numbers on screen instead of an empty shell. -->
  <div id="tb-static">
    <div class="warn" id="tb-boot-error" hidden></div>
    __TOKENBURN_STATIC__
  </div>

  <div id="tb-app" hidden>
  <div class="warnings" id="warnings"></div>
  <div class="cards" id="cards"></div>

  <div class="controls" id="controls">
    <div class="group"><label>Metric</label>
      <select id="metric">
        <option value="total_tokens">Total tokens</option>
        <option value="input_tokens">Input tokens</option>
        <option value="output_tokens">Output tokens</option>
        <option value="cache_read_tokens">Cache read</option>
        <option value="cost">API-equiv cost</option>
        <option value="sessions">Session count</option>
      </select>
    </div>
    <div class="group"><label>Provider</label><div class="multi" id="f-provider"></div></div>
    <div class="group"><label>Model</label><div class="multi" id="f-model"></div></div>
    <div class="group"><label>Project</label><div class="multi" id="f-project"></div></div>
    <div class="group"><label>Confidence</label><div class="multi" id="f-confidence"></div></div>
    <div class="group"><label>&nbsp;</label><button class="btn secondary" id="reset">Reset filters</button></div>
  </div>

  <div class="grid2">
    <div class="panel"><h2>Provider share <span class="hint" id="h-provshare"></span></h2><div class="donut-wrap"><div id="provshare-donut"></div><div class="legend" id="provshare-legend"></div></div></div>
    <div class="panel"><h2>Cost by provider <span class="hint">API-equiv $</span></h2><div id="cost-provider"></div></div>
  </div>

  <div class="panel"><h2>Daily usage trend <span class="hint" id="h-daily"></span></h2><div id="daily"></div></div>

  <div class="grid2">
    <div class="panel"><h2>Top models <span class="hint" id="h-models"></span></h2><div id="models"></div></div>
    <div class="panel"><h2>Top projects <span class="hint" id="h-projects"></span></h2><div id="projects"></div></div>
  </div>

  <div class="grid2">
    <div class="panel"><h2>Token composition <span class="hint">by provider</span></h2><div id="composition"></div><div class="legend" id="composition-legend"></div></div>
    <div class="panel"><h2>Data quality <span class="hint">by confidence</span></h2><div class="donut-wrap"><div id="conf-donut"></div><div class="legend" id="conf-legend"></div></div></div>
  </div>

  <div class="panel"><h2>Subscription value <span class="hint">API-equiv value ÷ cash paid</span></h2><div id="subs"></div></div>

  <div class="panel">
    <h2>Token black holes <span class="hint">most expensive sessions (current filters)</span></h2>
    <div id="blackholes"></div>
  </div>

  <div class="panel" id="session-panel">
    <h2>Sessions</h2>
    <div class="tbl-tools">
      <input id="search" placeholder="Search sessions…" style="min-width:220px">
      <div class="colpick multi" id="colpick"></div>
      <button class="btn secondary" id="export-csv">Export filtered CSV</button>
      <span class="muted" id="rowcount"></span>
    </div>
    <div class="tbl-scroll"><table id="sess-table"><thead></thead><tbody></tbody></table></div>
    <div class="pager">
      <button class="btn secondary" id="prev">Prev</button>
      <span id="pageinfo"></span>
      <button class="btn secondary" id="next">Next</button>
    </div>
  </div>

  <div class="panel"><h2>Pricing assumptions</h2><div class="tbl-scroll"><table id="pricing-table"><thead></thead><tbody></tbody></table></div></div>

  <div class="panel">
    <h2>Provider caveats &amp; data provenance</h2>
    <div id="caveats"></div>
  </div>

  <div class="foot" id="privacy"></div>
  </div>
</div>

<script id="tokenburn-data" type="application/json">__TOKENBURN_DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById('tokenburn-data').textContent);
const SESS = DATA.sessions;
const PALETTE = ['#5b9cff','#ff8a5b','#36d399','#f7c948','#b072ff','#ff6b9d','#56d4dd','#9aa3b2'];
const METRIC_LABEL = {total_tokens:'Total tokens',input_tokens:'Input',output_tokens:'Output',cache_read_tokens:'Cache read',cost:'API-equiv $',sessions:'Sessions'};

const state = {
  metric: DATA.config.default_metric || 'total_tokens',
  provider: new Set(), model: new Set(), project: new Set(), confidence: new Set(),
  search: '', sort: 'total_tokens', dir: -1, page: 1, pageSize: 50,
  hidden: new Set()
};

const COLS = [
  {k:'date',label:'Date',num:false},
  {k:'provider',label:'Provider',num:false},
  {k:'tool',label:'Tool',num:false},
  {k:'model',label:'Model',num:false},
  {k:'project',label:'Project',num:false},
  {k:'session',label:'Session',num:false},
  {k:'input_tokens',label:'Input',num:true},
  {k:'output_tokens',label:'Output',num:true},
  {k:'cache_read_tokens',label:'Cache R',num:true},
  {k:'cache_creation_tokens',label:'Cache W',num:true},
  {k:'reasoning_tokens',label:'Reason',num:true},
  {k:'total_tokens',label:'Total',num:true},
  {k:'cost',label:'Cost $',num:true},
  {k:'confidence',label:'Confidence',num:false},
  {k:'source',label:'Source',num:false},
];

// ---------- formatting ----------
const fmtInt = n => Math.round(n).toLocaleString();
const fmtMoney = n => '$' + (n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function fmtCompact(n){
  n = +n||0; const a = Math.abs(n);
  if(a>=1e9) return (n/1e9).toFixed(2)+'B';
  if(a>=1e6) return (n/1e6).toFixed(2)+'M';
  if(a>=1e3) return (n/1e3).toFixed(1)+'k';
  return ''+Math.round(n);
}
const metricVal = (row, m) => m==='sessions' ? 1 : (row[m]||0);
const fmtMetric = (v, m) => m==='cost' ? fmtMoney(v) : (m==='sessions' ? fmtInt(v) : fmtCompact(v));

// ---------- filtering ----------
function passes(row){
  if(state.provider.size && !state.provider.has(row.provider)) return false;
  if(state.model.size && !state.model.has(row.model||'(unknown)')) return false;
  if(state.project.size && !state.project.has(row.project||'(none)')) return false;
  if(state.confidence.size && !state.confidence.has(row.confidence)) return false;
  if(state.search){
    const q = state.search.toLowerCase();
    const hay = [row.provider,row.model,row.project,row.session,row.confidence,row.source].map(x=>(x||'').toLowerCase()).join(' ');
    if(!hay.includes(q)) return false;
  }
  return true;
}
const filtered = () => SESS.filter(passes);

function groupSum(rows, key, metric){
  const out = new Map();
  for(const r of rows){
    // A null/empty model must read '(unknown)' — matching the model filter,
    // the markdown report, and the static view. The old form fell through to
    // '(none)' here while the filter listed '(unknown)', so the chart label
    // and its own filter disagreed.
    const k = (r[key]==null||r[key]==='') ? (key==='model'?'(unknown)':'(none)') : r[key];
    out.set(k, (out.get(k)||0) + metricVal(r, metric));
  }
  return [...out.entries()].sort((a,b)=>b[1]-a[1]);
}

// ---------- charts (vanilla SVG) ----------
function barList(el, pairs, metric, max){
  const top = pairs.slice(0, max||15);
  const peak = top.length ? top[0][1] : 1;
  el.innerHTML = top.map((p,i)=>{
    const w = peak>0 ? Math.max(1,(p[1]/peak*100)) : 0;
    const col = PALETTE[i%PALETTE.length];
    return `<div class="bar-row"><span class="lab" title="${esc(p[0])}">${esc(p[0])}</span>`+
           `<span class="track"><span class="fill" style="width:${w}%;background:${col}"></span></span>`+
           `<span class="val">${fmtMetric(p[1],metric)}</span></div>`;
  }).join('') || '<div class="muted">No data.</div>';
}

function donut(elDonut, elLegend, pairs, metric){
  const total = pairs.reduce((a,b)=>a+b[1],0);
  const top = pairs.slice(0,7);
  const rest = pairs.slice(7).reduce((a,b)=>a+b[1],0);
  if(rest>0) top.push(['Other', rest]);
  const R=52, C=2*Math.PI*R; let off=0;
  const segs = top.map((p,i)=>{
    const frac = total>0 ? p[1]/total : 0;
    const len = frac*C;
    const col = PALETTE[i%PALETTE.length];
    const s = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${col}" stroke-width="20" stroke-dasharray="${len} ${C-len}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"></circle>`;
    off += len; return s;
  }).join('');
  elDonut.innerHTML = `<svg width="140" height="140" viewBox="0 0 140 140">${segs}`+
    `<text x="70" y="66" text-anchor="middle" fill="var(--fg)" font-size="13" font-weight="600">${fmtMetric(total,metric)}</text>`+
    `<text x="70" y="84" text-anchor="middle" fill="var(--muted)" font-size="10">${METRIC_LABEL[metric]}</text></svg>`;
  elLegend.innerHTML = top.map((p,i)=>{
    const pct = total>0?(p[1]/total*100).toFixed(0):0;
    return `<span><i style="background:${PALETTE[i%PALETTE.length]}"></i>${esc(p[0])} · ${pct}%</span>`;
  }).join('');
}

function dailyChart(el, rows, metric){
  const m = new Map();
  for(const r of rows){ const d=r.date||'?'; m.set(d,(m.get(d)||0)+metricVal(r,metric)); }
  const days = [...m.keys()].sort();
  if(!days.length){ el.innerHTML='<div class="muted">No data.</div>'; return; }
  const vals = days.map(d=>m.get(d));
  const peak = Math.max(...vals,1);
  const W=Math.max(560, days.length*22), H=200, padL=8, padB=22, padT=10;
  const bw = (W-padL)/days.length;
  const bars = days.map((d,i)=>{
    const h=(vals[i]/peak)*(H-padB-padT);
    const x=padL+i*bw, y=H-padB-h;
    return `<rect x="${x+2}" y="${y}" width="${Math.max(1,bw-4)}" height="${h}" fill="var(--accent)" rx="2"><title>${d}: ${fmtMetric(vals[i],metric)}</title></rect>`;
  }).join('');
  const ticks = days.map((d,i)=> (i%Math.ceil(days.length/12||1)===0)?`<text x="${padL+i*bw+bw/2}" y="${H-6}" text-anchor="middle" fill="var(--muted)" font-size="9">${d.slice(5)}</text>`:'').join('');
  el.innerHTML = `<div style="overflow-x:auto"><svg width="${W}" height="${H}">${bars}${ticks}</svg></div>`;
}

function composition(el, elLegend, rows){
  const keys=['input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens','reasoning_tokens'];
  const labels=['Input','Output','Cache R','Cache W','Reason'];
  const provs = groupSum(rows,'provider','total_tokens').map(p=>p[0]);
  el.innerHTML = provs.map(pv=>{
    const sub = rows.filter(r=>r.provider===pv);
    const parts = keys.map(k=>sub.reduce((a,r)=>a+(r[k]||0),0));
    const tot = parts.reduce((a,b)=>a+b,0)||1;
    const seg = parts.map((v,i)=>v>0?`<span class="fill" style="width:${v/tot*100}%;background:${PALETTE[i%PALETTE.length]}" title="${labels[i]}: ${fmtCompact(v)}"></span>`:'').join('');
    return `<div class="bar-row"><span class="lab" title="${esc(pv)}">${esc(pv)}</span><span class="track" style="display:flex">${seg}</span><span class="val">${fmtCompact(tot)}</span></div>`;
  }).join('') || '<div class="muted">No data.</div>';
  elLegend.innerHTML = labels.map((l,i)=>`<span><i style="background:${PALETTE[i%PALETTE.length]}"></i>${l}</span>`).join('');
}

function subsChart(el, rows){
  // Recompute API-equiv value per subscription from filtered rows.
  const provCost = new Map();
  for(const r of rows) provCost.set(r.provider,(provCost.get(r.provider)||0)+(r.cost||0));
  const data = DATA.subscriptions.map(s=>{
    const val = s.providers.reduce((a,p)=>a+(provCost.get(p)||0),0);
    const mult = s.monthly_cost_usd ? val/s.monthly_cost_usd : 0;
    return {name:s.name, paid:s.monthly_cost_usd, val, mult};
  }).sort((a,b)=>b.mult-a.mult);
  const peak = Math.max(...data.map(d=>d.mult),1);
  el.innerHTML = data.map((d,i)=>{
    const w=Math.max(1,d.mult/peak*100);
    return `<div class="bar-row"><span class="lab" title="${esc(d.name)}">${esc(d.name)}</span>`+
      `<span class="track"><span class="fill" style="width:${w}%;background:${PALETTE[i%PALETTE.length]}"></span></span>`+
      `<span class="val">${d.mult.toFixed(1)}x</span></div>`+
      `<div class="muted" style="margin:-2px 0 6px 38%;font-size:11px">${fmtMoney(d.val)} value · ${fmtMoney(d.paid)} paid</div>`;
  }).join('') || '<div class="muted">No subscriptions configured.</div>';
}

// ---------- summary cards ----------
function cards(rows){
  const sum = k => rows.reduce((a,r)=>a+(r[k]||0),0);
  const tot = sum('total_tokens'), cost = sum('cost');
  const subTotal = DATA.subscription_total_usd||0;
  const provs = new Set(rows.map(r=>r.provider)).size;
  const models = new Set(rows.map(r=>r.model||'(unknown)')).size;
  const projects = new Set(rows.map(r=>r.project||'(none)')).size;
  const mult = subTotal ? (cost/subTotal) : null;
  const topProv = groupSum(rows,'provider','total_tokens')[0];
  const topProj = groupSum(rows,'project','total_tokens')[0];
  const c = [
    ['Total tokens', fmtInt(tot)],
    ['Input', fmtCompact(sum('input_tokens'))],
    ['Output', fmtCompact(sum('output_tokens'))],
    ['Cache read', fmtCompact(sum('cache_read_tokens'))],
    ['API-equiv cost', fmtMoney(cost)],
    ['Cash paid', fmtMoney(subTotal)],
    ['Value multiple', mult!=null? mult.toFixed(2)+'x':'—'],
    ['Sessions', fmtInt(rows.length)],
    ['Providers', provs],
    ['Models', models],
    ['Projects', projects],
    ['Top provider', topProv? topProv[0]:'—'],
  ];
  document.getElementById('cards').innerHTML = c.map(x=>`<div class="card"><div class="k">${x[0]}</div><div class="v">${x[1]}</div></div>`).join('');
}

// ---------- table ----------
function thead(){
  document.querySelector('#sess-table thead').innerHTML = '<tr>'+COLS.filter(c=>!state.hidden.has(c.k)).map(c=>{
    const arrow = state.sort===c.k ? (state.dir<0?' ▼':' ▲') : '';
    return `<th class="${c.num?'num':''}" data-k="${c.k}">${c.label}${arrow}</th>`;
  }).join('')+'</tr>';
  document.querySelectorAll('#sess-table th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k;
    if(state.sort===k) state.dir*=-1; else {state.sort=k; state.dir=COLS.find(c=>c.k===k).num?-1:1;}
    state.page=1; renderTable();
  });
}
function sortedRows(){
  const rows = filtered();
  const c = COLS.find(x=>x.k===state.sort)||{num:true};
  rows.sort((a,b)=>{
    let va=a[state.sort], vb=b[state.sort];
    if(c.num){ va=+va||0; vb=+vb||0; return (va-vb)*state.dir; }
    va=(va||'').toString(); vb=(vb||'').toString();
    return va.localeCompare(vb)*state.dir;
  });
  return rows;
}
function renderTable(){
  const rows = sortedRows();
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total/state.pageSize));
  if(state.page>pages) state.page=pages;
  const start=(state.page-1)*state.pageSize;
  const slice = rows.slice(start, start+state.pageSize);
  const cols = COLS.filter(c=>!state.hidden.has(c.k));
  document.querySelector('#sess-table tbody').innerHTML = slice.map(r=>'<tr>'+cols.map(c=>{
    let v=r[c.k];
    if(c.num) v = (c.k==='cost') ? fmtMoney(r.cost) : fmtInt(r[c.k]||0);
    else if(c.k==='session') v=`<span class="tag">${esc((r.session||'').slice(0,12))}</span>`;
    else v=esc(v==null||v===''?'—':v);
    return `<td class="${c.num?'num':''}">${v}</td>`;
  }).join('')+'</tr>').join('');
  document.getElementById('rowcount').textContent = `${total.toLocaleString()} sessions`;
  document.getElementById('pageinfo').textContent = `Page ${state.page} / ${pages}`;
}

// ---------- pricing / caveats / privacy ----------
function renderPricing(){
  const cols=['Provider','Model','Input /M','Output /M','Cache W /M','Cache R /M','Effective','Status'];
  document.querySelector('#pricing-table thead').innerHTML='<tr>'+cols.map((c,i)=>`<th class="${i>=2&&i<=5?'num':''}">${c}</th>`).join('')+'</tr>';
  document.querySelector('#pricing-table tbody').innerHTML = DATA.pricing.map(p=>{
    const m = x=> x==null?'—':fmtMoney(x);
    const stat = p.status==='missing'
      ? '<span class="tag" style="color:#f7c948;border-color:#f7c948">missing</span>'
      : (p.status==='approximate'
          ? `<span class="tag" style="color:#ff8a5b;border-color:#ff8a5b" title="no exact row; rate taken from ${esc(p.priced_as)}">~ priced as ${esc(p.priced_as)}</span>`
          : '<span class="tag">exact</span>');
    const model = p.source_url? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener">${esc(p.model)}</a>`:esc(p.model);
    return `<tr><td>${esc(p.provider)}</td><td>${model}</td><td class="num">${m(p.input_per_million_usd)}</td><td class="num">${m(p.output_per_million_usd)}</td><td class="num">${m(p.cache_write_per_million_usd)}</td><td class="num">${m(p.cache_read_per_million_usd)}</td><td>${esc(p.effective_date||'—')}</td><td>${stat}</td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">No pricing rows.</td></tr>';
}
function renderCaveats(){
  const present = new Set(SESS.map(r=>r.provider));
  document.getElementById('caveats').innerHTML = Object.entries(DATA.caveats)
    .filter(([k])=>present.has(k))
    .map(([k,v])=>`<div class="caveat"><b>${esc(k)}:</b> ${esc(v)}</div>`).join('')
    + `<div class="caveat" style="margin-top:10px">Confidence labels: <b>exact_from_local_log</b> (Claude/Codex), <b>exact_from_provider_log</b> (Gemini API), <b>manual_import</b> (Cursor/hand-imported), <b>estimated_from_session_summary</b> (Codex cumulative fallback). The daily trend attributes each session to its first local date.</div>`;
}
function renderPrivacy(){
  const p=DATA.privacy;
  const parts=[
    'Generated locally — no data left this machine.',
    p.store_raw_prompts?'⚠ raw prompts stored':'Raw prompts excluded',
    p.store_raw_messages?'⚠ raw messages stored':'Raw messages excluded',
    'Source code &amp; file contents excluded',
    p.redact_home_dir?'Home directory redacted':'Home directory NOT redacted',
    p.hash_project_paths?'Project paths hashed':'Project paths shown',
  ];
  document.getElementById('privacy').innerHTML = 'Privacy: '+parts.join(' · ')+
    `<br>Generated ${esc(DATA.meta.generated_at)} · timezone ${esc(DATA.meta.timezone)} · TokenBurn dashboard.`;
}

// ---------- filter widgets ----------
function buildMulti(id, field, getKey){
  const opts = [...new Set(SESS.map(getKey))].sort((a,b)=>(''+a).localeCompare(''+b));
  const el = document.getElementById(id);
  el.classList.add('multi');
  el.innerHTML = `<div class="multi-btn btn secondary">All</div><div class="multi-pop">`+
    opts.map(o=>`<label><input type="checkbox" value="${esc(o)}">${esc(o)}</label>`).join('')+`</div>`;
  const btn=el.querySelector('.multi-btn');
  btn.onclick=(e)=>{e.stopPropagation(); document.querySelectorAll('.multi.open').forEach(m=>{if(m!==el)m.classList.remove('open')}); el.classList.toggle('open');};
  el.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
    const set=state[field]; set.clear();
    el.querySelectorAll('input:checked').forEach(c=>set.add(c.value));
    btn.textContent = set.size? `${set.size} selected` : 'All';
    state.page=1; renderAll();
  });
}
function buildColpick(){
  const el=document.getElementById('colpick');
  el.innerHTML=`<div class="multi-btn btn secondary">Columns</div><div class="multi-pop">`+
    COLS.map(c=>`<label><input type="checkbox" value="${c.k}" ${state.hidden.has(c.k)?'':'checked'}>${c.label}</label>`).join('')+`</div>`;
  const btn=el.querySelector('.multi-btn');
  btn.onclick=(e)=>{e.stopPropagation(); document.querySelectorAll('.multi.open').forEach(m=>{if(m!==el)m.classList.remove('open')}); el.classList.toggle('open');};
  el.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
    if(cb.checked) state.hidden.delete(cb.value); else state.hidden.add(cb.value);
    thead(); renderTable();
  });
}
document.addEventListener('click', ()=>document.querySelectorAll('.multi.open').forEach(m=>m.classList.remove('open')));

// ---------- CSV export ----------
function exportCSV(){
  const rows = sortedRows();
  const header = COLS.map(c=>c.label);
  const lines = [header.join(',')];
  for(const r of rows){
    lines.push(COLS.map(c=>{
      let v=r[c.k]; if(v==null)v='';
      v=(''+v).replace(/"/g,'""');
      return /[",\n]/.test(v)?`"${v}"`:v;
    }).join(','));
  }
  const blob=new Blob([lines.join('\n')],{type:'text/csv'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=`tokenburn-sessions-${DATA.meta.label}.csv`;
  a.click(); URL.revokeObjectURL(a.href);
}

// ---------- render orchestration ----------
function renderAll(){
  const rows = filtered();
  const m = state.metric;
  document.getElementById('h-provshare').textContent = METRIC_LABEL[m];
  document.getElementById('h-daily').textContent = METRIC_LABEL[m];
  document.getElementById('h-models').textContent = METRIC_LABEL[m];
  document.getElementById('h-projects').textContent = METRIC_LABEL[m];
  cards(rows);
  donut(document.getElementById('provshare-donut'), document.getElementById('provshare-legend'), groupSum(rows,'provider',m), m);
  barList(document.getElementById('cost-provider'), groupSum(rows,'provider','cost'), 'cost', 8);
  dailyChart(document.getElementById('daily'), rows, m);
  barList(document.getElementById('models'), groupSum(rows,'model',m), m, 15);
  barList(document.getElementById('projects'), groupSum(rows,'project',m), m, 15);
  composition(document.getElementById('composition'), document.getElementById('composition-legend'), rows);
  donut(document.getElementById('conf-donut'), document.getElementById('conf-legend'), groupSum(rows,'confidence','total_tokens'), 'total_tokens');
  subsChart(document.getElementById('subs'), rows);
  // black holes = top sessions by cost under current filters
  const bh = [...rows].sort((a,b)=>(b.cost||0)-(a.cost||0)).slice(0,10);
  document.getElementById('blackholes').innerHTML = bh.map((r,i)=>{
    const peak=bh[0].cost||1, w=Math.max(1,(r.cost||0)/peak*100);
    return `<div class="bar-row"><span class="lab" title="${esc((r.project||'')+' · '+r.model)}">${esc(r.provider)} · ${esc(r.project||'(none)')}</span>`+
      `<span class="track"><span class="fill" style="width:${w}%;background:${PALETTE[i%PALETTE.length]}"></span></span>`+
      `<span class="val">${fmtMoney(r.cost)}</span></div>`+
      `<div class="muted" style="margin:-2px 0 6px 38%;font-size:11px">${fmtCompact(r.total_tokens)} tokens · ${esc((r.session||'').slice(0,12))}</div>`;
  }).join('') || '<div class="muted">No sessions.</div>';
  renderTable();
}

function esc(s){return (''+ (s==null?'':s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

// ---------- init ----------
function init(){
  const r=DATA.meta.range;
  let sub = `${r.start} → ${r.end} · ${DATA.meta.timezone}`;
  if(DATA.meta.provider_filter) sub += ` · provider: ${DATA.meta.provider_filter}`;
  document.getElementById('subtitle').textContent = sub;
  document.title = `TokenBurn — ${DATA.meta.label}`;
  document.getElementById('warnings').innerHTML = (DATA.warnings||[]).map(w=>`<div class="warn">⚠ ${esc(w)}</div>`).join('');

  document.getElementById('metric').value = state.metric;
  document.getElementById('metric').onchange = e=>{state.metric=e.target.value; renderAll();};
  buildMulti('f-provider','provider', r=>r.provider);
  buildMulti('f-model','model', r=>r.model||'(unknown)');
  buildMulti('f-project','project', r=>r.project||'(none)');
  buildMulti('f-confidence','confidence', r=>r.confidence);
  buildColpick();
  document.getElementById('reset').onclick=()=>{
    state.provider.clear();state.model.clear();state.project.clear();state.confidence.clear();
    state.search='';document.getElementById('search').value='';
    document.querySelectorAll('.controls .multi').forEach(el=>{el.querySelectorAll('input:checked').forEach(c=>c.checked=false);const b=el.querySelector('.multi-btn');if(b)b.textContent='All';});
    state.page=1; renderAll();
  };
  document.getElementById('search').oninput=e=>{state.search=e.target.value;state.page=1;renderTable();renderAll();};
  document.getElementById('prev').onclick=()=>{if(state.page>1){state.page--;renderTable();}};
  document.getElementById('next').onclick=()=>{state.page++;renderTable();};
  document.getElementById('export-csv').onclick=exportCSV;
  if(!DATA.meta){}
  if(SESS.length===0){document.getElementById('session-panel').insertAdjacentHTML('beforeend','<div class="muted">No usage events in this range.</div>');}

  thead();
  renderPricing();
  renderCaveats();
  renderPrivacy();
  renderAll();

  // Last step, and only on success: swap the server-rendered static view for
  // the interactive one. If anything above threw, the static view stays up.
  document.getElementById('tb-app').hidden = false;
  document.getElementById('tb-static').hidden = true;
}

try {
  init();
} catch (err) {
  // Keep the static view visible and say why the interactive one is missing.
  var banner = document.getElementById('tb-boot-error');
  if (banner) {
    banner.hidden = false;
    banner.textContent = '⚠ The interactive dashboard failed to load (' +
      ((err && err.message) ? err.message : String(err)) +
      '). Showing the static view below — all totals are still correct.';
  }
  var app = document.getElementById('tb-app');
  if (app) app.hidden = true;
  var stat = document.getElementById('tb-static');
  if (stat) stat.hidden = false;
  throw err;
}
</script>
</body>
</html>
"""
