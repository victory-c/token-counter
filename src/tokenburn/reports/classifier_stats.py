"""Classifier health / re-clustering signal dashboard.

`tokencounter classifier-stats` shows:
  - Coverage per provider: how many sessions got classified (eligible vs. not)
  - Confidence distribution: where the classifier was unsure
  - Override pairs: where users overruled the heuristic, by (heuristic → override) pair
  - Unclassified composition: where the still-unclassified spend is going

The point of the command is to answer "is it time to re-shape the taxonomy or
add a new bucket?" without having to write ad-hoc SQL. Cursor and Gemini stay
ineligible — they don't carry conversation content, so they can't be
classified by the heuristic.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from rich.console import Console
from rich.table import Table

from ..models import DateRange
from .by_task import CLASSIFIED_PROVIDERS

# 0-1 confidence is bucketed into these ranges, ordered high-to-low so the
# rendered histogram reads naturally (best confidence on top). Each tuple is
# (label, lower_bound_inclusive). The label is what the user sees in the
# histogram AND what the SQL CASE expression returns — keeping them tied to
# one source prevents silent zero-bucket drift if someone tweaks the ranges.
_CONF_BUCKETS: tuple[tuple[str, float | None], ...] = (
    ("0.80–1.00", 0.80),
    ("0.60–0.79", 0.60),
    ("0.40–0.59", 0.40),
    ("< 0.40", None),  # the ELSE bucket
)
_CONF_LABELS = tuple(label for label, _ in _CONF_BUCKETS)
_BAR_WIDTH = 20


def _confidence_case_sql() -> str:
    """Build the SQL CASE expression for the confidence histogram from the
    canonical bucket definitions, so the SQL labels and the Python tuple
    can't drift."""
    when_clauses = "\n                ".join(
        f"WHEN confidence >= {bound:.2f} THEN '{label}'"
        for label, bound in _CONF_BUCKETS
        if bound is not None
    )
    else_label = next(label for label, bound in _CONF_BUCKETS if bound is None)
    return (
        "CASE\n                "
        f"{when_clauses}\n                "
        f"ELSE '{else_label}'\n            "
        "END"
    )


