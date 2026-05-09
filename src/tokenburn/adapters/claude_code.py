from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..config import expand
from ..models import Confidence, DateRange, UsageEvent
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id, hash_path
from ..util.paths import decode_claude_project_dir
from .base import DiscoveredSource, ProviderAdapter


class ClaudeCodeAdapter(ProviderAdapter):
    id = "claude_code"
    display_name = "Claude Code"

    def discover(self) -> list[DiscoveredSource]:
        sources = []
        for raw in self.provider_config.paths or ["~/.claude/projects"]:
            p = expand(raw)
            sources.append(
                DiscoveredSource(
                    provider=self.id,
                    path=p,
                    kind="local_jsonl_dir",
                    exists=p.exists() and p.is_dir(),
                )
            )
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        if not source.exists:
            return
        tz = self.app_config.timezone
        privacy = self.app_config.privacy
        for jsonl_path in sorted(source.path.glob("**/*.jsonl")):
            project_path = self._project_from_filename(jsonl_path, source.path)
            yield from self._parse_file(jsonl_path, project_path, range_, tz, privacy)

    def _project_from_filename(self, jsonl_path: Path, root: Path) -> str:
        try:
            rel = jsonl_path.relative_to(root)
            top = rel.parts[0]
        except ValueError:
            top = jsonl_path.parent.name
        return decode_claude_project_dir(top)

    def _parse_file(
        self,
        path: Path,
        project_path_fallback: str,
        range_: DateRange,
        tz: str,
        privacy: Any,
    ) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                if rec.get("isApiErrorMessage"):
                    continue
                msg = rec.get("message") or {}
                model = msg.get("model")
                if not model or model == "<synthetic>":
                    continue
                usage = msg.get("usage") or {}
                ts_raw = rec.get("timestamp")
                if not ts_raw:
                    continue
                ts = parse_iso(ts_raw)
                if local_date(ts, tz) < range_.start or local_date(ts, tz) > range_.end:
                    continue
                msg_id = msg.get("id") or rec.get("uuid")
                if not msg_id:
                    continue

                cwd = rec.get("cwd") or project_path_fallback
                project_path = cwd
                project_hash = hash_path(project_path) if privacy.hash_project_paths else None

                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or 0)
                total = input_tokens + output_tokens + cache_creation + cache_read

                yield UsageEvent(
                    id=event_id("claude_code", msg_id),
                    provider=self.id,
                    tool="claude_code",
                    model=model,
                    session_id=rec.get("sessionId"),
                    conversation_id=rec.get("sessionId"),
                    project_path=project_path,
                    project_hash=project_hash,
                    repo_name=rec.get("gitBranch"),
                    timestamp_start=ts,
                    timezone=tz,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                    total_tokens=total,
                    source_type="local_jsonl",
                    source_path=str(path),
                    source_parser="claude_native_jsonl",
                    confidence=Confidence.EXACT_FROM_LOCAL_LOG,
                )


def reconcile_with_ccusage(db, range_: DateRange, console: Console) -> None:
    """Compare summed native totals against `ccusage daily --json`. Prints a table."""
    binary = shutil.which("ccusage") or (
        shutil.which("npx") and "npx ccusage"
    )
    if not binary:
        console.print("[yellow]ccusage not found on PATH — skipping reconciliation.[/yellow]")
        return

    cmd = (
        ["ccusage", "daily", "--json", "--since", range_.start.strftime("%Y%m%d"), "--until", range_.end.strftime("%Y%m%d")]
        if shutil.which("ccusage")
        else ["npx", "ccusage", "daily", "--json", "--since", range_.start.strftime("%Y%m%d"), "--until", range_.end.strftime("%Y%m%d")]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[red]Failed to run ccusage:[/red] {exc}")
        return
    if result.returncode != 0:
        console.print(f"[red]ccusage exited {result.returncode}[/red]\n{result.stderr}")
        return

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        console.print("[red]Could not parse ccusage JSON output.[/red]")
        return

    cc_total = 0
    for day in data.get("daily", []):
        cc_total += int(day.get("inputTokens", 0))
        cc_total += int(day.get("outputTokens", 0))
        cc_total += int(day.get("cacheCreationTokens", 0))
        cc_total += int(day.get("cacheReadTokens", 0))

    rows = list(
        db.query(
            """
            SELECT
              COALESCE(SUM(input_tokens),0)
              + COALESCE(SUM(output_tokens),0)
              + COALESCE(SUM(cache_creation_tokens),0)
              + COALESCE(SUM(cache_read_tokens),0) AS native_total
            FROM usage_events
            WHERE provider = 'claude_code'
              AND local_date BETWEEN :s AND :e
            """,
            {"s": range_.start.isoformat(), "e": range_.end.isoformat()},
        )
    )
    native = int(rows[0]["native_total"]) if rows else 0
    if cc_total == 0:
        delta_pct = 0.0
    else:
        delta_pct = abs(native - cc_total) / cc_total * 100

    table = Table(title="Claude Code reconciliation")
    table.add_column("Source")
    table.add_column("Total tokens", justify="right")
    table.add_row("native parser", f"{native:,}")
    table.add_row("ccusage", f"{cc_total:,}")
    table.add_row("delta", f"{delta_pct:.2f}%")
    console.print(table)
    if delta_pct > 5:
        console.print("[yellow]Warning: delta > 5% — investigate.[/yellow]")
