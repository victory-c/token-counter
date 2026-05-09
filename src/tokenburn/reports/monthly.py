from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

from ..config import AppConfig
from ..models import DateRange
from ..util.paths import redact_home


def build_summary(db, range_: DateRange, cfg: AppConfig, provider_filter: str | None = None) -> dict[str, Any]:
    where_provider = "AND provider = :p" if provider_filter else ""
    params = {"s": range_.start.isoformat(), "e": range_.end.isoformat()}
    if provider_filter:
        params["p"] = provider_filter

    base_where = (
        "local_date BETWEEN :s AND :e " + where_provider
    )

    totals_sql = f"""
        SELECT
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
            COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
            COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
            COALESCE(SUM(total_tokens),0) AS total_tokens,
            COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd,
            COALESCE(SUM(billed_cost_usd),0) AS billed_cost_usd,
            COUNT(*) AS event_count
        FROM usage_events
        WHERE {base_where}
    """
    totals = next(iter(db.query(totals_sql, params)), {})

    by_provider = list(
        db.query(
            f"""
            SELECT provider,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost,
                   COALESCE(SUM(billed_cost_usd),0) AS billed_cost,
                   GROUP_CONCAT(DISTINCT confidence) AS confidences,
                   COUNT(*) AS events
            FROM usage_events
            WHERE {base_where}
            GROUP BY provider
            ORDER BY tokens DESC
            """,
            params,
        )
    )

    by_model = list(
        db.query(
            f"""
            SELECT provider, model,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                   COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost
            FROM usage_events
            WHERE {base_where}
            GROUP BY provider, model
            ORDER BY tokens DESC
            """,
            params,
        )
    )

    by_project = list(
        db.query(
            f"""
            SELECT provider, project_path,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost,
                   COUNT(DISTINCT session_id) AS sessions
            FROM usage_events
            WHERE {base_where} AND project_path IS NOT NULL
            GROUP BY provider, project_path
            ORDER BY tokens DESC
            LIMIT 20
            """,
            params,
        )
    )

    top_sessions = list(
        db.query(
            f"""
            SELECT provider, session_id, project_path,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost,
                   COUNT(*) AS events
            FROM usage_events
            WHERE {base_where} AND session_id IS NOT NULL
            GROUP BY provider, session_id
            ORDER BY tokens DESC
            LIMIT 10
            """,
            params,
        )
    )

    by_day = list(
        db.query(
            f"""
            SELECT local_date AS day, provider,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd),0) AS cost
            FROM usage_events
            WHERE {base_where}
            GROUP BY day, provider
            ORDER BY day, provider
            """,
            params,
        )
    )

    subscription_total = sum(s.monthly_cost_usd for s in cfg.subscriptions.values())
    api_eq = float(totals.get("estimated_cost_usd") or 0.0)
    multiple = (api_eq / subscription_total) if subscription_total else None

    return {
        "range": {"start": range_.start.isoformat(), "end": range_.end.isoformat()},
        "totals": dict(totals) if totals else {},
        "by_provider": [dict(r) for r in by_provider],
        "by_model": [dict(r) for r in by_model],
        "by_project": [dict(r) for r in by_project],
        "top_sessions": [dict(r) for r in top_sessions],
        "by_day": [dict(r) for r in by_day],
        "subscription_total_usd": subscription_total,
        "value_multiple": multiple,
    }


