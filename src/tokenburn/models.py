from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import Enum


class Confidence(str, Enum):
    EXACT_FROM_PROVIDER_LOG = "exact_from_provider_log"
    EXACT_FROM_LOCAL_LOG = "exact_from_local_log"
    ESTIMATED_FROM_SESSION_SUMMARY = "estimated_from_session_summary"
    ESTIMATED_FROM_TEXT_RETOKENIZATION = "estimated_from_text_retokenization"
    MANUAL_IMPORT = "manual_import"
    UNAVAILABLE = "unavailable"


@dataclass
class DateRange:
    start: date
    end: date

    def contains(self, ts: datetime) -> bool:
        d = ts.date()
        return self.start <= d <= self.end


@dataclass
class UsageEvent:
    id: str
    provider: str
    tool: str
    timestamp_start: datetime
    timezone: str
    source_type: str
    source_parser: str
    confidence: Confidence

    account_plan: str | None = None
    model: str | None = None
    model_alias: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    project_path: str | None = None
    project_hash: str | None = None
    repo_name: str | None = None
    timestamp_end: datetime | None = None

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    request_count: int = 1
    estimated_cost_usd: float | None = None
    billed_cost_usd: float | None = None
    included_subscription_value_usd: float | None = None

    source_path: str | None = None
    raw_available: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_row(self) -> dict:
        from zoneinfo import ZoneInfo

        row = asdict(self)
        row["confidence"] = self.confidence.value
        row["timestamp_start"] = self.timestamp_start.isoformat()
        row["timestamp_end"] = self.timestamp_end.isoformat() if self.timestamp_end else None
        row["created_at"] = self.created_at.isoformat()
        local = (
            self.timestamp_start.astimezone(ZoneInfo(self.timezone))
            if self.timestamp_start.tzinfo
            else self.timestamp_start
        )
        row["local_date"] = local.date().isoformat()
        return row
