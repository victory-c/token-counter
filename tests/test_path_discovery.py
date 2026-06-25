from __future__ import annotations

from pathlib import Path

from tokenburn.adapters.claude_code import ClaudeCodeAdapter
from tokenburn.adapters.codex import CodexAdapter
from tokenburn.config import ProviderConfig
from tokenburn.util.paths import resolve_log_dirs


def test_resolve_log_dirs_honors_explicit_paths_verbatim(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/not/be/used")
    dirs = resolve_log_dirs(
        ["~/custom/a", "~/custom/b"],
        env_subdirs=[("CLAUDE_CONFIG_DIR", "projects")],
        fallbacks=["~/.claude/projects"],
    )
    assert dirs == [Path.home() / "custom" / "a", Path.home() / "custom" / "b"]


def test_resolve_log_dirs_probes_env_then_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    dirs = resolve_log_dirs(
        None,
        env_subdirs=[("CODEX_HOME", "sessions")],
        fallbacks=["~/.codex/sessions", "~/.config/codex/sessions"],
    )
    assert dirs[0] == (tmp_path / "codexhome" / "sessions").resolve()
    assert Path.home() / ".codex" / "sessions" in dirs
    assert Path.home() / ".config" / "codex" / "sessions" in dirs


def test_resolve_log_dirs_dedups_preserving_order(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dirs = resolve_log_dirs(
        None,
        env_subdirs=[],
        fallbacks=["~/.claude/projects", "~/.claude/projects", "~/.config/claude/projects"],
    )
    assert dirs == [Path.home() / ".claude" / "projects", Path.home() / ".config" / "claude" / "projects"]


def test_claude_discover_finds_custom_config_dir(monkeypatch, tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    projects = tmp_path / "altclaude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "altclaude"))

    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[])
    sources = ClaudeCodeAdapter(cfg, pcfg).discover()

    found = [s for s in sources if s.exists]
    assert found, "custom CLAUDE_CONFIG_DIR/projects should be discovered"
    assert found[0].path == projects.resolve()


def test_claude_discover_falls_back_to_canonical_when_nothing_exists(monkeypatch, tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    # Point HOME somewhere empty and clear the env override so no candidate exists.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "emptyhome"))

    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[])
    sources = ClaudeCodeAdapter(cfg, pcfg).discover()

    assert len(sources) == 1
    assert sources[0].exists is False
    assert sources[0].path.name == "projects"


def test_codex_discover_uses_codex_home(monkeypatch, tmp_path, tmp_app_config):
    cfg, _ = tmp_app_config
    sessions = tmp_path / "ch" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))

    pcfg = ProviderConfig(enabled=True, source="local_jsonl", paths=[])
    sources = CodexAdapter(cfg, pcfg).discover()

    found = [s for s in sources if s.exists]
    assert found and found[0].path == sessions.resolve()
