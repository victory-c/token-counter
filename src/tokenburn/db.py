from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import sqlite_utils

from .models import UsageEvent

SCHEMA = {
    "usage_events": """
        CREATE TABLE IF NOT EXISTS usage_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            tool TEXT NOT NULL,
            account_plan TEXT,
            model TEXT,
            model_alias TEXT,
            session_id TEXT,
            conversation_id TEXT,
            project_path TEXT,
            project_hash TEXT,
            repo_name TEXT,
            timestamp_start TEXT NOT NULL,
            timestamp_end TEXT,
            timezone TEXT NOT NULL,
            local_date TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_creation_tokens INTEGER,
            cache_read_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            request_count INTEGER DEFAULT 1,
            estimated_cost_usd REAL,
            billed_cost_usd REAL,
            included_subscription_value_usd REAL,
            source_type TEXT NOT NULL,
            source_path TEXT,
            source_parser TEXT NOT NULL,
            confidence TEXT NOT NULL,
            raw_available INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """,
    "providers": """
        CREATE TABLE IF NOT EXISTS providers (
            provider TEXT PRIMARY KEY,
            display_name TEXT,
            default_log_path TEXT,
            parser_version TEXT,
            enabled INTEGER
        )
    """,
    "model_pricing": """
        CREATE TABLE IF NOT EXISTS model_pricing (
            provider TEXT,
            model TEXT,
            effective_date TEXT,
            input_per_million_usd REAL,
            output_per_million_usd REAL,
            cache_write_per_million_usd REAL,
            cache_read_per_million_usd REAL,
            source_url TEXT,
            PRIMARY KEY (provider, model, effective_date)
        )
    """,
    "session_classifications": """
        CREATE TABLE IF NOT EXISTS session_classifications (
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            task_category TEXT NOT NULL,
            confidence REAL,
            classifier TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            features_json TEXT,
            classified_at TEXT NOT NULL,
            PRIMARY KEY (session_id, provider)
        )
    """,
    "session_overrides": """
        CREATE TABLE IF NOT EXISTS session_overrides (
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            task_category TEXT NOT NULL,
            note TEXT,
            set_at TEXT NOT NULL,
            PRIMARY KEY (session_id, provider)
        )
    """,
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_provider_time ON usage_events(provider, timestamp_start)",
    "CREATE INDEX IF NOT EXISTS idx_usage_local_date ON usage_events(local_date)",
    "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model)",
    "CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_path)",
    "CREATE INDEX IF NOT EXISTS idx_usage_project_hash ON usage_events(project_hash)",
    "CREATE INDEX IF NOT EXISTS idx_session_class_category ON session_classifications(task_category)",
    "CREATE INDEX IF NOT EXISTS idx_session_class_provider ON session_classifications(provider)",
]


# ---------------------------------------------------------------------------
# Lightweight in-place migration. SQLite's CREATE TABLE IF NOT EXISTS won't
# add columns to an already-existing table, so a DB created on an older
# version of the package is missing whatever columns we added since.
# Without a fix the indexes DDL below crashes on `local_date`, etc.
#
# This is intentionally one-off code — no migration framework. Each open
# diffs SCHEMA against PRAGMA table_info and ADD COLUMNs anything missing.
# ---------------------------------------------------------------------------

_CONSTRAINT_PREFIXES = ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE ", "CHECK ", "CONSTRAINT ")


def _split_top_level_commas(s: str) -> list[str]:
    """Split on commas at paren-depth zero. Needed because table-level
    constraint clauses like `PRIMARY KEY (a, b)` contain commas that a
    naive split would slice apart."""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _parse_expected_columns(create_sql: str) -> dict[str, str]:
    """Extract {column_name: type_decl} from a CREATE TABLE statement.

    Skips table-level constraints (PRIMARY KEY (...), FOREIGN KEY ..., etc.).
    """
    inner = create_sql[create_sql.index("(") + 1 : create_sql.rindex(")")]
    cols: dict[str, str] = {}
    for raw in _split_top_level_commas(inner):
        line = raw.strip()
        if not line or any(line.upper().startswith(p) for p in _CONSTRAINT_PREFIXES):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cols[parts[0]] = parts[1]
    return cols


def _migrate_table_columns(
    db: sqlite_utils.Database, table_name: str, create_sql: str
) -> None:
    """ALTER TABLE ADD COLUMN for any expected columns missing from the live
    table. SQLite's ADD COLUMN can't add NOT NULL without a DEFAULT, and
    can't introduce PRIMARY KEY at all — we strip both. Old rows stay
    nullable; new rows are validated by the application layer."""
    if table_name not in db.table_names():
        return
    existing = {r["name"] for r in db.query(f"PRAGMA table_info({table_name})")}
    for name, type_decl in _parse_expected_columns(create_sql).items():
        if name in existing:
            continue
        base = re.sub(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", "", type_decl, flags=re.IGNORECASE)
        base = re.sub(r"\s+", " ", base).strip()
        try:
            db.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {base}")
        except sqlite3.OperationalError as exc:
            # Concurrent first-open race: another process added the column
            # between our PRAGMA read and our ALTER. SQLite raises
            # "duplicate column name: foo" — fine, the column is there.
            if "duplicate column" not in str(exc).lower():
                raise


def open_db(path: Path) -> sqlite_utils.Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(str(path))
    # 1. Create fresh tables (no-op for existing tables).
    for ddl in SCHEMA.values():
        db.execute(ddl)
    # 2. Migrate: add columns the live tables are missing.
    for table_name, create_sql in SCHEMA.items():
        _migrate_table_columns(db, table_name, create_sql)
    # 3. Backfill local_date for rows that pre-date the column.
    #    substr(timestamp_start, 1, 10) takes the YYYY-MM-DD prefix; for
    #    timezone-aware timestamps this is UTC-correct enough for legacy
    #    rows (the slight inaccuracy at month boundaries is acceptable
    #    given the alternative is leaving them NULL and breaking date
    #    filters silently).
    if "usage_events" in db.table_names():
        db.execute(
            "UPDATE usage_events SET local_date = substr(timestamp_start, 1, 10) "
            "WHERE local_date IS NULL OR local_date = ''"
        )
    # 4. Indexes — safe now that all expected columns exist.
    for ddl in INDEXES:
        db.execute(ddl)
    db.conn.commit()
    return db


def upsert_event(db: sqlite_utils.Database, event: UsageEvent) -> None:
    row = event.to_row()
    row["raw_available"] = int(row["raw_available"])
    db["usage_events"].insert(row, pk="id", replace=True)


def upsert_events(db: sqlite_utils.Database, events: list[UsageEvent]) -> int:
    if not events:
        return 0
    rows = []
    for e in events:
        row = e.to_row()
        row["raw_available"] = int(row["raw_available"])
        rows.append(row)
    db["usage_events"].insert_all(rows, pk="id", replace=True, batch_size=500)
    return len(rows)
