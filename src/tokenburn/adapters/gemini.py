from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..config import expand
from ..models import Confidence, DateRange, UsageEvent
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id, hash_path
from .base import DiscoveredSource, ProviderAdapter


class GeminiAdapter(ProviderAdapter):
    id = "gemini"
    display_name = "Gemini"

    def discover(self) -> list[DiscoveredSource]:
        sources = []
        import_dir = self.provider_config.import_dir or "~/.tokenburn/imports/gemini"
        p = expand(import_dir)
        sources.append(
            DiscoveredSource(
                provider=self.id,
                path=p,
                kind="manual_import_dir",
                exists=p.exists() and p.is_dir(),
            )
        )
        for raw in self.provider_config.paths or []:
            ep = expand(raw)
            sources.append(
                DiscoveredSource(
                    provider=self.id,
                    path=ep,
                    kind="local_jsonl_dir",
                    exists=ep.exists(),
                )
            )
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        tz = self.app_config.timezone
        privacy = self.app_config.privacy
        path = source.path
        if path.is_dir():
            files = sorted(path.glob("*.jsonl"))
        else:
            files = [path]
        for f in files:
            yield from self._parse_jsonl(f, range_, tz, privacy)

    def _parse_jsonl(self, path: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_raw = rec.get("timestamp") or rec.get("time")
                if not ts_raw:
                    continue
                try:
                    ts = parse_iso(str(ts_raw))
                except (ValueError, TypeError):
                    continue
                if local_date(ts, tz) < range_.start or local_date(ts, tz) > range_.end:
                    continue

                model = rec.get("model")
                meta = rec.get("usageMetadata") or rec.get("usage_metadata") or {}
                input_tokens = int(meta.get("promptTokenCount") or 0)
                output_tokens = int(meta.get("candidatesTokenCount") or 0)
                cache_read = int(meta.get("cachedContentTokenCount") or 0)
                total_meta = meta.get("totalTokenCount")
                total = int(total_meta) if total_meta is not None else (input_tokens + output_tokens + cache_read)

                project = rec.get("project_path") or rec.get("cwd")
                project_hash = hash_path(project) if (privacy.hash_project_paths and project) else None

                yield UsageEvent(
                    id=event_id("gemini", str(path), str(line_no)),
                    provider=self.id,
                    tool=rec.get("tool") or "gemini",
                    model=model,
                    session_id=rec.get("session_id"),
                    project_path=project,
                    project_hash=project_hash,
                    timestamp_start=ts,
                    timezone=tz,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    total_tokens=total,
                    source_type="local_jsonl",
                    source_path=str(path),
                    source_parser="gemini_jsonl_import",
                    confidence=Confidence.EXACT_FROM_PROVIDER_LOG,
                )
