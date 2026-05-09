from __future__ import annotations

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
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_provider_time ON usage_events(provider, timestamp_start)",
    "CREATE INDEX IF NOT EXISTS idx_usage_local_date ON usage_events(local_date)",
    "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model)",
    "CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_path)",
]


def open_db(path: Path) -> sqlite_utils.Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(str(path))
    for ddl in SCHEMA.values():
        db.execute(ddl)
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
