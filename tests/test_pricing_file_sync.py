from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_PRICING = REPO_ROOT / "pricing.yaml"
PACKAGED_PRICING = REPO_ROOT / "src" / "tokenburn" / "pricing.yaml"


def test_packaged_pricing_matches_root_pricing():
    """The two pricing.yaml copies must stay byte-identical.

    `default_pricing_path()` prefers the repo-root copy in a source checkout
    but falls back to the packaged one inside a wheel. When only the root copy
    is edited the tests still pass while every installed user silently prices
    against a stale table, which is exactly how the Claude 5 rows went missing
    from released builds.
    """
    assert PACKAGED_PRICING.read_text() == ROOT_PRICING.read_text(), (
        "pricing.yaml and src/tokenburn/pricing.yaml have diverged. "
        "Copy the root file over the packaged one: "
        "cp pricing.yaml src/tokenburn/pricing.yaml"
    )


@pytest.mark.parametrize("path", [ROOT_PRICING, PACKAGED_PRICING], ids=["root", "packaged"])
def test_pricing_rows_are_well_formed(path: Path):
    rows = yaml.safe_load(path.read_text())["prices"]
    required = {
        "provider",
        "model",
        "effective_date",
        "input_per_million_usd",
        "output_per_million_usd",
        "cache_write_per_million_usd",
        "cache_read_per_million_usd",
        "source_url",
    }
    seen: set[tuple[str, str, object]] = set()
    for row in rows:
        missing = required - row.keys()
        assert not missing, f"{row.get('provider')}/{row.get('model')} missing {sorted(missing)}"
        assert row["source_url"], f"{row['provider']}/{row['model']} has no source_url"

        key = (row["provider"], row["model"], row["effective_date"])
        assert key not in seen, f"duplicate row for {key}"
        seen.add(key)
