from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..config import expand
from ..models import Confidence, DateRange, UsageEvent
from ..privacy import project_identity
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id
from .base import DiscoveredSource, ProviderAdapter

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _usage_counts(raw: dict) -> dict[str, int]:
    return {field: int(raw.get(field, 0) or 0) for field in _USAGE_FIELDS}


class CodexAdapter(ProviderAdapter):
    id = "codex"
    display_name = "OpenAI Codex CLI"

    def discover(self) -> list[DiscoveredSource]:
        explicit = self.provider_config.paths or []
        codex_home = os.environ.get("CODEX_HOME")
        candidates = []
        if explicit:
            candidates.extend(expand(p) for p in explicit)
        elif codex_home:
            candidates.append(expand(codex_home) / "sessions")
        else:
            candidates.append(expand("~/.codex/sessions"))

        sources = []
        for p in candidates:
            sources.append(
                DiscoveredSource(
                    provider=self.id,
                    path=p,
                    kind="local_rollout_dir",
                    exists=p.exists() and p.is_dir(),
                )
            )
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        if not source.exists:
            return
        tz = self.app_config.timezone
        privacy = self.app_config.privacy

        for jsonl_path in sorted(source.path.glob("**/rollout-*.jsonl")):
            yield from self._parse_file(jsonl_path, range_, tz, privacy)

    def _parse_file(self, path: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        current_model: str | None = None
        current_cwd: str | None = None
        prev_usage = {field: 0 for field in _USAGE_FIELDS}
        line_no = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line_no += 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                payload = rec.get("payload") or {}

                if rtype == "turn_context":
                    current_model = payload.get("model") or current_model
                    current_cwd = payload.get("cwd") or current_cwd
                    continue

                if rtype != "event_msg":
                    continue
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                last = info.get("last_token_usage")
                cumulative = info.get("total_token_usage") or {}
                cumulative_usage = _usage_counts(cumulative)

                if last:
                    delta = last
                    confidence = Confidence.EXACT_FROM_LOCAL_LOG
                else:
                    baseline = prev_usage
                    if cumulative_usage["total_tokens"] < prev_usage["total_tokens"]:
                        baseline = {field: 0 for field in _USAGE_FIELDS}
                    delta = {
                        field: max(cumulative_usage[field] - baseline[field], 0)
                        for field in _USAGE_FIELDS
                    }
                    confidence = Confidence.ESTIMATED_FROM_SESSION_SUMMARY

                if cumulative:
                    prev_usage = cumulative_usage
                elif last:
                    last_usage = _usage_counts(last)
                    prev_usage = {
                        field: prev_usage[field] + last_usage[field]
                        for field in _USAGE_FIELDS
                    }

                ts_raw = rec.get("timestamp")
                if not ts_raw:
                    continue
                ts = parse_iso(ts_raw)
                if local_date(ts, tz) < range_.start or local_date(ts, tz) > range_.end:
                    continue

                input_tokens = int(delta.get("input_tokens", 0))
                cached = int(delta.get("cached_input_tokens", 0))
                output_tokens = int(delta.get("output_tokens", 0))
                reasoning = int(delta.get("reasoning_output_tokens", 0))
                total = int(delta.get("total_tokens", input_tokens + output_tokens))

                project, project_hash = project_identity(current_cwd, privacy)

                yield UsageEvent(
                    id=event_id("codex", str(path), str(line_no), ts_raw),
                    provider=self.id,
                    tool="codex_cli",
                    model=current_model,
                    session_id=path.stem,
                    project_path=project,
                    project_hash=project_hash,
                    timestamp_start=ts,
                    timezone=tz,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cached,
                    reasoning_tokens=reasoning,
                    total_tokens=total,
                    source_type="local_jsonl",
                    source_path=str(path),
                    source_parser="codex_native_jsonl",
                    confidence=confidence,
                )
