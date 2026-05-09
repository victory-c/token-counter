from __future__ import annotations

import hashlib


def event_id(*parts: str) -> str:
    h = hashlib.sha256()
    h.update("|".join(parts).encode("utf-8"))
    return h.hexdigest()


def hash_path(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
