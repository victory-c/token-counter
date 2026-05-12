"""Regression tests for the lightweight in-place migration in open_db.

Anyone who installed an early version of tokenburn before the `local_date`
column existed would crash on the next open because the index DDL runs on
every open. open_db now diffs SCHEMA against PRAGMA table_info and
ALTER TABLE ADD COLUMNs anything missing.
"""
from __future__ import annotations

import sqlite3

from tokenburn.db import _parse_expected_columns, _split_top_level_commas, open_db


def _make_v01_style_db(path) -> None:
    """Hand-write a usage_events table that mirrors a pre-v0.2 layout
    (no local_date, no project_hash). Insert one row so the migration
    has data to backfill."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE usage_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            tool TEXT NOT NULL,
            timestamp_start TEXT NOT NULL,
            timezone TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_parser TEXT NOT NULL,
            confidence TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO usage_events "
        "(id, provider, tool, timestamp_start, timezone, source_type, source_parser, confidence, created_at) "
        "VALUES ('e1', 'claude_code', 'claude_code', '2026-04-15T10:00:00+00:00', "
        "'UTC', 'local_jsonl', 'native', 'exact', '2026-04-15T10:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_open_db_adds_missing_columns_on_v01_style_db(tmp_path):
    p = tmp_path / "old.sqlite"
    _make_v01_style_db(p)

    db = open_db(p)
    cols = {r["name"] for r in db.query("PRAGMA table_info(usage_events)")}

    # All schema columns should be present after migration.
    expected = {
        "local_date", "project_hash", "repo_name", "model", "session_id",
        "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "reasoning_tokens", "total_tokens",
        "estimated_cost_usd", "billed_cost_usd",
        "included_subscription_value_usd", "raw_available",
    }
    assert expected <= cols


def test_open_db_backfills_local_date(tmp_path):
    p = tmp_path / "old.sqlite"
    _make_v01_style_db(p)

    db = open_db(p)
    row = next(iter(db.query("SELECT local_date FROM usage_events WHERE id='e1'")))
    assert row["local_date"] == "2026-04-15"


def test_open_db_is_idempotent_on_fresh_db(tmp_path):
    p = tmp_path / "fresh.sqlite"
    db1 = open_db(p)
    cols1 = {r["name"] for r in db1.query("PRAGMA table_info(usage_events)")}
    # Re-open the same DB; should succeed (no duplicate ADD COLUMN errors).
    db2 = open_db(p)
    cols2 = {r["name"] for r in db2.query("PRAGMA table_info(usage_events)")}
    assert cols1 == cols2
    assert "local_date" in cols2


def test_open_db_creates_full_schema_on_fresh_db(tmp_path):
    db = open_db(tmp_path / "new.sqlite")
    tables = set(db.table_names())
    # All five tables get created on a fresh DB.
    assert {
        "usage_events",
        "providers",
        "model_pricing",
        "session_classifications",
        "session_overrides",
    } <= tables


def test_parse_expected_columns_skips_table_level_primary_key():
    """PRIMARY KEY (a, b) clauses must not be misinterpreted as a column —
    they contain commas inside parens that would trip a naive split."""
    sql = """
        CREATE TABLE IF NOT EXISTS thing (
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            note TEXT,
            PRIMARY KEY (session_id, provider)
        )
    """
    cols = _parse_expected_columns(sql)
    assert set(cols.keys()) == {"session_id", "provider", "note"}


def test_split_top_level_commas_respects_paren_depth():
    assert _split_top_level_commas("a, b, c") == ["a", " b", " c"]
    assert _split_top_level_commas("a, foo(x, y), b") == ["a", " foo(x, y)", " b"]


def test_migrate_swallows_concurrent_duplicate_column_race(tmp_path):
    """Simulates the concurrent first-open race: process A reads PRAGMA
    table_info and sees `local_date` missing, then process B sneaks in
    and adds the column, then process A tries ALTER TABLE and SQLite
    raises 'duplicate column name'. The migration must swallow that
    specific error and continue."""
    import sqlite3
    from unittest.mock import patch

    from tokenburn.db import SCHEMA, _migrate_table_columns

    p = tmp_path / "race.sqlite"
    _make_v01_style_db(p)
    # First open: do everything the live code does, get the DB in v0.2 shape.
    open_db(p).close()

    # Now stage the race: monkey-patch table_info to claim a column is
    # missing (forcing an ADD COLUMN), but the column actually exists.
    # ALTER TABLE will then raise "duplicate column name" — the migration
    # should catch it and not re-raise.
    import sqlite_utils

    db = sqlite_utils.Database(str(p))
    create_sql = SCHEMA["usage_events"]

    original_query = db.query

    def fake_query(sql, *args, **kwargs):
        # Pretend local_date is missing from the live table.
        if "PRAGMA table_info(usage_events)" in sql:
            rows = list(original_query(sql, *args, **kwargs))
            return iter([r for r in rows if r["name"] != "local_date"])
        return original_query(sql, *args, **kwargs)

    with patch.object(db, "query", side_effect=fake_query):
        # Must NOT raise.
        _migrate_table_columns(db, "usage_events", create_sql)

    # And a non-duplicate OperationalError should still propagate.
    def bad_execute(sql, *args, **kwargs):
        raise sqlite3.OperationalError("some unrelated error")

    with patch.object(db, "execute", side_effect=bad_execute), \
         patch.object(db, "query", side_effect=fake_query):
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            _migrate_table_columns(db, "usage_events", create_sql)
