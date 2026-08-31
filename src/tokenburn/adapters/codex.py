from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..models import Confidence, DateRange, UsageEvent
from ..privacy import project_identity
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id
from ..util.paths import resolve_log_dirs
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


def _record_model(payload: dict) -> str | None:
    """Any model id this record advertises, wherever Codex happened to put it."""
    if not isinstance(payload, dict):
        return None
    candidates = (
        payload.get("model"),
        (payload.get("thread_settings") or {}).get("model"),
        (payload.get("state") or {}).get("model"),
        ((payload.get("collaboration_mode") or {}).get("settings") or {}).get("model"),
        ((payload.get("thread_settings") or {}).get("collaboration_mode") or {})
        .get("settings", {})
        .get("model"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _prescan_context(path: Path) -> tuple[str | None, str | None]:
    """Find the thread's model and cwd before attributing any usage.

    `turn_context` is written when a turn *starts*, but a thread forked into a
    subagent resumes its parent's context and reports usage long before that —
    in real logs the first turn_context lands on line 384 of 501 while
    token_count events begin on line 5. Streaming state forward therefore
    leaves everything up to that point with no model and no project, which
    grouped 42.6M tokens as "(unknown)" and priced them at $0.

    A rollout file is one thread on one model, so resolving both up front from
    whichever record happens to carry them (session_meta holds cwd; the model
    may only appear in a mid-file event_msg's thread_settings) attributes the
    whole file. The main loop still applies later updates, so a genuine
    mid-file change is not masked.
    """
    model: str | None = None
    cwd: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = rec.get("payload") or {}
                if model is None:
                    model = _record_model(payload)
                if cwd is None and isinstance(payload, dict):
                    value = payload.get("cwd")
                    if isinstance(value, str) and value.strip():
                        cwd = value.strip()
                if model and cwd:
                    break
    except OSError:
        return None, None
    return model, cwd


class CodexAdapter(ProviderAdapter):
    id = "codex"
    display_name = "OpenAI Codex CLI"

    def discover(self) -> list[DiscoveredSource]:
        # Honour an explicit config; otherwise probe CODEX_HOME, then the
        # default and XDG-style locations.
        candidates = resolve_log_dirs(
            self.provider_config.paths,
            env_subdirs=[("CODEX_HOME", "sessions")],
            fallbacks=["~/.codex/sessions", "~/.config/codex/sessions"],
        )
        existing = [p for p in candidates if p.exists() and p.is_dir()]
        chosen = existing or candidates[:1]
        return [
            DiscoveredSource(
                provider=self.id,
                path=p,
                kind="local_rollout_dir",
                exists=p.exists() and p.is_dir(),
            )
            for p in chosen
        ]

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        if not source.exists:
            return
        tz = self.app_config.timezone
        privacy = self.app_config.privacy

        for jsonl_path in sorted(source.path.glob("**/rollout-*.jsonl")):
            yield from self._parse_file(jsonl_path, range_, tz, privacy)

    def _parse_file(self, path: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        # Resolve the thread's model/cwd up front; usage can be reported long
        # before the record that declares them. See _prescan_context.
        current_model, current_cwd = _prescan_context(path)
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
