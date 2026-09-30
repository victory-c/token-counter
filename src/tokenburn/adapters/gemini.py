from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

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
    model=1, input=2, output=3, cache_write=4, cache_read=5, thinking=9
    (thinking is already included in output).
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
            "reasoning_tokens": ints.get(9, 0),
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


def _gen_metadata_records(blob: bytes) -> Iterator[list[tuple[int, int, int | bytes]]]:
    """Yield the per-generation metadata messages nested in field 1."""
    try:
        outer = _protobuf_fields(blob)
    except ValueError:
        return
    for number, wire, value in outer:
        if number != 1 or wire != 2 or not isinstance(value, bytes):
            continue
        try:
            yield _protobuf_fields(value)
        except ValueError:
            continue


def _metadata_entries(blob: bytes) -> dict[str, str]:
    """Decode the string→string metadata map stored in gen_metadata."""
    out: dict[str, str] = {}
    for fields in _gen_metadata_records(blob):
        for number, wire, value in fields:
            if number != 20 or wire != 2 or not isinstance(value, bytes):
                continue
            try:
                entry = _protobuf_fields(value)
            except ValueError:
                continue
            if len(entry) != 2 or [
                (entry_number, entry_wire) for entry_number, entry_wire, _ in entry
            ] != [(1, 2), (2, 2)]:
                continue
            key_raw, value_raw = entry[0][2], entry[1][2]
            if not isinstance(key_raw, bytes) or not isinstance(value_raw, bytes):
                continue
            try:
                key = key_raw.decode("utf-8")
                metadata_value = value_raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if re.fullmatch(r"[a-z0-9_]{3,40}", key):
                out.setdefault(key, metadata_value)
    return out


def _model_ids(blob: bytes) -> list[str]:
    """Every selected model id (field 19) in generation metadata, most common first."""
    counts: dict[str, int] = {}
    for fields in _gen_metadata_records(blob):
        for number, wire, raw in fields:
            if number != 19 or wire != 2 or not isinstance(raw, bytes):
                continue
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                continue
            if re.fullmatch(r"(?:claude|gemini|gpt)[a-z0-9._-]{3,60}", name):
                counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n))


def _model_from_blob(blob: bytes) -> str | None:
    """Identify the model behind an Antigravity conversation.

    Antigravity records the selected model in field 19 of each generation's
    metadata: a concrete vendor id (`claude-opus-4-6-thinking`) when routing to
    another vendor, or an internal label (`gemini-pro-default`) for its own
    models. A conversation can switch models, so use the most frequently
    selected id rather than allowing any earlier vendor id to override it.

    Never scrape the surrounding transcript: gen_metadata stores conversation
    text next to the metadata, so a loose match reports fragments of the user's
    own source as model names.
    """
    ids = _model_ids(blob)
    if ids:
        return ids[0]
    enum_name = _metadata_entries(blob).get("model_enum", "")
    if enum_name:
        return "antigravity-" + enum_name.removeprefix("MODEL_").lower().replace("_", "-")
    return None


_PARENTHETICAL = re.compile(r"\s*\(.*?\)")


def _model_from_display(display: str) -> str:
    """Turn an Antigravity display name into an API-style model id.

    "Gemini 3.1 Pro (High)" -> "gemini-3.1-pro";
    "Claude Opus 4.6 (Thinking)" -> "claude-opus-4-6".
    """
    name = _PARENTHETICAL.sub("", display).strip().lower().replace(" ", "-")
    if name.startswith("claude"):
        name = name.replace(".", "-")
    return name


def resolve_antigravity_model(
    model_id: str | None, display: str | None, enum_display: str | None = None
) -> str | None:
    """Replace Antigravity routing labels with the model that actually ran.

    Field 19 holds a concrete vendor id (`claude-opus-4-6-thinking`) or a
    routing label ending in `-default` / `-agent` for Antigravity's own tiers.
    For labels, the real model comes from the generation's display name
    (field 21, e.g. "Gemini 3.1 Pro (High)"), or failing that the display name
    seen on another generation with the same `model_enum` — newer builds only
    record it on some rows. With neither, the label is kept (and priced at 0).
    """
    if model_id and not model_id.endswith(("-default", "-agent")):
        return model_id
    for name in (display, enum_display):
        if name:
            return _model_from_display(name)
    return model_id


@dataclass
class _AntigravityGeneration:
    """One model call from gen_metadata (field 1 of each row)."""

    idx: int
    step_idx: int | None  # the steps row holding this call's response
    ts: datetime | None
    usage: dict[str, int]
    model_id: str | None
    display: str | None
    model_enum: str | None


def _fields(data: bytes) -> dict[int, list[int | bytes]]:
    out: dict[int, list[int | bytes]] = {}
    try:
        for number, _wire, value in _protobuf_fields(data):
            out.setdefault(number, []).append(value)
    except ValueError:
        return {}
    return out


def _first(fields: dict[int, list[int | bytes]], number: int, kind: type):
    return next((v for v in fields.get(number, ()) if isinstance(v, kind)), None)