def render_monthly_report(
    db, range_: DateRange, cfg: AppConfig, console: Console, provider_filter: str | None = None
) -> None:
    summary = build_summary(db, range_, cfg, provider_filter=provider_filter)
    totals = summary["totals"]
    redact = cfg.privacy.redact_home_dir

    title = f"TokenBurn — {range_.start} to {range_.end}"
    if provider_filter:
        title += f" ({provider_filter})"

    summary_table = Table(title=title, show_lines=False)
    summary_table.add_column("Metric")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Events", f"{int(totals.get('event_count', 0)):,}")
    summary_table.add_row("Total tokens", f"{int(totals.get('total_tokens', 0)):,}")
    summary_table.add_row("Input", f"{int(totals.get('input_tokens', 0)):,}")
    summary_table.add_row("Output", f"{int(totals.get('output_tokens', 0)):,}")
    summary_table.add_row("Cache creation", f"{int(totals.get('cache_creation_tokens', 0)):,}")
    summary_table.add_row("Cache read", f"{int(totals.get('cache_read_tokens', 0)):,}")
    summary_table.add_row("Reasoning", f"{int(totals.get('reasoning_tokens', 0)):,}")
    summary_table.add_row("API-equivalent cost (USD)", f"${float(totals.get('estimated_cost_usd', 0.0)):,.2f}")
    if totals.get("billed_cost_usd"):
        summary_table.add_row("Imported billed cost (USD)", f"${float(totals['billed_cost_usd']):,.2f}")
    summary_table.add_row("Subscription cash paid (USD)", f"${summary['subscription_total_usd']:,.2f}")
    if summary["value_multiple"] is not None:
        summary_table.add_row("Subscription value multiple", f"{summary['value_multiple']:.2f}x")
    console.print(summary_table)

    prov_table = Table(title="By provider")
    prov_table.add_column("Provider")
    prov_table.add_column("Tokens", justify="right")
    prov_table.add_column("API-equiv $", justify="right")
    prov_table.add_column("Events", justify="right")
    prov_table.add_column("Confidences", overflow="fold")
    for r in summary["by_provider"]:
        prov_table.add_row(
            r["provider"],
            f"{int(r['tokens']):,}",
            f"${float(r['cost']):,.2f}",
            f"{int(r['events']):,}",
            r.get("confidences") or "",
        )
    console.print(prov_table)

    if summary["by_model"]:
        model_table = Table(title="By model")
        model_table.add_column("Provider")
        model_table.add_column("Model")
        model_table.add_column("Input", justify="right")
        model_table.add_column("Output", justify="right")
        model_table.add_column("Cache R", justify="right")
        model_table.add_column("Cache W", justify="right")
        model_table.add_column("Reason", justify="right")
        model_table.add_column("Tokens", justify="right")
        model_table.add_column("Cost $", justify="right")
        for r in summary["by_model"][:25]:
            model_table.add_row(
                r["provider"],
                r["model"] or "(unknown)",
                f"{int(r['input_tokens']):,}",
                f"{int(r['output_tokens']):,}",
                f"{int(r['cache_read_tokens']):,}",
                f"{int(r['cache_creation_tokens']):,}",
                f"{int(r['reasoning_tokens']):,}",
                f"{int(r['tokens']):,}",
                f"${float(r['cost']):,.2f}",
            )
        console.print(model_table)

    if summary["by_project"]:
        proj_table = Table(title="Top projects")
        proj_table.add_column("Provider")
        proj_table.add_column("Project")
        proj_table.add_column("Tokens", justify="right")
        proj_table.add_column("Cost $", justify="right")
        proj_table.add_column("Sessions", justify="right")
        for r in summary["by_project"]:
            proj = redact_home(r["project_path"]) if redact else r["project_path"]
            proj_table.add_row(
                r["provider"], proj, f"{int(r['tokens']):,}", f"${float(r['cost']):,.2f}", f"{int(r['sessions']):,}"
            )
        console.print(proj_table)

    if summary["top_sessions"]:
        sess_table = Table(title="Top 10 token-burning sessions")
        sess_table.add_column("Provider")
        sess_table.add_column("Project")
        sess_table.add_column("Session")
        sess_table.add_column("Tokens", justify="right")
        sess_table.add_column("Cost $", justify="right")
        for r in summary["top_sessions"]:
            proj = redact_home(r["project_path"]) if redact and r["project_path"] else (r["project_path"] or "")
            sid = (r["session_id"] or "")[:12]
            sess_table.add_row(
                r["provider"], proj, sid, f"{int(r['tokens']):,}", f"${float(r['cost']):,.2f}"
            )
        console.print(sess_table)
