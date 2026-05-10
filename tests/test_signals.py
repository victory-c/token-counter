from __future__ import annotations

from pathlib import Path

from tokenburn.classifier.signals import claude_code_features, codex_features


def test_claude_features_buckets_by_session():
    fix = Path(__file__).parent / "fixtures" / "claude" / "-Users-victorchun-demo" / "sess1.jsonl"
    sessions = claude_code_features([fix])
    assert "sess1" in sessions
    f = sessions["sess1"]
    assert f.provider == "claude_code"
    # 4 assistant rows in the fixture; 1 is rate-limit/synthetic and gets
    # filtered; the remaining 3 are valid assistant turns.
    assert f.turn_count == 3
    assert f.cwd == "/Users/victorchun/demo"
    assert f.git_branch == "main"
    # No tool_use blocks in this fixture, so all tool counts are zero
    assert f.read_count == 0
    assert f.edit_count == 0


def test_codex_features_buckets_by_file_stem():
    fix = (
        Path(__file__).parent
        / "fixtures" / "codex" / "sessions" / "2026" / "04" / "15"
        / "rollout-2026-04-15T10-00-00-aaaa.jsonl"
    )
    sessions = codex_features([fix])
    assert "rollout-2026-04-15T10-00-00-aaaa" in sessions
    f = sessions["rollout-2026-04-15T10-00-00-aaaa"]
    assert f.provider == "codex"
    assert f.cwd == "/Users/victorchun/demo"
