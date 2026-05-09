from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig, ProviderConfig
from ..models import DateRange, UsageEvent


@dataclass
class DiscoveredSource:
    provider: str
    path: Path
    kind: str
    exists: bool


@dataclass
class ProviderSummary:
    provider: str
    event_count: int
    total_tokens: int
    sources: list[DiscoveredSource]


class ProviderAdapter(ABC):
    id: str
    display_name: str

    def __init__(self, app_config: AppConfig, provider_config: ProviderConfig) -> None:
        self.app_config = app_config
        self.provider_config = provider_config

    @abstractmethod
    def discover(self) -> list[DiscoveredSource]: ...

    @abstractmethod
    def parse(self, source: DiscoveredSource, range_: DateRange) -> Iterator[UsageEvent]: ...

    def summarize(self, events: list[UsageEvent]) -> ProviderSummary:
        total = sum((e.total_tokens or 0) for e in events)
        return ProviderSummary(
            provider=self.id,
            event_count=len(events),
            total_tokens=total,
            sources=self.discover(),
        )
