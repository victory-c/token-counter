from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import PARSER_VERSION, __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    expand,
    load_config,
    write_default_config,
)
from .db import open_db
from .models import DateRange
from .pricing import PricingTable, default_pricing_path, estimate_cost
from .util.dates import month_range

app = typer.Typer(help="TokenCounter — local AI coding-agent token usage auditor.")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"tokencounter {__version__} (parser v{PARSER_VERSION})")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Entrypoint — only here to mount the global --version flag."""


def _adapter_for(name: str, app_cfg: AppConfig):
    from .adapters.claude_code import ClaudeCodeAdapter
    from .adapters.codex import CodexAdapter
    from .adapters.cursor import CursorAdapter
    from .adapters.gemini import GeminiAdapter

    registry = {
        "claude_code": ClaudeCodeAdapter,
        "codex": CodexAdapter,
        "cursor": CursorAdapter,
        "gemini": GeminiAdapter,
    }
    cls = registry.get(name)
    if cls is None:
        raise typer.BadParameter(f"Unknown provider: {name}")
    pcfg = app_cfg.providers.get(name)
    if pcfg is None:
        raise typer.BadParameter(f"Provider {name!r} not in config")
    return cls(app_cfg, pcfg)


def _all_enabled_adapters(app_cfg: AppConfig):
    return [
        _adapter_for(name, app_cfg)
        for name, pcfg in app_cfg.providers.items()
        if pcfg.enabled
    ]


@app.command()
def init(
    path: Path | None = typer.Option(None, help="Where to write the config (default ~/.tokenburn/config.yaml)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config"),
) -> None:
    """Create a default config file."""
    target = path or DEFAULT_CONFIG_PATH
    if target.exists() and not force:
        console.print(f"[yellow]Config already exists at {target}[/yellow] — pass --force to overwrite.")
        raise typer.Exit(code=1)
    written = write_default_config(target)
    # Also create the imports dirs so users have a clear drop zone.
    cfg = load_config(written)
    for pcfg in cfg.providers.values():
        if pcfg.import_dir:
            expand(pcfg.import_dir).mkdir(parents=True, exist_ok=True)
    console.print(f"[green]Wrote config to {written}[/green]")


@app.command()
def doctor(
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Validate config, log paths, pricing, optional binaries."""
    cfg_path = config or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        console.print(f"[red]No config at {cfg_path}.[/red] Run `tokencounter init`.")
        raise typer.Exit(code=1)
    cfg = load_config(cfg_path)

    table = Table(title="tokencounter doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    table.add_row("tokencounter version", "[green]ok[/green]", f"{__version__} (parser v{PARSER_VERSION})")
    table.add_row("config", "[green]ok[/green]", str(cfg_path))
    table.add_row("timezone", "[green]ok[/green]", cfg.timezone)

    db_path = expand(cfg.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_db(db_path).close()
    table.add_row("database", "[green]ok[/green]", str(db_path))

    pricing_path = default_pricing_path()
    if pricing_path.exists():
        try:
            pt = PricingTable.load(pricing_path)
            row_count = sum(len(v) for v in pt._by_key.values())  # type: ignore[attr-defined]
            table.add_row("pricing", "[green]ok[/green]", f"{row_count} rows from {pricing_path}")
        except Exception as exc:  # noqa: BLE001
            table.add_row("pricing", "[red]fail[/red]", str(exc))
    else:
        table.add_row("pricing", "[yellow]missing[/yellow]", str(pricing_path))

    for name, pcfg in cfg.providers.items():
        if not pcfg.enabled:
            table.add_row(f"provider:{name}", "[dim]disabled[/dim]", "")
            continue
        adapter = _adapter_for(name, cfg)
        sources = adapter.discover()
        present = [s for s in sources if s.exists]
        if present:
            detail = ", ".join(str(s.path) for s in present)
            table.add_row(f"provider:{name}", "[green]found[/green]", detail)
        else:
            detail = ", ".join(str(s.path) for s in sources) or "(no paths)"
            table.add_row(f"provider:{name}", "[yellow]missing[/yellow]", detail)

    ccusage = shutil.which("ccusage")
    table.add_row("ccusage binary", "[green]found[/green]" if ccusage else "[dim]not found[/dim]", ccusage or "")
    npx = shutil.which("npx")
    table.add_row("npx binary", "[green]found[/green]" if npx else "[dim]not found[/dim]", npx or "")

    # Classifier coverage: how many distinct (provider, session_id) sessions
    # in usage_events have a classification row?
    db = open_db(db_path)
    coverage = next(iter(db.query("""
        SELECT
            COUNT(DISTINCT e.provider || ':' || e.session_id) AS sessions_total,
            COUNT(DISTINCT CASE WHEN c.task_category IS NOT NULL
                                THEN e.provider || ':' || e.session_id END) AS sessions_classified
        FROM usage_events e
        LEFT JOIN session_classifications c
            ON c.session_id = e.session_id AND c.provider = e.provider
        WHERE e.session_id IS NOT NULL
          AND e.provider IN ('claude_code', 'codex')
    """)), {})
    total = int(coverage.get("sessions_total") or 0)
    classified = int(coverage.get("sessions_classified") or 0)
    if total > 0:
        pct = int(classified / total * 100)
        status = "[green]ok[/green]" if classified == total else "[yellow]partial[/yellow]"
        table.add_row(
            "classifier coverage",
            status,
            f"{classified}/{total} sessions classified ({pct}%) — Claude+Codex"
            + (" — run `tokencounter classify`" if classified < total else ""),
        )
    else:
        table.add_row("classifier coverage", "[dim]n/a[/dim]", "no Claude/Codex events ingested yet")

    console.print(table)


@app.command()
def scan(
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Discover available log sources for each enabled provider."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    table = Table(title="Discovered sources")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Kind")
    for name, pcfg in cfg.providers.items():
        if not pcfg.enabled:
            table.add_row(name, "[dim]disabled[/dim]", "", "")
            continue
        adapter = _adapter_for(name, cfg)
        for src in adapter.discover():
            status = "[green]found[/green]" if src.exists else "[yellow]missing[/yellow]"
            table.add_row(name, status, str(src.path), src.kind)
    console.print(table)


@app.command()
def providers(
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """List configured providers."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    table = Table(title="Configured providers")
    table.add_column("Provider")
    table.add_column("Enabled")
    table.add_column("Source")
    table.add_column("Paths / import_dir")
    for name, pcfg in cfg.providers.items():
        paths = ", ".join(pcfg.paths) if pcfg.paths else (pcfg.import_dir or "")
        table.add_row(name, "yes" if pcfg.enabled else "no", pcfg.source, paths)
    console.print(table)


def _resolve_range(month: str | None, frm: str | None, to: str | None, tz: str) -> DateRange:
    if month:
        return month_range(month, tz)
    if frm and to:
        from datetime import date as _date
        return DateRange(start=_date.fromisoformat(frm), end=_date.fromisoformat(to))
    raise typer.BadParameter("Provide either --month YYYY-MM or both --from and --to")


def _range_label(month: str | None, rng: DateRange) -> str:
    """Filename-safe label for a range: the month if given, else start_to_end."""
    if month:
        return month
    return f"{rng.start.isoformat()}_to_{rng.end.isoformat()}"


def _resolve_output_dir(cfg: AppConfig, override: Path | None) -> tuple[Path, str | None]:
    """Resolve the export directory, creating it if needed.

    Returns (dir, warning). Falls back to the current working directory with a
    warning when the configured/overridden directory can't be created.
    """
    target = expand(override) if override else expand(cfg.exports.default_output_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target, None
    except OSError:
        cwd = Path.cwd()
        return cwd, (
            f"Could not write to {target}. Generated files in current directory instead: {cwd}"
        )


def _open_in_browser(path: Path) -> bool:
    import webbrowser

    try:
        return webbrowser.open(path.resolve().as_uri())
    except Exception:  # noqa: BLE001
        return False


def _ingest(cfg: AppConfig, db, rng: DateRange, provider: str | None) -> int:
    """Scan each enabled provider's discovered sources into the DB.

    Shared by `report`, `dashboard`, and `export-month` so they all ingest
    fresh data the same way. Returns the number of events ingested.
    """
    from .db import upsert_events

    pricing = PricingTable.load(default_pricing_path()) if default_pricing_path().exists() else None
    targets = [provider] if provider else [n for n, pcfg in cfg.providers.items() if pcfg.enabled]
    total = 0
    for name in targets:
        pcfg = cfg.providers.get(name)
        if not pcfg or not pcfg.enabled:
            continue
        adapter = _adapter_for(name, cfg)
        for src in (s for s in adapter.discover() if s.exists):
            batch: list = []
            for ev in adapter.parse(src, rng):
                if pricing is not None:
                    ev.estimated_cost_usd = estimate_cost(ev, pricing)
                batch.append(ev)
            if batch:
                upsert_events(db, batch)
                total += len(batch)
    return total


@app.command()
def report(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    provider: str | None = typer.Option(None, "--provider", help="Limit to one provider"),
    by_task: bool = typer.Option(False, "--by-task", help="Add a per-task breakdown (requires `tokencounter classify` to have run)"),
    config: Path | None = typer.Option(None, help="Config path"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip re-scanning sources; query DB only"),
) -> None:
    """Run adapters over the date range, ingest into the DB, render summary."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))

    total_ingested = 0 if no_scan else _ingest(cfg, db, rng, provider)

    from .reports.monthly import render_monthly_report
    render_monthly_report(db, rng, cfg, console, provider_filter=provider)

    if by_task:
        from .reports.by_task import build_task_summary, render_task_table
        task_summary = build_task_summary(db, rng, provider_filter=provider)
        if task_summary["by_task"]:
            render_task_table(task_summary, console)
            classified = sum(1 for r in task_summary["by_task"] if r["task_category"] != "unclassified")
            if classified == 0:
                console.print("[yellow]No classified sessions in this range. Run `tokencounter classify` first.[/yellow]")
        else:
            console.print("[yellow]No data in this range.[/yellow]")

    if total_ingested:
        console.print(f"[dim]Ingested {total_ingested} events.[/dim]")


@app.command(name="import")
def import_cmd(
    provider: str = typer.Argument(..., help="cursor | gemini"),
    path: Path = typer.Argument(..., help="Path to import file"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Manually import a Cursor CSV or Gemini JSONL file."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    if provider not in {"cursor", "gemini"}:
        raise typer.BadParameter("provider must be 'cursor' or 'gemini'")
    if not path.exists():
        raise typer.BadParameter(f"File not found: {path}")
    adapter = _adapter_for(provider, cfg)
    db = open_db(expand(cfg.db_path))
    pricing = PricingTable.load(default_pricing_path()) if default_pricing_path().exists() else None

    from .adapters.base import DiscoveredSource
    src = DiscoveredSource(provider=provider, path=path, kind="manual_import", exists=True)
    rng = DateRange(start=__import__("datetime").date(1970, 1, 1), end=__import__("datetime").date(9999, 12, 31))
    batch = []
    for ev in adapter.parse(src, rng):
        if pricing is not None:
            ev.estimated_cost_usd = estimate_cost(ev, pricing)
        batch.append(ev)
    if batch:
        from .db import upsert_events
        upsert_events(db, batch)
    console.print(f"[green]Imported {len(batch)} events from {path}[/green]")


@app.command()
def export(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    fmt: str = typer.Option("markdown", "--format", help="markdown | json | csv"),
    output: Path | None = typer.Option(None, "--output", help="Write to file (default stdout)"),
    by_task: bool = typer.Option(False, "--by-task", help="Include task-classification sections"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Export the aggregated report (no re-scan)."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))

    from .reports.monthly import build_summary

    summary = build_summary(db, rng, cfg)

    task_summary = None
    savings_summary = None
    if by_task:
        from .reports.by_task import build_savings, build_task_summary
        task_summary = build_task_summary(db, rng)
        savings_summary = build_savings(db, rng)

    if fmt == "markdown":
        from .reports.markdown import render_markdown
        text = render_markdown(summary, rng, cfg, task_summary=task_summary, savings_summary=savings_summary)
    elif fmt == "json":
        payload = dict(summary)
        if task_summary is not None:
            payload["by_task"] = task_summary["by_task"]
        if savings_summary is not None:
            payload["savings"] = savings_summary
        text = json.dumps(payload, indent=2, default=str)
    elif fmt == "csv":
        from .reports.markdown import render_csv
        text = render_csv(summary)
    else:
        raise typer.BadParameter("format must be markdown | json | csv")

    if output:
        output.write_text(text)
        console.print(f"[green]Wrote {output}[/green]")
    else:
        typer.echo(text)


def _build_dashboard_html(db, rng: DateRange, cfg: AppConfig, provider_filter: str | None) -> str:
    """Build the standalone dashboard HTML string from the DB (no re-scan)."""
    from .reports.dashboard import build_dashboard_payload, render_dashboard_html

    pricing = PricingTable.load(default_pricing_path()) if default_pricing_path().exists() else None
    payload = build_dashboard_payload(
        db, rng, cfg, pricing=pricing, provider_filter=provider_filter
    )
    return render_dashboard_html(payload)


def _warn_if_empty(cfg: AppConfig, db, rng: DateRange, provider: str | None) -> None:
    """When a range has no events, tell the user where we looked.

    This is the difference between a baffling blank dashboard and an actionable
    "your logs aren't where I expected" message.
    """
    params: dict = {"s": rng.start.isoformat(), "e": rng.end.isoformat()}
    where = "local_date BETWEEN :s AND :e"
    if provider:
        where += " AND provider = :p"
        params["p"] = provider
    row = next(iter(db.query(f"SELECT COUNT(*) AS n FROM usage_events WHERE {where}", params)), {"n": 0})
    if int(row.get("n") or 0) > 0:
        return

    console.print(f"[yellow]No usage events found for {rng.start} → {rng.end}.[/yellow]")
    console.print("[dim]Sources checked:[/dim]")
    names = [provider] if provider else [n for n, p in cfg.providers.items() if p.enabled]
    for name in names:
        pcfg = cfg.providers.get(name)
        if not pcfg or not pcfg.enabled:
            continue
        for s in _adapter_for(name, cfg).discover():
            mark = "[green]found[/green]" if s.exists else "[red]missing[/red]"
            console.print(f"  [dim]{name}:[/dim] {s.path} ({mark})")
    console.print(
        "[dim]If a path is missing, set it under providers.<name>.paths in your config, "
        "or `tokencounter import cursor|gemini <file>` for manual exports.[/dim]"
    )


@app.command()
def dashboard(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    provider: str | None = typer.Option(None, "--provider", help="Limit to one provider"),
    output: Path | None = typer.Option(None, "--output", help="Write to this file (overrides default ~/Downloads path)"),
    open_: bool = typer.Option(False, "--open", help="Open the dashboard in the default browser"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip scanning sources; use the existing DB only"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Generate a standalone interactive HTML dashboard.

    Scans your configured log sources into the DB first (use --no-scan to skip
    and render from existing data only).
    """
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))

    if not no_scan:
        _ingest(cfg, db, rng, provider)

    html = _build_dashboard_html(db, rng, cfg, provider)

    if output:
        out_path = expand(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir, warning = _resolve_output_dir(cfg, None)
        if warning:
            console.print(f"[yellow]Warning: {warning}[/yellow]")
        label = _range_label(month, rng)
        out_path = out_dir / cfg.exports.dashboard_filename_pattern.format(label=label)

    out_path.write_text(html)
    console.print(f"[green]Generated interactive HTML dashboard:[/green] {out_path}")
    _warn_if_empty(cfg, db, rng, provider)

    if open_ or cfg.dashboard.auto_open:
        if _open_in_browser(out_path):
            console.print("[dim]Opened dashboard in default browser.[/dim]")
        else:
            console.print("[yellow]Could not open a browser automatically.[/yellow]")


@app.command(name="export-month")
def export_month(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    provider: str | None = typer.Option(None, "--provider", help="Limit to one provider"),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Directory for both files (default ~/Downloads)"),
    by_task: bool = typer.Option(False, "--by-task", help="Include task-classification sections in the Markdown report"),
    open_dashboard: bool = typer.Option(False, "--open-dashboard", help="Open the dashboard after generating"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip scanning sources; use the existing DB only"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Generate BOTH the Markdown report and the HTML dashboard.

    Scans your configured log sources first (use --no-scan to skip). The two
    outputs are parallel and never overwrite each other. If one fails, the
    other is still written and the failure is reported.
    """
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))
    label = _range_label(month, rng)

    if not no_scan:
        _ingest(cfg, db, rng, provider)

    out_dir, warning = _resolve_output_dir(cfg, output_dir)
    if warning:
        console.print(f"[yellow]Warning: {warning}[/yellow]")

    md_path = out_dir / cfg.exports.markdown_filename_pattern.format(label=label)
    html_path = out_dir / cfg.exports.dashboard_filename_pattern.format(label=label)

    md_ok = False
    try:
        from .reports.markdown import render_markdown
        from .reports.monthly import build_summary

        summary = build_summary(db, rng, cfg, provider_filter=provider)
        task_summary = savings_summary = None
        if by_task:
            from .reports.by_task import build_savings, build_task_summary
            task_summary = build_task_summary(db, rng, provider_filter=provider)
            savings_summary = build_savings(db, rng)
        text = render_markdown(summary, rng, cfg, task_summary=task_summary, savings_summary=savings_summary)
        md_path.write_text(text)
        md_ok = True
        console.print(f"[green]Generated Markdown report:[/green] {md_path}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to generate Markdown report:[/red] {exc}")

    html_ok = False
    try:
        html = _build_dashboard_html(db, rng, cfg, provider)
        html_path.write_text(html)
        html_ok = True
        console.print(f"[green]Generated interactive HTML dashboard:[/green] {html_path}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to generate dashboard:[/red] {exc}")

    if html_ok and md_ok:
        pass
    elif md_ok:
        console.print("[dim]The Markdown report was saved successfully.[/dim]")
    elif html_ok:
        console.print("[dim]The dashboard was saved successfully.[/dim]")
    else:
        raise typer.Exit(code=1)

    _warn_if_empty(cfg, db, rng, provider)

    if open_dashboard and html_ok:
        if _open_in_browser(html_path):
            console.print("[dim]Opened dashboard in default browser.[/dim]")
        else:
            console.print("[yellow]Could not open a browser automatically.[/yellow]")


@app.command()
def classify(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM (informational only — classifier scans all sessions)"),
    provider: str | None = typer.Option(None, "--provider", help="Limit to one provider (claude_code | codex)"),
    reclassify: bool = typer.Option(False, "--reclassify", help="Overwrite existing classifications"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Classify sessions into task categories using the heuristic classifier."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    db = open_db(expand(cfg.db_path))

    if provider and provider not in {"claude_code", "codex"}:
        raise typer.BadParameter("provider must be 'claude_code' or 'codex' (others lack signal)")
    targets = [provider] if provider else ["claude_code", "codex"]

    from .classifier.engine import classify_range
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

    last = {"provider": None, "task": None}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
        transient=True,
    ) as progress:
        def cb(prov: str, cur: int, total: int) -> None:
            if last["provider"] != prov:
                last["task"] = progress.add_task(f"Classifying {prov}", total=total)
                last["provider"] = prov
            progress.update(last["task"], completed=cur)

        report = classify_range(db, cfg, providers=targets, reclassify=reclassify, progress_cb=cb)

    summary = Table(title="Classification summary")
    summary.add_column("Provider")
    summary.add_column("Sessions classified", justify="right")
    for prov, n in report.provider_counts.items():
        summary.add_row(prov, str(n))
    if report.skipped_existing:
        summary.add_row("[dim]skipped (already classified)[/dim]", str(report.skipped_existing))
    console.print(summary)

    cat_table = Table(title="By task category")
    cat_table.add_column("Category")
    cat_table.add_column("Sessions", justify="right")
    for cat, n in report.category_counts.items():
        if n:
            cat_table.add_row(cat.value, str(n))
    console.print(cat_table)
    if report.skipped_existing and not reclassify:
        console.print("[dim]Pass --reclassify to overwrite existing classifications.[/dim]")


@app.command()
def override(
    session: str = typer.Option(..., "--session", help="Session ID"),
    provider: str = typer.Option(..., "--provider", help="claude_code | codex"),
    category: str | None = typer.Option(None, "--category", help="Task category to set"),
    note: str | None = typer.Option(None, "--note", help="Optional note explaining the override"),
    clear: bool = typer.Option(False, "--clear", help="Remove a prior override"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Manually override the classification for a session."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    db = open_db(expand(cfg.db_path))

    if clear:
        db.execute(
            "DELETE FROM session_overrides WHERE session_id = ? AND provider = ?",
            [session, provider],
        )
        db.conn.commit()
        console.print(f"[green]Cleared override for {provider}/{session}[/green]")
        return

    if not category:
        raise typer.BadParameter("--category is required (or pass --clear)")

    from .classifier.taxonomy import TaskCategory
    valid = {c.value for c in TaskCategory}
    if category not in valid:
        raise typer.BadParameter(f"category must be one of: {sorted(valid)}")

    from datetime import datetime, timezone
    db["session_overrides"].insert(
        {
            "session_id": session,
            "provider": provider,
            "task_category": category,
            "note": note or "",
            "set_at": datetime.now(timezone.utc).isoformat(),
        },
        pk=("session_id", "provider"),
        replace=True,
    )
    db.conn.commit()
    console.print(f"[green]Set {provider}/{session} → {category}[/green]")


@app.command(name="task-detail")
def task_detail(
    session: str = typer.Option(..., "--session", help="Session ID"),
    provider: str | None = typer.Option(None, "--provider", help="claude_code | codex (auto-detected if omitted)"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Show the classifier's verdict and reasoning for one session."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    db = open_db(expand(cfg.db_path))

    rows = list(
        db.query(
            "SELECT * FROM session_classifications WHERE session_id = :s "
            + ("AND provider = :p" if provider else ""),
            {"s": session, "p": provider} if provider else {"s": session},
        )
    )
    if not rows:
        console.print(f"[yellow]No classification found for session {session!r}.[/yellow]")
        console.print("Run `tokencounter classify` first, or check the session ID.")
        raise typer.Exit(code=1)

    overrides = list(
        db.query(
            "SELECT * FROM session_overrides WHERE session_id = :s "
            + ("AND provider = :p" if provider else ""),
            {"s": session, "p": provider} if provider else {"s": session},
        )
    )
    override_by_provider = {o["provider"]: o for o in overrides}

    from .classifier.fitness import load_default as load_fit
    from .classifier.taxonomy import DESCRIPTIONS, TaskCategory
    fit = load_fit()

    for r in rows:
        prov = r["provider"]
        ovr = override_by_provider.get(prov)
        effective = ovr["task_category"] if ovr else r["task_category"]
        try:
            cat_enum = TaskCategory(effective)
            description = DESCRIPTIONS.get(cat_enum, "")
        except ValueError:
            description = ""

        table = Table(title=f"{prov} / {session}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Effective category", effective)
        table.add_row("Description", description)
        table.add_row("Heuristic verdict", r["task_category"])
        table.add_row("Confidence", f"{float(r['confidence'] or 0):.2f}")
        table.add_row("Classifier", f"{r['classifier']} ({r['classifier_version']})")
        table.add_row("Min model class", fit.minimum_class_for(effective))
        if ovr:
            table.add_row("Override note", ovr.get("note") or "(none)")
        console.print(table)

        # Show signals (parsed from features_json)
        import json as _json
        try:
            features = _json.loads(r["features_json"] or "{}")
        except _json.JSONDecodeError:
            features = {}
        if features:
            sig_table = Table(title="Signals")
            sig_table.add_column("Field")
            sig_table.add_column("Value")
            for k, v in sorted(features.items()):
                if v in (0, "", None, [], {}):
                    continue
                sig_table.add_row(k, str(v))
            console.print(sig_table)


@app.command()
def savings(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Show right-sizing opportunities: tasks where a cheaper model would suffice."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))

    from .reports.by_task import build_savings, render_savings
    savings_summary = build_savings(db, rng)
    render_savings(savings_summary, console)


@app.command(name="classifier-stats")
def classifier_stats(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Classifier health dashboard: coverage, confidence, override patterns, unclassified $$$.

    Defaults to all-time when no range is provided — override-pattern signal
    benefits from accumulating data across months.
    """
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone) if (month or frm or to) else None
    db = open_db(expand(cfg.db_path))

    from .reports.classifier_stats import build_classifier_stats, render_classifier_stats
    summary = build_classifier_stats(db, rng)
    render_classifier_stats(summary, console)


@app.command()
def reconcile(
    month: str = typer.Option(..., "--month", help="YYYY-MM"),
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Compare native Claude parser totals against ccusage (when available)."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    pcfg = cfg.providers.get("claude_code")
    if pcfg is None:
        raise typer.BadParameter("claude_code provider not configured")
    from .adapters.claude_code import reconcile_with_ccusage

    rng = month_range(month, cfg.timezone)
    db = open_db(expand(cfg.db_path))
    reconcile_with_ccusage(db, rng, console)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
