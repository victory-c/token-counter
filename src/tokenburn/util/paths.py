from __future__ import annotations

from pathlib import Path


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
