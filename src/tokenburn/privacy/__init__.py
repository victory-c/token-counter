from __future__ import annotations

from typing import Any

from ..util.hashing import hash_path


def project_identity(project_path: str | None, privacy: Any) -> tuple[str | None, str | None]:
    """Return the project path fields that are safe to persist."""
    if not project_path:
        return None, None

    project_hash = hash_path(project_path) if privacy.hash_project_paths else None
    if privacy.hash_project_paths or privacy.redact_project_paths:
        return None, project_hash
    return project_path, project_hash
