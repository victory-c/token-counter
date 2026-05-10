"""Classifier orchestration.

Reads source JSONL via signals.iter_session_features, runs the heuristic
classifier on each session, and persists the result into
session_classifications. Returns per-provider counts for CLI feedback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlite_utils

from ..config import AppConfig
from ..privacy import project_identity
from .heuristic import classify
from .signals import (
    SessionFeatures,
    claude_code_features,
    codex_features,
    discover_claude_jsonl,
    discover_codex_rollouts,
)
from .taxonomy import CLASSIFIER_VERSION, TaskCategory


@dataclass
class ClassifyReport:
    provider_counts: dict[str, int]
    category_counts: dict[TaskCategory, int]
    skipped_existing: int

    def total(self) -> int:
        return sum(self.provider_counts.values())


def _load_existing_session_ids(db: sqlite_utils.Database, provider: str) -> set[str]:
    rows = db.query(
        "SELECT session_id FROM session_classifications WHERE provider = :p",
        {"p": provider},
    )
    return {r["session_id"] for r in rows}


def _persist(
    db: sqlite_utils.Database,
    feat: SessionFeatures,
    cls,
    cfg: AppConfig,
) -> None:
    features = feat.to_json_safe()
    cwd, cwd_hash = project_identity(features.get("cwd"), cfg.privacy)
    features["cwd"] = cwd
    if cwd_hash:
        features["cwd_hash"] = cwd_hash

    row = {
        "session_id": feat.session_id,
        "provider": feat.provider,
        "task_category": cls.category.value,
        "confidence": cls.confidence,
        "classifier": "heuristic",
        "classifier_version": CLASSIFIER_VERSION,
        "features_json": json.dumps(features, default=str),
        "classified_at": datetime.now(UTC).isoformat(),
    }
    db["session_classifications"].insert(
        row, pk=("session_id", "provider"), replace=True
    )


def classify_range(
    db: sqlite_utils.Database,
    cfg: AppConfig,
    providers: list[str] | None = None,
    *,
    reclassify: bool = False,
    progress_cb=None,
) -> ClassifyReport:
    """Classify all sessions for the requested providers.

    `progress_cb(provider, current, total)` is called as work progresses
    (used by the CLI to draw a progress bar; tests pass None).
    """
    targets = providers or ["claude_code", "codex"]
    provider_counts: dict[str, int] = {}
    category_counts: dict[TaskCategory, int] = {c: 0 for c in TaskCategory}
    skipped = 0

    if "claude_code" in targets:
        existing = set() if reclassify else _load_existing_session_ids(db, "claude_code")
        sessions = claude_code_features(discover_claude_jsonl(cfg))
        sids = list(sessions)
        for i, sid in enumerate(sids, start=1):
            if progress_cb:
                progress_cb("claude_code", i, len(sids))
            if sid in existing:
                skipped += 1
                continue
            cls = classify(sessions[sid])
            _persist(db, sessions[sid], cls, cfg)
            category_counts[cls.category] += 1
        provider_counts["claude_code"] = len(sids) - (skipped if not reclassify else 0)

    if "codex" in targets:
        existing = set() if reclassify else _load_existing_session_ids(db, "codex")
        sessions = codex_features(discover_codex_rollouts(cfg))
        sids = list(sessions)
        prev_skipped = skipped
        for i, sid in enumerate(sids, start=1):
            if progress_cb:
                progress_cb("codex", i, len(sids))
            if sid in existing:
                skipped += 1
                continue
            cls = classify(sessions[sid])
            _persist(db, sessions[sid], cls, cfg)
            category_counts[cls.category] += 1
        provider_counts["codex"] = len(sids) - (skipped - prev_skipped if not reclassify else 0)

    db.conn.commit()
    return ClassifyReport(
        provider_counts=provider_counts,
        category_counts=category_counts,
        skipped_existing=skipped,
    )
