from __future__ import annotations

import contextlib
import os
from pathlib import Path


def resolve_log_dirs(
    explicit: list[str] | None,
    env_subdirs: list[tuple[str, str]],
    fallbacks: list[str],
) -> list[Path]:
    """Ordered, de-duplicated candidate log directories for a provider.

    Discovery is intentionally machine-agnostic so a clone runs anywhere:

    * If ``explicit`` paths are configured, they are honoured verbatim — the
      user has told us exactly where to look, so we don't second-guess.
    * Otherwise we probe environment-variable homes first (e.g. a custom
      ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``), then common fallback locations
      (``~/.claude/projects``, the XDG-style ``~/.config/...``). ``~`` and env
      vars expand per-user, so nothing is pinned to the author's machine.

    Returns expanded, resolved paths with duplicates removed (preserving order).
    Existence is *not* filtered here — callers decide whether to keep
    non-existent candidates for "here's where I looked" diagnostics.
    """
    raw: list[str] = []
    if explicit:
        raw.extend(explicit)
    else:
        for env_var, subdir in env_subdirs:
            val = os.environ.get(env_var)
            if val:
                raw.append(str(Path(val) / subdir) if subdir else val)
        raw.extend(fallbacks)

    seen: set[Path] = set()
    out: list[Path] = []
    for r in raw:
        p = Path(str(r)).expanduser()
        with contextlib.suppress(OSError):
            p = p.resolve()
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def redact_home(path: str | Path) -> str:
    s = str(path)
    home = str(Path.home())
    if s.startswith(home):
        return "~" + s[len(home):]
    return s


def decode_claude_project_dir(dir_name: str) -> str:
    """Claude Code encodes project paths in dir names by replacing / with -.
    e.g. -Users-victorchun-token-counter -> /Users/victorchun/token-counter
    """
    if dir_name.startswith("-"):
        return "/" + dir_name[1:].replace("-", "/")
    return dir_name.replace("-", "/")