def _text(fields: dict[int, list[int | bytes]], number: int) -> str | None:
    raw = _first(fields, number, bytes)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _proto_timestamp(fields: dict[int, list[int | bytes]] | None) -> datetime | None:
    seconds = _first(fields or {}, 1, int)
    if not seconds:
        return None
    nanos = _first(fields or {}, 2, int) or 0
    try:
        return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_generation(idx: int, blob: bytes) -> _AntigravityGeneration | None:
    """Decode one gen_metadata row.

    Layout (field numbers under the row's field 1): 4 = ModelUsageStats,
    9.4 = created_at Timestamp, 19 = model id, 20 = string map, 21 = display.
    """
    gen_raw = _first(_fields(blob), 1, bytes)
    if gen_raw is None:
        return None
    gen = _fields(gen_raw)
    usage_raw = _first(gen, 4, bytes)
    if usage_raw is None:
        return None
    u = _fields(usage_raw)
    usage = {
        "input_tokens": _first(u, 2, int) or 0,
        "output_tokens": _first(u, 3, int) or 0,
        "cache_creation_tokens": _first(u, 4, int) or 0,
        "cache_read_tokens": _first(u, 5, int) or 0,
        "reasoning_tokens": _first(u, 9, int) or 0,
    }
    metadata = _metadata_entries(blob)
    last_step = metadata.get("last_step_index", "")
    timing = _fields(_first(gen, 9, bytes) or b"")
    return _AntigravityGeneration(
        idx=idx,
        # A generation records the last step it saw; its response is the next.
        step_idx=int(last_step) + 1 if last_step.isdigit() else None,
        ts=_proto_timestamp(_fields(_first(timing, 4, bytes) or b"")),
        usage=usage,
        model_id=_text(gen, 19),
        display=_text(gen, 21),
        model_enum=metadata.get("model_enum"),
    )


def _antigravity_workspaces(root: Path) -> dict[str, str]:
    """conversation_id -> first local workspace folder, from the summaries DB."""
    db_path = root / "conversation_summaries.db"
    if not db_path.is_file():
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT conversation_id, workspace_uris FROM conversation_summaries"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    out: dict[str, str] = {}
    for conversation_id, uris_json in rows:
        try:
            uris = json.loads(uris_json or "[]")
        except json.JSONDecodeError:
            continue
        for uri in uris if isinstance(uris, list) else []:
            if isinstance(uri, str) and uri.startswith("file://"):
                out[conversation_id] = unquote(urlparse(uri).path)
                break
    return out


_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


def _match_antigravity_calls(
    steps: list[tuple[int, datetime, dict]], gens: list[_AntigravityGeneration]
) -> Iterator[tuple[str, int, datetime, dict, _AntigravityGeneration | None, str]]:
    """Attribute each API call to the generation that made it.

    Yields ``(id_key, id_idx, timestamp, usage, generation, source_parser)``.

    Every API response is stored on a step with its exact usage. A
    generation records the last step it saw, and owns the response steps
    after the previous generation up to and including ``last_step_index + 1``.
    Its usage is the *sum* of those steps: usually one, but retried or
    multi-call generations aggregate several. So steps are the unit of
    counting and the generation supplies their model. Whatever part of a
    generation's usage no step accounts for (a turn interrupted before its
    response step was written) is yielded as its own residual event, keeping
    totals equal to the generation totals without double counting.
    """
    step_owner: dict[int, _AntigravityGeneration] = {}
    residuals: list[tuple[_AntigravityGeneration, dict]] = []
    prev_step = -1
    for gen in sorted((g for g in gens if g.step_idx is not None), key=lambda g: (g.step_idx, g.idx)):
        window = [(idx, usage) for idx, _ts, usage in steps if prev_step < idx <= gen.step_idx]
        for idx, _usage in window:
            step_owner[idx] = gen
        prev_step = gen.step_idx
        residual = {k: gen.usage[k] - sum(u[k] for _i, u in window) for k in _TOKEN_KEYS}
        if any(v < 0 for v in residual.values()):
            continue  # steps disagree with the generation; trust the steps
        residual["reasoning_tokens"] = max(
            0, gen.usage["reasoning_tokens"] - sum(u.get("reasoning_tokens", 0) for _i, u in window)
        )
        residuals.append((gen, residual))

    for idx, ts, usage in steps:
        # Keyed on path + step idx: the id scheme rows already in users' DBs
        # were written with, so re-scans update them in place.
        yield "gemini-antigravity", idx, ts, usage, step_owner.get(idx), "antigravity_sqlite_protobuf"

    for gen, residual in residuals:
        if gen.ts is not None and any(residual[k] for k in _TOKEN_KEYS):
            yield "gemini-antigravity-gen", gen.idx, gen.ts, residual, gen, "antigravity_gen_metadata"


