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

app = typer.Typer(help="TokenBurn Ledger — local AI coding-agent token usage auditor.")
console = Console()


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
        console.print(f"[red]No config at {cfg_path}.[/red] Run `tokenburn init`.")
        raise typer.Exit(code=1)
    cfg = load_config(cfg_path)

    table = Table(title="tokenburn doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    table.add_row("tokenburn version", "[green]ok[/green]", f"{__version__} (parser v{PARSER_VERSION})")
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


@app.command()
def report(
    month: str | None = typer.Option(None, "--month", help="YYYY-MM"),
    frm: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    provider: str | None = typer.Option(None, "--provider", help="Limit to one provider"),
    config: Path | None = typer.Option(None, help="Config path"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip re-scanning sources; query DB only"),
) -> None:
    """Run adapters over the date range, ingest into the DB, render summary."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))
    pricing = PricingTable.load(default_pricing_path()) if default_pricing_path().exists() else None

    targets = [provider] if provider else [n for n, pcfg in cfg.providers.items() if pcfg.enabled]
    total_ingested = 0

    if not no_scan:
        for name in targets:
            pcfg = cfg.providers.get(name)
            if not pcfg or not pcfg.enabled:
                continue
            adapter = _adapter_for(name, cfg)
            sources = [s for s in adapter.discover() if s.exists]
            for src in sources:
                batch: list = []
                for ev in adapter.parse(src, rng):
                    if pricing is not None:
                        ev.estimated_cost_usd = estimate_cost(ev, pricing)
                    batch.append(ev)
                if batch:
                    from .db import upsert_events
                    upsert_events(db, batch)
                    total_ingested += len(batch)

    from .reports.monthly import render_monthly_report
    render_monthly_report(db, rng, cfg, console, provider_filter=provider)
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
    config: Path | None = typer.Option(None, help="Config path"),
) -> None:
    """Export the aggregated report (no re-scan)."""
    cfg = load_config(config or DEFAULT_CONFIG_PATH)
    rng = _resolve_range(month, frm, to, cfg.timezone)
    db = open_db(expand(cfg.db_path))

    from .reports.monthly import build_summary

    summary = build_summary(db, rng, cfg)
    if fmt == "markdown":
        from .reports.markdown import render_markdown
        text = render_markdown(summary, rng, cfg)
    elif fmt == "json":
        text = json.dumps(summary, indent=2, default=str)
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
