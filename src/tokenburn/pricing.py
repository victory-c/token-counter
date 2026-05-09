from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from .models import UsageEvent


@dataclass(frozen=True)
class PriceRow:
    provider: str
    model: str
    effective_date: date
    input_per_million_usd: float
    output_per_million_usd: float
    cache_write_per_million_usd: float
    cache_read_per_million_usd: float
    source_url: str


class PricingTable:
    def __init__(self, rows: list[PriceRow]) -> None:
        self._by_key: dict[tuple[str, str], list[PriceRow]] = defaultdict(list)
        for r in rows:
            self._by_key[(r.provider, r.model)].append(r)
        for k in self._by_key:
            self._by_key[k].sort(key=lambda r: r.effective_date)

    @classmethod
    def load(cls, path: Path) -> PricingTable:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        rows = []
        for raw in data.get("prices", []):
            ed = raw["effective_date"]
            if isinstance(ed, str):
                ed = date.fromisoformat(ed)
            rows.append(
                PriceRow(
                    provider=raw["provider"],
                    model=raw["model"],
                    effective_date=ed,
                    input_per_million_usd=float(raw.get("input_per_million_usd", 0.0)),
                    output_per_million_usd=float(raw.get("output_per_million_usd", 0.0)),
                    cache_write_per_million_usd=float(raw.get("cache_write_per_million_usd", 0.0)),
                    cache_read_per_million_usd=float(raw.get("cache_read_per_million_usd", 0.0)),
                    source_url=str(raw.get("source_url", "")),
                )
            )
        return cls(rows)

    def lookup(self, provider: str, model: str | None, when: datetime) -> PriceRow | None:
        if model is None:
            return None
        # Try exact (provider, model) first; fall back to model-prefix match.
        key = (provider, self._normalize(model))
        candidates = self._by_key.get(key)
        if not candidates:
            candidates = self._best_prefix_match(provider, model)
        if not candidates:
            return None
        target = when.date()
        applicable = [r for r in candidates if r.effective_date <= target]
        return applicable[-1] if applicable else None

    def _normalize(self, model: str) -> str:
        return model.lower().split("@")[0].strip()

    def _best_prefix_match(self, provider: str, model: str) -> list[PriceRow]:
        m = self._normalize(model)
        best_key = None
        best_len = 0
        for (p, mk) in self._by_key:
            if p != provider:
                continue
            if m.startswith(mk) and len(mk) > best_len:
                best_key = (p, mk)
                best_len = len(mk)
        return self._by_key.get(best_key, []) if best_key else []


def estimate_cost(event: UsageEvent, table: PricingTable) -> float | None:
    row = table.lookup(event.provider, event.model, event.timestamp_start)
    if row is None:
        return None
    cost = 0.0
    cost += (event.input_tokens or 0) / 1_000_000 * row.input_per_million_usd
    cost += (event.output_tokens or 0) / 1_000_000 * row.output_per_million_usd
    cost += (event.cache_creation_tokens or 0) / 1_000_000 * row.cache_write_per_million_usd
    cost += (event.cache_read_tokens or 0) / 1_000_000 * row.cache_read_per_million_usd
    # Reasoning tokens are billed as output by major vendors (OpenAI o-series, Gemini thinking).
    cost += (event.reasoning_tokens or 0) / 1_000_000 * row.output_per_million_usd
    return round(cost, 6)


def default_pricing_path() -> Path:
    """Look for pricing.yaml in the package root (alongside pyproject.toml)."""
    pkg_root = Path(__file__).resolve().parents[2]
    candidate = pkg_root / "pricing.yaml"
    if candidate.exists():
        return candidate
    # Fallback to user config dir
    user_copy = Path.home() / ".tokenburn" / "pricing.yaml"
    return user_copy