def _is_antigravity_dir(path: Path) -> bool:
    return path.name.startswith("antigravity") and (path / "conversations").is_dir()


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
            # Antigravity CLI and IDE keep separate app dirs with one layout.
            fallbacks=["~/.gemini/tmp", "~/.gemini/antigravity-cli", "~/.gemini/antigravity"],
        )
        for ep in candidates:
            if ep == p or not (ep.exists() and ep.is_dir()):
                continue
            kind = "antigravity_sqlite_dir" if _is_antigravity_dir(ep) else "local_jsonl_dir"
            sources.append(DiscoveredSource(provider=self.id, path=ep, kind=kind, exists=True))
        return sources

    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]:
        tz = self.app_config.timezone
        privacy = self.app_config.privacy
        path = source.path
        if path.is_dir() and _is_antigravity_dir(path):
            yield from self._parse_antigravity(path, range_, tz, privacy)
            return
        files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        for f in files:
            yield from self._parse_jsonl(f, range_, tz, privacy)

    def _parse_antigravity(
        self, root: Path, range_: DateRange, tz: str, privacy
    ) -> Iterator[UsageEvent]:
        """Emit one event per API call in an Antigravity app dir.

        Two passes: first load every conversation, learning which display name
        each `model_enum` maps to (newer builds record it on only some rows);
        then attribute calls per conversation via `_match_antigravity_calls`.
        """
        conversations = []
        enum_names: dict[str, str] = {}
        for path in sorted((root / "conversations").glob("*.db")):
            loaded = self._load_antigravity_conversation(path)
            if loaded is None:
                continue
            conversations.append((path, *loaded))
            for gen in loaded[2]:
                if gen.model_enum and gen.display:
                    enum_names.setdefault(gen.model_enum, gen.display)

        workspaces = _antigravity_workspaces(root)
        tool = "antigravity_cli" if root.name.endswith("-cli") else "antigravity"
        for path, session_id, steps, gens, conversation_model in conversations:
            project, project_hash = project_identity(workspaces.get(path.stem), privacy)
            base = {
                "provider": self.id,
                "tool": tool,
                "session_id": session_id,
                "conversation_id": path.stem,
                "project_path": project,
                "project_hash": project_hash,
                "timezone": tz,
                "source_type": "local_sqlite",
                "source_path": str(path),
                "confidence": Confidence.EXACT_FROM_PROVIDER_LOG,
            }
            for id_key, id_idx, ts, usage, gen, parser in _match_antigravity_calls(steps, gens):
                if not range_.start <= local_date(ts, tz) <= range_.end:
                    continue
                model = resolve_antigravity_model(
                    (gen.model_id if gen else None) or conversation_model,
                    gen.display if gen else None,
                    enum_names.get(gen.model_enum or "") if gen else None,
                )
                raw_id = gen.model_id if gen else None
                yield UsageEvent(
                    **base,
                    id=event_id(id_key, str(path), str(id_idx)),
                    model=model,
                    model_alias=raw_id if raw_id and raw_id != model else None,
                    timestamp_start=ts,
                    input_tokens=int(usage["input_tokens"]),
                    output_tokens=int(usage["output_tokens"]),
                    cache_creation_tokens=int(usage["cache_creation_tokens"]),
                    cache_read_tokens=int(usage["cache_read_tokens"]),
                    reasoning_tokens=int(usage.get("reasoning_tokens", 0)),
                    total_tokens=sum(int(usage[k]) for k in _TOKEN_KEYS),
                    source_parser=parser,
                )

    def _load_antigravity_conversation(self, path: Path):
        """Read one conversation DB: (session_id, steps, generations, model).

        `steps` holds (idx, timestamp, usage) for steps carrying a response;
        `model` is the conversation's dominant model id, used as a fallback for
        calls that can't be matched to a generation. Returns None if the file
        is unreadable or not an Antigravity conversation.
        """
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            trajectory_id = connection.execute(
                "SELECT trajectory_id FROM trajectory_meta LIMIT 1"
            ).fetchone()
            steps = []
            step_times: dict[int, datetime] = {}
            for idx, metadata, payload in connection.execute(
                "SELECT idx, metadata, step_payload FROM steps ORDER BY idx"
            ):
                ts = _timestamp_from_step_metadata(metadata or b"")
                if ts is None:
                    continue
                step_times[idx] = ts
                usage = _extract_model_usage_stats(payload or b"")
                if usage is not None:
                    steps.append((idx, ts, usage))
            blobs = [
                (idx, blob)
                for idx, blob in connection.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
                if isinstance(blob, bytes)
            ]
        except (sqlite3.Error, OSError):
            return None
        finally:
            if connection is not None:
                connection.close()

        gens = [g for g in (_parse_generation(idx, blob) for idx, blob in blobs) if g is not None]
        for g in gens:
            if g.ts is None and g.step_idx is not None:
                g.ts = step_times.get(g.step_idx - 1)
        session_id = trajectory_id[0] if trajectory_id else path.stem
        conversation_model = _model_from_blob(b"".join(blob for _idx, blob in blobs))
        return session_id, steps, gens, conversation_model or "gemini-antigravity"

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
                total = (
                    int(total_meta)
                    if total_meta is not None
                    else (input_tokens + output_tokens + cache_read)
                )

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