def build_classifier_stats(db, range_: DateRange | None) -> dict[str, Any]:
    """Pull all four classifier-health datasets in one shot.

    `range_` is optional: passing None scans all time, which is what you
    usually want for override-pattern detection (more data = more signal).
    """
    has_range = range_ is not None
    params: dict[str, Any] = {}
    where_events = ""
    # Histograms and override counts always require a matching usage_events row
    # so orphan classifications (left over from JSONL walks that didn't end up
    # in events) don't inflate the totals. With a range, the EXISTS clause
    # also filters to that range.
    range_clause = "AND e.local_date BETWEEN :s AND :e" if has_range else ""
    exists_classification = (
        f"WHERE EXISTS (SELECT 1 FROM usage_events e "
        f"WHERE e.session_id = c.session_id AND e.provider = c.provider "
        f"{range_clause})"
    )
    override_exists_clause = (
        f"AND EXISTS (SELECT 1 FROM usage_events e "
        f"WHERE e.session_id = c.session_id AND e.provider = c.provider "
        f"{range_clause})"
    )
    if has_range:
        params["s"] = range_.start.isoformat()
        params["e"] = range_.end.isoformat()
        where_events = "WHERE e.local_date BETWEEN :s AND :e"

    # --- 1. Coverage per provider ---
    coverage_rows = list(
        db.query(
            f"""
            SELECT
                e.provider AS provider,
                COUNT(DISTINCT e.session_id) AS total_sessions,
                COUNT(DISTINCT c.session_id) AS classified_sessions
            FROM usage_events e
            LEFT JOIN session_classifications c
                ON c.session_id = e.session_id AND c.provider = e.provider
            {where_events}
            GROUP BY e.provider
            ORDER BY e.provider
            """,
            params,
        )
    )
    coverage = []
    for r in coverage_rows:
        total = int(r["total_sessions"] or 0)
        classified = int(r["classified_sessions"] or 0)
        eligible = r["provider"] in CLASSIFIED_PROVIDERS
        coverage.append(
            {
                "provider": r["provider"],
                "total": total,
                "classified": classified,
                "eligible": eligible,
                "pct": (classified / total) if (eligible and total) else None,
            }
        )

    # --- 2. Confidence histogram ---
    hist_rows = list(
        db.query(
            f"""
            SELECT
                {_confidence_case_sql()} AS bucket,
                COUNT(*) AS n
            FROM session_classifications c
            {exists_classification}
            GROUP BY bucket
            """,
            params,
        )
    )
    hist_map = {r["bucket"]: int(r["n"]) for r in hist_rows}
    confidence = OrderedDict((b, hist_map.get(b, 0)) for b in _CONF_LABELS)
    total_classified = sum(confidence.values())

    # --- 3. Override pairs (only disagreements count) ---
    pair_rows = list(
        db.query(
            f"""
            SELECT
                c.task_category AS heuristic,
                o.task_category AS override_cat,
                COUNT(*) AS n
            FROM session_classifications c
            JOIN session_overrides o
                ON c.session_id = o.session_id AND c.provider = o.provider
            WHERE c.task_category != o.task_category
            {override_exists_clause}
            GROUP BY c.task_category, o.task_category
            ORDER BY n DESC
            LIMIT 10
            """,
            params,
        )
    )
    override_pairs = [
        {"heuristic": r["heuristic"], "override": r["override_cat"], "n": int(r["n"])}
        for r in pair_rows
    ]

    # Total overrides (including same-category, just for the "X out of Y" line).
    # Always require a matching usage_event so we stay consistent with the
    # rest of the dashboard.
    total_overrides = next(
        iter(
            db.query(
                f"""
                SELECT COUNT(*) AS n FROM session_overrides o
                WHERE EXISTS (
                    SELECT 1 FROM usage_events e
                    WHERE e.session_id = o.session_id
                      AND e.provider = o.provider
                      {range_clause}
                )
                """,
                params,
            )
        ),
        {"n": 0},
    )["n"]

    # --- 4. Unclassified composition (sessions with no classification row) ---
    unclass_rows = list(
        db.query(
            f"""
            SELECT
                e.provider AS provider,
                COUNT(DISTINCT e.session_id) AS sessions,
                COALESCE(SUM(e.total_tokens), 0) AS tokens,
                COALESCE(SUM(e.estimated_cost_usd), 0) AS cost
            FROM usage_events e
            LEFT JOIN session_classifications c
                ON c.session_id = e.session_id AND c.provider = e.provider
            {where_events}
            {"AND" if where_events else "WHERE"} c.session_id IS NULL
            GROUP BY e.provider
            ORDER BY cost DESC
            """,
            params,
        )
    )
    unclassified_eligible = []
    unclassified_ineligible = []
    for r in unclass_rows:
        item = {
            "provider": r["provider"],
            "sessions": int(r["sessions"] or 0),
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0.0),
        }
        if r["provider"] in CLASSIFIED_PROVIDERS:
            unclassified_eligible.append(item)
        else:
            unclassified_ineligible.append(item)

    return {
        "range": (range_.start.isoformat(), range_.end.isoformat()) if has_range else None,
        "coverage": coverage,
        "confidence": dict(confidence),
        "confidence_total": total_classified,
        "override_pairs": override_pairs,
        "override_total": int(total_overrides or 0),
        "unclassified_eligible": unclassified_eligible,
        "unclassified_ineligible": unclassified_ineligible,
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _bar(share: float, width: int = _BAR_WIDTH) -> str:
    """Render a horizontal Unicode bar for a share in [0, 1]."""
    if share <= 0:
        return ""
    full = int(share * width)
    remainder = (share * width) - full
    tail = ""
    if remainder >= 0.75:
        tail = "▊"
    elif remainder >= 0.5:
        tail = "▌"
    elif remainder >= 0.25:
        tail = "▎"
    return "█" * full + tail


def render_classifier_stats(summary: dict[str, Any], console: Console) -> None:
    title_suffix = (
        f"{summary['range'][0]} → {summary['range'][1]}" if summary["range"] else "all time"
    )
    console.print(f"[bold]Classifier — {title_suffix}[/bold]")

    # Coverage
    cov = Table(title="Coverage", show_lines=False)
    cov.add_column("Provider")
    cov.add_column("Classified", justify="right")
    cov.add_column("Total", justify="right")
    cov.add_column("Pct", justify="right")
    cov.add_column("Note")
    if not summary["coverage"]:
        cov.add_row("—", "—", "—", "—", "no usage_events yet")
    for r in summary["coverage"]:
        if r["eligible"]:
            pct = f"{r['pct']*100:.0f}%" if r["pct"] is not None else "—"
            note = ""
        else:
            pct = "—"
            note = "[dim]provider not eligible[/dim]"
        cov.add_row(r["provider"], f"{r['classified']:,}", f"{r['total']:,}", pct, note)
    console.print(cov)

    # Confidence histogram
    total = summary["confidence_total"]
    hist = Table(title="Confidence distribution (classified sessions only)", show_lines=False)
    hist.add_column("Bucket")
    hist.add_column("Sessions", justify="right")
    hist.add_column("Bar")
    hist.add_column("Share", justify="right")
    if total == 0:
        hist.add_row("—", "0", "", "—")
    else:
        for bucket, n in summary["confidence"].items():
            share = (n / total) if total else 0.0
            hist.add_row(bucket, f"{n:,}", _bar(share), f"{share*100:.0f}%")
    console.print(hist)
    if total > 0:
        console.print(
            "[dim]Low-confidence sessions are top candidates for "
            "`tokencounter task-detail --session …` inspection.[/dim]"
        )

    # Overrides
    ot = summary["override_total"]
    classified = total  # total in confidence buckets == total classified rows in range
    if ot == 0:
        console.print("\n[bold]Overrides[/bold]: none recorded in this range.")
    else:
        pct = (ot / classified * 100) if classified else 0.0
        console.print(
            f"\n[bold]Overrides[/bold]: {ot:,} of {classified:,} classified sessions "
            f"({pct:.0f}%)."
        )
        if summary["override_pairs"]:
            pairs = Table(title="Top (heuristic → override) pairs", show_lines=False)
            pairs.add_column("Heuristic said")
            pairs.add_column("User said")
            pairs.add_column("Count", justify="right")
            pairs.add_column("Share", justify="right")
            for p in summary["override_pairs"]:
                share = (p["n"] / classified) if classified else 0.0
                pairs.add_row(
                    p["heuristic"],
                    p["override"],
                    f"{p['n']:,}",
                    f"{share*100:.1f}%",
                )
            console.print(pairs)
            console.print(
                "[dim]A pair at ≥5% of classified sessions suggests the taxonomy "
                "is missing a label — candidate for the next re-clustering pass.[/dim]"
            )

    # Unclassified composition
    elig = summary["unclassified_eligible"]
    inelig = summary["unclassified_ineligible"]
    if not elig and not inelig:
        console.print("\n[green]No unclassified sessions — nothing to follow up on.[/green]")
        return

    unc = Table(title="Unclassified composition", show_lines=False)
    unc.add_column("Provider")
    unc.add_column("Sessions", justify="right")
    unc.add_column("Tokens", justify="right")
    unc.add_column("Cost $", justify="right")
    unc.add_column("Note")
    for r in elig:
        unc.add_row(
            r["provider"],
            f"{r['sessions']:,}",
            f"{r['tokens']:,}",
            f"${r['cost']:,.2f}",
            "[yellow]eligible — should be classified[/yellow]",
        )
    for r in inelig:
        unc.add_row(
            r["provider"],
            f"{r['sessions']:,}",
            f"{r['tokens']:,}",
            f"${r['cost']:,.2f}",
            "[dim]ineligible provider (no convo content)[/dim]",
        )
    console.print(unc)
    if not elig:
        console.print(
            "[dim]No eligible-provider sessions left unclassified — "
            "run `tokencounter classify` if that's not what you expected.[/dim]"
        )
