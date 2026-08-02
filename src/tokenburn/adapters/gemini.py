from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..config import expand
from ..models import Confidence, DateRange, UsageEvent
from ..privacy import project_identity
from ..util.dates import local_date, parse_iso
from ..util.hashing import event_id
from ..util.paths import resolve_log_dirs
from .base import DiscoveredSource, ProviderAdapter


# Antigravity stores protobuf messages in SQLite. These small wire-format helpers
# intentionally decode only the fields needed from ModelUsageStats, rather than
# depending on Antigravity's private generated protobuf package.
def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint is too large")
    raise ValueError("truncated protobuf varint")


def _protobuf_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 0x07
        if number == 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
            fields.append((number, wire_type, value))
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated fixed64 protobuf field")
            fields.append((number, wire_type, data[offset : offset + 8]))
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf bytes field")
            fields.append((number, wire_type, data[offset:end]))
            offset = end
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated fixed32 protobuf field")
            fields.append((number, wire_type, data[offset : offset + 4]))
            offset += 4
        else:
            # Groups are not used by the records we parse. Stop at an unknown
            # wire type rather than risking an infinite loop on corrupt data.
            break
    return fields


def _extract_model_usage_stats(payload: bytes) -> dict[str, int | str] | None:
    """Find a nested Antigravity ModelUsageStats protobuf message.

    ModelUsageStats has stable fields in the stored response:
    model=1, input=2, output=3, cache_write=4, cache_read=5.
    We recurse through length-delimited fields because step_payload wraps the
    response in several private message types.
    """
    try:
        fields = _protobuf_fields(payload)
    except ValueError:
        return None

    ints = {number: value for number, wire, value in fields if wire == 0 and isinstance(value, int)}
    # ModelUsageStats.model is an enum (field 1), not a string. Requiring the
    # enum wire type plus at least one cache field avoids matching ordinary
    # trajectory messages that happen to use fields 2 and 3.
    if (
        1 in ints
        and {2, 3}.issubset(ints)
        and ({4, 5} & ints.keys())
        and 0 <= ints[1] < 10_000
        and 0 < ints[2] < 10**10
        and 0 <= ints[3] < 10**10
    ):
        cache_write = ints.get(4, 0)
        cache_read = ints.get(5, 0)
        return {
            "model_enum": ints[1],
            "input_tokens": ints[2],
            "output_tokens": ints[3],
            "cache_creation_tokens": cache_write,
            "cache_read_tokens": cache_read,
            "total_tokens": ints[2] + ints[3] + cache_write + cache_read,
        }

    for _number, wire, value in fields:
        if wire == 2 and isinstance(value, bytes):
            result = _extract_model_usage_stats(value)
            if result is not None:
                return result
    return None


def _timestamp_from_step_metadata(metadata: bytes) -> datetime | None:
    """Read the protobuf Timestamp embedded as field 1 of step metadata."""
    try:
        outer = _protobuf_fields(metadata)
        timestamp = next(value for number, wire, value in outer if number == 1 and wire == 2)
        if not isinstance(timestamp, bytes):
            return None
        fields = _protobuf_fields(timestamp)
        seconds = next(value for number, wire, value in fields if number == 1 and wire == 0)
        nanos = next((value for number, wire, value in fields if number == 2 and wire == 0), 0)
        if not isinstance(seconds, int) or not isinstance(nanos, int):
            return None
        return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=UTC)
    except (StopIteration, TypeError, ValueError, OSError):
        return None


def _model_from_blob(blob: bytes) -> str | None:
    text = blob.decode("utf-8", "ignore")
    match = re.search(r"gemini[-_][a-zA-Z0-9._-]+", text, re.IGNORECASE)
    return match.group(0) if match else None


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
        candidates = resolve_log_dirs(
            self.provider_config.paths,
            env_subdirs=[("GEMINI_HOME", "tmp")],
            fallbacks=["~/.gemini/tmp", "~/.gemini/antigravity-cli"],
        )
        for ep in candidates:
            if ep == p or not (ep.exists() and ep.is_dir()):
                continue
            kind = "antigravity_sqlite_dir" if ep.name == "antigravity-cli" else "local_jsonl_dir"
            sources.append(DiscoveredSource(provider=self.id, path=ep, kind=kind, exists=True))
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        tz = self.app_config.timezone
        privacy = self.app_config.privacy
        path = source.path
        if path.is_dir() and path.name == "antigravity-cli":
            yield from self._parse_antigravity(path, range_, tz, privacy)
            return
        files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        for f in files:
            yield from self._parse_jsonl(f, range_, tz, privacy)

    def _parse_antigravity(self, root: Path, range_: DateRange, tz: str, privacy) -> Iterator[UsageEvent]:
        conversation_dir = root / "conversations"
        for path in sorted(conversation_dir.glob("*.db")):
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                rows = connection.execute(
                    "SELECT idx, metadata, step_payload FROM steps ORDER BY idx"
                )
                trajectory_id = connection.execute(
                    "SELECT trajectory_id FROM trajectory_meta LIMIT 1"
                ).fetchone()
                session_id = trajectory_id[0] if trajectory_id else path.stem
                model_blob = b"".join(
                    row[0]
                    for row in connection.execute("SELECT data FROM gen_metadata ORDER BY idx")
                    if isinstance(row[0], bytes)
                )
                model = _model_from_blob(model_blob) or "gemini-antigravity"
                for idx, metadata, payload in rows:
                    ts = _timestamp_from_step_metadata(metadata or b"")
                    usage = _extract_model_usage_stats(payload or b"")
                    if ts is None or usage is None or not local_date(ts, tz):
                        continue
                    if local_date(ts, tz) < range_.start or local_date(ts, tz) > range_.end:
                        continue
                    project, project_hash = project_identity(None, privacy)
                    yield UsageEvent(
                        id=event_id("gemini-antigravity", str(path), str(idx)),
                        provider=self.id,
                        tool="antigravity",
                        model=model,
                        session_id=session_id,
                        project_path=project,
                        project_hash=project_hash,
                        timestamp_start=ts,
                        timezone=tz,
                        input_tokens=int(usage["input_tokens"]),
                        output_tokens=int(usage["output_tokens"]),
                        cache_creation_tokens=int(usage["cache_creation_tokens"]),
                        cache_read_tokens=int(usage["cache_read_tokens"]),
                        total_tokens=int(usage["total_tokens"]),
                        source_type="local_sqlite",
                        source_path=str(path),
                        source_parser="antigravity_sqlite_protobuf",
                        confidence=Confidence.EXACT_FROM_PROVIDER_LOG,
                    )
            except (sqlite3.Error, OSError):
                continue
            finally:
                if connection is not None:
                    connection.close()

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
                project, project_hash = project_identity(project, privacy)

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
