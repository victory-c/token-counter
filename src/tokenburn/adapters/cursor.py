from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

from ..config import expand
from ..models import Confidence, DateRange, UsageEvent
from ..privacy import project_identity
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id
from .base import DiscoveredSource, ProviderAdapter

REQUIRED_COLUMNS = {"timestamp", "model"}
RECOGNIZED_COLUMNS = {
    "timestamp",
    "model",
    "request_type",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "source",
    "session_id",
    "project_path",
}

# Cursor's native dashboard export uses different column names. When we see
# "Date" + "Model" we translate columns into our internal schema before parsing.
CURSOR_NATIVE_HEADERS = {"Date", "Model"}


def _from_cursor_native(row: dict) -> dict:
    """Translate one row from Cursor's dashboard CSV export into our schema."""
    cost_raw = (row.get("Cost") or "").strip()
    if cost_raw in {"Included", "-", "", "Errored, No Charge"}:
        cost_usd = None
    else:
        try:
            cost_usd = float(cost_raw.replace("$", ""))
        except ValueError:
            cost_usd = None

    return {
        "timestamp": row.get("Date") or "",
        "model": row.get("Model") or "",
        "request_type": row.get("Kind") or "",
        "input_tokens": row.get("Input (w/o Cache Write)") or "",
        "output_tokens": row.get("Output Tokens") or "",
        "cache_read_tokens": row.get("Cache Read") or "",
        "cache_write_tokens": row.get("Input (w/ Cache Write)") or "",
        "cost_usd": "" if cost_usd is None else str(cost_usd),
        "session_id": row.get("Cloud Agent ID") or row.get("Automation ID") or "",
        "project_path": "",
        "source": "cursor_dashboard_export",
    }


class CursorAdapter(ProviderAdapter):
    id = "cursor"
    display_name = "Cursor"

    def discover(self) -> list[DiscoveredSource]:
        sources = []
        import_dir = self.provider_config.import_dir or "~/.tokenburn/imports/cursor"
        p = expand(import_dir)
        sources.append(
            DiscoveredSource(
                provider=self.id,
                path=p,
                kind="manual_import_dir",
                exists=p.exists() and p.is_dir(),
            )
        )
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        tz = self.app_config.timezone
        privacy = self.app_config.privacy

        path = source.path
        if path.is_dir():
            files = sorted(list(path.glob("*.csv")) + list(path.glob("*.json")))
        else:
            files = [path]

        for f in files:
            if f.suffix.lower() == ".csv":
                yield from self._parse_csv(f, range_, tz, privacy)
            elif f.suffix.lower() == ".json":
                yield from self._parse_json(f, range_, tz, privacy)

    def _row_to_event(
        self,
        row: dict,
        path: Path,
        line_no: int,
        range_: DateRange,
        tz: str,
        privacy,
    ) -> UsageEvent | None:
        ts_raw = row.get("timestamp")
        if not ts_raw:
            return None
        try:
            ts = parse_iso(str(ts_raw))
        except (ValueError, TypeError):
            return None
        if local_date(ts, tz) < range_.start or local_date(ts, tz) > range_.end:
            return None

        raw_model = (row.get("model") or "").strip()
        if raw_model.lower() in {"auto", "cursor-auto", ""}:
            model = "cursor-auto"
            model_alias = None
        else:
            model = raw_model
            model_alias = raw_model

        input_tokens = _to_int(row.get("input_tokens"))
        output_tokens = _to_int(row.get("output_tokens"))
        cache_read = _to_int(row.get("cache_read_tokens"))
        cache_write = _to_int(row.get("cache_write_tokens"))
        cost = _to_float(row.get("cost_usd"))

        if input_tokens is None and output_tokens is None and cost is not None:
            confidence = Confidence.MANUAL_IMPORT
            total = None
        else:
            confidence = Confidence.MANUAL_IMPORT
            total = (input_tokens or 0) + (output_tokens or 0) + (cache_read or 0) + (cache_write or 0)

        project = row.get("project_path") or None
        project, project_hash = project_identity(project, privacy)

        return UsageEvent(
            id=event_id("cursor", str(path), str(line_no), str(ts_raw), raw_model),
            provider=self.id,
            tool="cursor",
            model=model,
            model_alias=model_alias,
            session_id=row.get("session_id"),
            project_path=project,
            project_hash=project_hash,
            timestamp_start=ts,
            timezone=tz,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_write,
            cache_read_tokens=cache_read,
            total_tokens=total,
            billed_cost_usd=cost,
            source_type="manual_import",
            source_path=str(path),
            source_parser="cursor_csv_import",
            confidence=confidence,
        )

    def _parse_csv(self, path: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return
            fields = set(reader.fieldnames)
            is_native_export = CURSOR_NATIVE_HEADERS.issubset(fields)
            if not is_native_export:
                missing = REQUIRED_COLUMNS - fields
                if missing:
                    raise ValueError(
                        f"{path}: missing required column(s) {sorted(missing)}; got {reader.fieldnames}"
                    )
            for line_no, row in enumerate(reader, start=2):
                if is_native_export:
                    row = _from_cursor_native(row)
                ev = self._row_to_event(row, path, line_no, range_, tz, privacy)
                if ev is not None:
                    yield ev

    def _parse_json(self, path: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("rows") or data.get("events") or []
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected JSON array or {{rows:[]}} object")
        for line_no, row in enumerate(data, start=1):
            ev = self._row_to_event(row, path, line_no, range_, tz, privacy)
            if ev is not None:
                yield ev


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
