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


# gen_metadata holds protobuf map<string, string> entries alongside the raw
# conversation transcript. Matching `\n <klen> key \x12 <vlen> value` and then
# checking both declared lengths keeps us on real entries instead of whatever
# the transcript happens to contain.
_METADATA_ENTRY = re.compile(rb"\x0a([\x01-\x7f])([ -~]+?)\x12([\x01-\x7f])([ -~]+)")

# Length-prefixed vendor model ids, e.g. b"\x18claude-opus-4-6-thinking".
_MODEL_ID = re.compile(rb"([\x08-\x40])((?:claude|gemini|gpt)[a-z0-9._-]{3,60})")

_MODEL_KEYS = ("model_enum",)


def _metadata_entries(blob: bytes) -> dict[str, str]:
    """Decode the string→string metadata map stored in gen_metadata."""
    out: dict[str, str] = {}
    for match in _METADATA_ENTRY.finditer(blob):
        key_len, key_raw, value_len, value_raw = (
            match.group(1)[0],
            match.group(2),
            match.group(3)[0],
            match.group(4),
        )
        if len(key_raw) < key_len or len(value_raw) < value_len:
            continue
        key = key_raw[:key_len].decode("utf-8", "ignore")
        value = value_raw[:value_len].decode("utf-8", "ignore")
        if len(key) != key_len or len(value) != value_len:
            continue
        if re.fullmatch(r"[a-z0-9_]{3,40}", key):
            out.setdefault(key, value)
    return out


def _model_ids(blob: bytes) -> list[str]:
    """Every length-prefixed vendor model id in the blob, most common first."""
    counts: dict[str, int] = {}
    for match in _MODEL_ID.finditer(blob):
        declared, raw = match.group(1)[0], match.group(2)
        if len(raw) < declared:
            continue
        name = raw[:declared].decode("utf-8", "ignore")
        if len(name) != declared or not re.fullmatch(r"[a-z0-9._-]+", name):
            continue
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n))


def _model_from_blob(blob: bytes) -> str | None:
    """Identify the model behind an Antigravity conversation.

    Antigravity records a concrete vendor model id (`claude-opus-4-6-thinking`)
    for requests it routes to another vendor, but only an internal routing
    label (`gemini-pro-default`) plus a placeholder enum for its own Gemini
    models. Concrete ids carry a version number and routing labels do not, so
    prefer an id containing a digit and fall back to the label.

    Never scrape the surrounding transcript: gen_metadata stores conversation
    text next to the metadata, so a loose match reports fragments of the user's
    own source as model names.
    """
    ids = _model_ids(blob)
    for name in ids:
        if any(ch.isdigit() for ch in name):
            return name
    if ids:
        return ids[0]
    enum_name = _metadata_entries(blob).get("model_enum", "")
    if enum_name:
        return "antigravity-" + enum_name.removeprefix("MODEL_").lower().replace("_", "-")
    return None


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
