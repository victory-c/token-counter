"""Per-task aggregation and savings calculation.

Joins usage_events with session_classifications (and session_overrides, which
take precedence). Produces:
  - by_task: sessions/tokens/cost grouped by task_category, plus dominant model
  - savings: for each (task, model) pair where the model class overshoots
    the task's minimum class, recompute the cost as if the representative
    model of the minimum class had been used; report the delta as savings
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from rich.console import Console
from rich.table import Table

from ..classifier.fitness import FitnessTable, load_default as load_default_fitness
from ..models import DateRange
from ..pricing import PricingTable, default_pricing_path

# Providers that participate in classification. Cursor + Gemini events
# stay grouped as `unclassified`.
CLASSIFIED_PROVIDERS = ("claude_code", "codex")


def build_task_summary(db, range_: DateRange) -> dict[str, Any]:
    params = {"s": range_.start.isoformat(), "e": range_.end.isoformat()}

    # Per (category, provider, model): sum tokens, cost, sessions.
    # Use COALESCE(override, classification, 'unclassified') so manual
    # overrides win over the heuristic.
    rows = list(
        db.query(
            """
            SELECT
                COALESCE(o.task_category, c.task_category, 'unclassified') AS task_category,
                e.provider AS provider,
                e.model AS model,
                COALESCE(SUM(e.input_tokens),0) AS input_tokens,
                COALESCE(SUM(e.output_tokens),0) AS output_tokens,
                COALESCE(SUM(e.cache_creation_tokens),0) AS cache_creation_tokens,
                COALESCE(SUM(e.cache_read_tokens),0) AS cache_read_tokens,
                COALESCE(SUM(e.reasoning_tokens),0) AS reasoning_tokens,
                COALESCE(SUM(e.total_tokens),0) AS tokens,
                COALESCE(SUM(e.estimated_cost_usd),0) AS cost,
                COUNT(DISTINCT e.session_id) AS sessions
            FROM usage_events e
            LEFT JOIN session_classifications c
                ON c.session_id = e.session_id AND c.provider = e.provider
            LEFT JOIN session_overrides o
                ON o.session_id = e.session_id AND o.provider = e.provider
            WHERE e.local_date BETWEEN :s AND :e
            GROUP BY
                COALESCE(o.task_category, c.task_category, 'unclassified'),
                e.provider, e.model
            ORDER BY
                COALESCE(o.task_category, c.task_category, 'unclassified'),
                tokens DESC
            """,
            params,
        )
    )

    # Roll up to category-level with dominant model per category.
    by_task: dict[str, dict] = defaultdict(
        lambda: {
            "task_category": None,
            "tokens": 0,
            "cost": 0.0,
            "sessions": set(),
            "models": defaultdict(lambda: {"tokens": 0, "provider": None, "cost": 0.0}),
        }
    )
    for r in rows:
        bucket = by_task[r["task_category"]]
        bucket["task_category"] = r["task_category"]
        bucket["tokens"] += int(r["tokens"])
        bucket["cost"] += float(r["cost"])
        bucket["sessions"].add((r["provider"], r["model"]))  # not perfect, see note
        m = bucket["models"][r["model"] or "(unknown)"]
        m["tokens"] += int(r["tokens"])
        m["cost"] += float(r["cost"])
        m["provider"] = r["provider"]

    out_rows: list[dict] = []
    for cat, b in by_task.items():
        # Pick the model with the most tokens as "dominant"
        dominant_model, dominant_info = max(
            b["models"].items(), key=lambda kv: kv[1]["tokens"], default=(None, {"tokens": 0})
        ) if b["models"] else (None, {"tokens": 0})
        share = (dominant_info["tokens"] / b["tokens"]) if b["tokens"] else 0.0
        out_rows.append({
            "task_category": cat,
            "tokens": b["tokens"],
            "cost": b["cost"],
            "dominant_model": dominant_model,
            "dominant_share": share,
            "dominant_provider": dominant_info.get("provider"),
        })
    out_rows.sort(key=lambda r: r["tokens"], reverse=True)
    return {"by_task": out_rows, "raw_rows": [dict(r) for r in rows]}


def build_savings(
    db,
    range_: DateRange,
    fitness: FitnessTable | None = None,
    pricing: PricingTable | None = None,
) -> dict[str, Any]:
    fitness = fitness or load_default_fitness()
    if pricing is None and default_pricing_path().exists():
        pricing = PricingTable.load(default_pricing_path())

    summary = build_task_summary(db, range_)
    raw_rows = summary["raw_rows"]

    opportunities: list[dict] = []
    for r in raw_rows:
        cat = r["task_category"]
        if cat == "unclassified":
            continue
        actual_model = r["model"]
        if not actual_model:
            continue
        if not fitness.is_overshooting(actual_model, cat):
            continue
        target_class = fitness.minimum_class_for(cat)
        rep_model = fitness.representative_for(target_class)
        actual_cost = float(r["cost"])
        if pricing is None or rep_model is None or actual_cost <= 0:
            continue

        # Re-price the same token mix at the representative model.
        # Look up under the representative's natural provider (claude_code
        # for "claude-...", etc.) — falls back to event's provider.
        rep_provider = _provider_for_rep_model(rep_model, r["provider"])
        # Fake an event for pricing lookup
        from datetime import datetime
        from ..models import Confidence, UsageEvent
        from ..pricing import estimate_cost
        sample_dt = datetime.fromisoformat(range_.start.isoformat() + "T12:00:00")
        ev = UsageEvent(
            id="x",
            provider=rep_provider,
            tool=rep_provider,
            timestamp_start=sample_dt,
            timezone="UTC",
            source_type="virtual",
            source_parser="virtual",
            confidence=Confidence.MANUAL_IMPORT,
            model=rep_model,
            input_tokens=int(r["input_tokens"]),
            output_tokens=int(r["output_tokens"]),
            cache_creation_tokens=int(r["cache_creation_tokens"]),
            cache_read_tokens=int(r["cache_read_tokens"]),
            reasoning_tokens=int(r["reasoning_tokens"]),
        )
        rep_cost = estimate_cost(ev, pricing) or 0.0
        if rep_cost >= actual_cost:
            continue  # rep wasn't actually cheaper; skip
        opportunities.append({
            "task_category": cat,
            "actual_model": actual_model,
            "actual_cost": round(actual_cost, 2),
            "recommended_class": target_class,
            "recommended_model": rep_model,
            "recommended_cost": round(rep_cost, 2),
            "savings_usd": round(actual_cost - rep_cost, 2),
            "savings_pct": round((actual_cost - rep_cost) / actual_cost, 3),
            "rationale": fitness.rationale_for(cat),
            "tokens": int(r["tokens"]),
        })
    # Aggregate per category for the headline numbers.
    per_category: dict[str, dict] = defaultdict(lambda: {"actual": 0.0, "rec": 0.0, "savings": 0.0, "tokens": 0})
    for o in opportunities:
        b = per_category[o["task_category"]]
        b["actual"] += o["actual_cost"]
        b["rec"] += o["recommended_cost"]
        b["savings"] += o["savings_usd"]
        b["tokens"] += o["tokens"]
    category_rows = sorted(
        [
            {
                "task_category": cat,
                "actual_cost": round(v["actual"], 2),
                "recommended_cost": round(v["rec"], 2),
                "savings_usd": round(v["savings"], 2),
                "savings_pct": round(v["savings"] / v["actual"], 3) if v["actual"] else 0.0,
                "tokens": v["tokens"],
                "minimum_class": fitness.minimum_class_for(cat),
            }
            for cat, v in per_category.items()
        ],
        key=lambda r: r["savings_usd"],
        reverse=True,
    )
    total_savings = round(sum(o["savings_usd"] for o in opportunities), 2)
    total_actual = round(sum(o["actual_cost"] for o in opportunities), 2)
    return {
        "category_rows": category_rows,
        "model_rows": opportunities,
        "total_savings_usd": total_savings,
        "total_actual_usd": total_actual,
        "savings_pct": round(total_savings / total_actual, 3) if total_actual else 0.0,
    }


def _provider_for_rep_model(model: str, fallback: str) -> str:
    m = model.lower()
    if m.startswith("claude"):
        return "claude_code"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3"):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return fallback or "claude_code"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_task_table(summary: dict[str, Any], console: Console, fitness: FitnessTable | None = None) -> None:
    fitness = fitness or load_default_fitness()
    table = Table(title="By task")
    table.add_column("Task")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost $", justify="right")
    table.add_column("Dominant model")
    table.add_column("Min class")
    for r in summary["by_task"]:
        cat = r["task_category"]
        share = f"({r['dominant_share']*100:.0f}%)" if r["dominant_share"] else ""
        warn = ""
        if cat != "unclassified":
            if fitness.is_overshooting(r["dominant_model"], cat):
                warn = "  [yellow]over-served[/yellow]"
        table.add_row(
            cat,
            f"{int(r['tokens']):,}",
            f"${float(r['cost']):,.2f}",
            f"{r['dominant_model'] or '—'} {share}{warn}",
            fitness.minimum_class_for(cat) if cat != "unclassified" else "—",
        )
    console.print(table)


def render_savings(summary: dict[str, Any], console: Console) -> None:
    if not summary["category_rows"]:
        console.print("[green]No right-sizing opportunities found in this range. Nice.[/green]")
        return
    table = Table(title="Right-sizing opportunities")
    table.add_column("Task")
    table.add_column("Tokens", justify="right")
    table.add_column("Spent $", justify="right")
    table.add_column("Could spend $", justify="right")
    table.add_column("Save $", justify="right")
    table.add_column("(%)", justify="right")
    table.add_column("Min class")
    for r in summary["category_rows"]:
        table.add_row(
            r["task_category"],
            f"{int(r['tokens']):,}",
            f"${r['actual_cost']:,.2f}",
            f"${r['recommended_cost']:,.2f}",
            f"${r['savings_usd']:,.2f}",
            f"-{int(r['savings_pct']*100)}%",
            r["minimum_class"],
        )
    console.print(table)
    pct = int(summary["savings_pct"] * 100) if summary["savings_pct"] else 0
    console.print(
        f"[bold]Total potential monthly savings: ${summary['total_savings_usd']:,.2f}[/bold]"
        f"  (~{pct}% of the right-sizable spend)"
    )
    console.print(
        "[dim]These are recommendations against retail API list prices. "
        "Always A/B before switching production workloads.[/dim]"
    )
