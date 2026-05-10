"""Per-session feature extraction from raw JSONL.

The classifier needs more than what's in usage_events — it needs tool-call
sequences, message-text patterns, and file-extension fingerprints. This module
walks the same JSONL files the adapters parse, but produces a SessionFeatures
object per (provider, session_id).

Privacy: keyword scanning runs in-memory; only set-membership booleans
(via `keywords_present`) are persisted, never raw text.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import AppConfig, expand
from .taxonomy import all_keywords

_KEYWORDS = all_keywords()
# Require at least one letter, not pure-digit (avoids matching version
# fragments like ".15" or ".0.9").
_FILE_EXT_PATTERN = re.compile(r"\.([A-Za-z][A-Za-z0-9]{0,5})\b")


@dataclass
class SessionFeatures:
    provider: str
    session_id: str

    # Volume
    turn_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    total_tokens: int = 0

    # Tool use (Claude Code naming; Codex equivalents fold in here too)
    read_count: int = 0
    edit_count: int = 0
    write_count: int = 0
    bash_count: int = 0
    grep_count: int = 0
    glob_count: int = 0
    web_search_count: int = 0
    web_fetch_count: int = 0
    todowrite_count: int = 0
    # Codex-specific (kept separate so heuristic can cross-reference)
    exec_command_count: int = 0
    apply_patch_count: int = 0

    # Content fingerprint
    files_touched: int = 0
    file_extensions_touched: set[str] = field(default_factory=set)
    user_message_total_chars: int = 0
    first_user_message_chars: int = 0
    keywords_present: set[str] = field(default_factory=set)

    # Context
    git_branch: str | None = None
    cwd: str | None = None
    duration_seconds: float = 0.0

    def to_json_safe(self) -> dict:
        d = asdict(self)
        d["file_extensions_touched"] = sorted(self.file_extensions_touched)
        d["keywords_present"] = sorted(self.keywords_present)
        return d


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

# Tool names from Claude Code that we track. These come from the JSONL
# `message.content[*].toolUse.name` field on assistant turns.
_CLAUDE_TOOL_FIELD = {
    "Read": "read_count",
    "Edit": "edit_count",
    "Write": "write_count",
    "MultiEdit": "edit_count",
    "Bash": "bash_count",
    "Grep": "grep_count",
    "Glob": "glob_count",
    "WebSearch": "web_search_count",
    "WebFetch": "web_fetch_count",
    "TodoWrite": "todowrite_count",
}

# Common file paths appear in tool args; we sniff these to fill files_touched
# and file_extensions_touched.
_PATH_KEYS = ("file_path", "path", "notebook_path", "files")


def claude_code_features(jsonl_paths: Iterable[Path]) -> dict[str, SessionFeatures]:
    """Walk Claude Code JSONL files and bucket features by sessionId."""
    sessions: dict[str, SessionFeatures] = {}
    earliest: dict[str, float] = {}
    latest: dict[str, float] = {}
    files_per_session: dict[str, set[str]] = defaultdict(set)

    for path in jsonl_paths:
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                sid = rec.get("sessionId")
                if not sid:
                    continue
                feat = sessions.get(sid)
                if feat is None:
                    feat = SessionFeatures(provider="claude_code", session_id=sid)
                    sessions[sid] = feat
                ts_raw = rec.get("timestamp")
                ts_seconds = _iso_to_seconds(ts_raw)
                if ts_seconds is not None:
                    if sid not in earliest or ts_seconds < earliest[sid]:
                        earliest[sid] = ts_seconds
                    if sid not in latest or ts_seconds > latest[sid]:
                        latest[sid] = ts_seconds

                feat.cwd = rec.get("cwd") or feat.cwd
                feat.git_branch = rec.get("gitBranch") or feat.git_branch

                if rtype == "user":
                    feat.user_message_count += 1
                    text = _extract_user_text(rec)
                    if text:
                        if feat.first_user_message_chars == 0:
                            feat.first_user_message_chars = len(text)
                        feat.user_message_total_chars += len(text)
                        _scan_keywords(text, feat.keywords_present)
                        _scan_file_extensions(text, feat.file_extensions_touched)
                elif rtype == "assistant":
                    if rec.get("isApiErrorMessage"):
                        continue
                    msg = rec.get("message") or {}
                    if (msg.get("model") or "") == "<synthetic>":
                        continue
                    feat.assistant_message_count += 1
                    feat.turn_count += 1
                    usage = msg.get("usage") or {}
                    feat.total_tokens += int(usage.get("input_tokens") or 0)
                    feat.total_tokens += int(usage.get("output_tokens") or 0)
                    feat.total_tokens += int(usage.get("cache_creation_input_tokens") or 0)
                    feat.total_tokens += int(usage.get("cache_read_input_tokens") or 0)
                    _scan_assistant_tools(msg.get("content") or [], feat, files_per_session[sid])

    # Materialize derived fields
    for sid, feat in sessions.items():
        feat.files_touched = len(files_per_session[sid])
        if sid in earliest and sid in latest:
            feat.duration_seconds = max(0.0, latest[sid] - earliest[sid])
    return sessions


def _extract_user_text(rec: dict) -> str:
    msg = rec.get("message") or rec.get("content")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(c.get("text", "")) for c in content if isinstance(c, dict)
            )
    if isinstance(rec.get("content"), str):
        return rec["content"]
    return ""


def _scan_keywords(text: str, target: set[str]) -> None:
    if not text:
        return
    lowered = text.lower()
    for kw in _KEYWORDS:
        if kw in lowered:
            target.add(kw)


def _scan_file_extensions(text: str, target: set[str]) -> None:
    if not text:
        return
    for m in _FILE_EXT_PATTERN.finditer(text):
        ext = "." + m.group(1).lower()
        if 2 <= len(ext) <= 6:
            target.add(ext)


def _scan_assistant_tools(content_blocks: list, feat: SessionFeatures, files: set[str]) -> None:
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name") or ""
            attr = _CLAUDE_TOOL_FIELD.get(name)
            if attr:
                setattr(feat, attr, getattr(feat, attr) + 1)
            tool_input = block.get("input") or {}
            _record_files(tool_input, feat, files)


def _record_files(tool_input: dict, feat: SessionFeatures, files: set[str]) -> None:
    for key in _PATH_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            files.add(v)
            _track_extension(v, feat.file_extensions_touched)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    files.add(item)
                    _track_extension(item, feat.file_extensions_touched)


def _track_extension(p: str, target: set[str]) -> None:
    name = os.path.basename(p)
    if "." not in name:
        return
    ext = "." + name.rsplit(".", 1)[-1].lower()
    if 2 <= len(ext) <= 8:
        target.add(ext)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def codex_features(rollout_paths: Iterable[Path]) -> dict[str, SessionFeatures]:
    """Walk Codex rollout-*.jsonl files; bucket features by file stem (= session)."""
    sessions: dict[str, SessionFeatures] = {}

    for path in rollout_paths:
        sid = path.stem  # rollout-YYYY-MM-DDTHH-MM-SS-<uuid>
        feat = SessionFeatures(provider="codex", session_id=sid)
        earliest: float | None = None
        latest: float | None = None
        files: set[str] = set()

        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                payload = rec.get("payload") or {}
                ts_seconds = _iso_to_seconds(rec.get("timestamp"))
                if ts_seconds is not None:
                    earliest = ts_seconds if earliest is None else min(earliest, ts_seconds)
                    latest = ts_seconds if latest is None else max(latest, ts_seconds)

                if rtype == "turn_context":
                    feat.cwd = payload.get("cwd") or feat.cwd
                    continue

                if rtype != "response_item":
                    if rtype == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        last = info.get("last_token_usage") or {}
                        feat.total_tokens += int(last.get("total_tokens") or 0)
                    continue

                ptype = payload.get("type")
                if ptype == "function_call":
                    feat.turn_count += 1
                    name = payload.get("name") or ""
                    if name == "exec_command":
                        feat.exec_command_count += 1
                    elif name == "apply_patch":
                        feat.apply_patch_count += 1
                        feat.edit_count += 1  # treat patches as edits
                    elif name == "shell":
                        feat.bash_count += 1
                    args_raw = payload.get("arguments") or "{}"
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except json.JSONDecodeError:
                        args = {}
                    if isinstance(args, dict):
                        _record_files(args, feat, files)
                elif ptype == "message":
                    role = payload.get("role")
                    text_blocks = payload.get("content") or []
                    text = " ".join(
                        b.get("text", "") for b in text_blocks if isinstance(b, dict)
                    )
                    if role == "user":
                        feat.user_message_count += 1
                        if text:
                            if feat.first_user_message_chars == 0:
                                feat.first_user_message_chars = len(text)
                            feat.user_message_total_chars += len(text)
                            _scan_keywords(text, feat.keywords_present)
                            _scan_file_extensions(text, feat.file_extensions_touched)
                    elif role == "assistant":
                        feat.assistant_message_count += 1

        feat.files_touched = len(files)
        if earliest is not None and latest is not None:
            feat.duration_seconds = max(0.0, latest - earliest)
        sessions[sid] = feat

    return sessions


# ---------------------------------------------------------------------------
# Source discovery (mirrors adapter discovery, but for the classifier)
# ---------------------------------------------------------------------------


def discover_claude_jsonl(cfg: AppConfig) -> list[Path]:
    pcfg = cfg.providers.get("claude_code")
    if not pcfg or not pcfg.enabled:
        return []
    out: list[Path] = []
    for raw in pcfg.paths or ["~/.claude/projects"]:
        root = expand(raw)
        if root.exists():
            out.extend(sorted(root.glob("**/*.jsonl")))
    return out


def discover_codex_rollouts(cfg: AppConfig) -> list[Path]:
    pcfg = cfg.providers.get("codex")
    if not pcfg or not pcfg.enabled:
        return []
    explicit = pcfg.paths or []
    candidates: list[Path] = []
    if explicit:
        candidates.extend(expand(p) for p in explicit)
    elif (codex_home := os.environ.get("CODEX_HOME")):
        candidates.append(expand(codex_home) / "sessions")
    else:
        candidates.append(expand("~/.codex/sessions"))
    out: list[Path] = []
    for root in candidates:
        if root.exists():
            out.extend(sorted(root.glob("**/rollout-*.jsonl")))
    return out


def _iso_to_seconds(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        # ISO 8601 with Z suffix or offset; we just want a monotone scalar.
        from datetime import datetime
        from dateutil import parser as dt_parser
        dt = dt_parser.isoparse(ts)
        return dt.timestamp()
    except (ValueError, TypeError, OSError):
        return None


def iter_session_features(cfg: AppConfig, providers: Iterable[str]) -> Iterator[SessionFeatures]:
    """Yield SessionFeatures for every session of every requested provider."""
    providers = set(providers)
    if "claude_code" in providers:
        for feat in claude_code_features(discover_claude_jsonl(cfg)).values():
            yield feat
    if "codex" in providers:
        for feat in codex_features(discover_codex_rollouts(cfg)).values():
            yield feat
