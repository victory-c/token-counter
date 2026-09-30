from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokenburn.adapters.base import DiscoveredSource
from tokenburn.adapters.gemini import (
    GeminiAdapter,
    _encode_varint,
    _extract_model_usage_stats,
    _model_from_blob,
    resolve_antigravity_model,
)
from tokenburn.config import ProviderConfig
from tokenburn.models import DateRange
from tokenburn.pricing import PricingTable, default_pricing_path, estimate_cost
from tokenburn.util.hashing import event_id


def test_extract_model_usage_stats_from_nested_response_payload():
    # ModelUsageStats fields: input_tokens=2, output_tokens=3,
    # cache_write_tokens=4, cache_read_tokens=5.
    usage = (
        _encode_varint(1 << 3)
        + _encode_varint(71)
        + _encode_varint(2 << 3)
        + _encode_varint(1200)
        + _encode_varint(3 << 3)
        + _encode_varint(300)
        + _encode_varint(4 << 3)
        + _encode_varint(40)
        + _encode_varint(5 << 3)
        + _encode_varint(60)
    )
    # Wrap the usage message in a response-like field 7.
    payload = _encode_varint(7 << 3 | 2) + _encode_varint(len(usage)) + usage

    result = _extract_model_usage_stats(payload)

    assert result == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_creation_tokens": 40,
        "cache_read_tokens": 60,
        "reasoning_tokens": 0,
        "total_tokens": 1600,
        "model_enum": 71,
    }


def test_extract_model_usage_stats_returns_none_for_unrelated_payload():
    assert _extract_model_usage_stats(b"\x18\x01\x22\x03foo") is None


def _field(number: int, value: bytes) -> bytes:
    return _encode_varint(number << 3 | 2) + _encode_varint(len(value)) + value


def _entry(key: str, value: str) -> bytes:
    body = (
        _encode_varint(1 << 3 | 2)
        + _encode_varint(len(key))
        + key.encode()
        + _encode_varint(2 << 3 | 2)
        + _encode_varint(len(value))
        + value.encode()
    )
    return _field(20, body)


def _length_prefixed(name: str) -> bytes:
    return _field(19, name.encode())


def _record(*fields: bytes) -> bytes:
    return _field(1, b"".join(fields))


def test_model_from_blob_reads_selected_vendor_id_not_other_routing_fields():
    # Field 19 is the selected model. Other fields can contain Antigravity's
    # internal routing labels and must not override it.
    blob = _record(
        _length_prefixed("claude-opus-4-6-thinking"),
        _field(28, b"gemini-pro-agent"),
        _entry("model_enum", "MODEL_PLACEHOLDER_M26"),
    )

    assert _model_from_blob(blob) == "claude-opus-4-6-thinking"


def test_model_from_blob_falls_back_to_routing_label():
    blob = _record(
        _length_prefixed("gemini-pro-default"),
        _field(28, b"gemini-pro-agent"),
        _entry("model_enum", "MODEL_PLACEHOLDER_M16"),
    )

    assert _model_from_blob(blob) == "gemini-pro-default"


def test_model_from_blob_ignores_transcript_text():
    """gen_metadata stores the conversation next to the metadata map.

    A loose regex over this blob reports fragments of the user's own source as
    model names — `gemini_model` here is a Python attribute, not a model.
    """
    transcript = _field(
        30,
        b"self._gemini_client = None\n"
        b"GEMINI_API_KEY = os.environ['GEMINI_API_KEY']\n"
        b"def gemini_model(self):\n",
    )
    blob = _record(transcript, _entry("model_enum", "MODEL_PLACEHOLDER_M16"))

    assert _model_from_blob(blob) == "antigravity-placeholder-m16"


def test_model_from_blob_rejects_length_like_transcript_prefix():
    # A loose length-prefix regex treats the leading space (ASCII 32) as the
    # declared length and surfaces this transcript-only token as a model id.
    transcript_model = b"gpt-5.6-sol-abcdefghijklmnopqrst"
    assert len(transcript_model) == 32
    blob = _record(
        _field(30, b" " + transcript_model),
        _entry("model_enum", "MODEL_PLACEHOLDER_M16"),
    )

    assert _model_from_blob(blob) == "antigravity-placeholder-m16"


def test_model_from_blob_uses_dominant_model_after_conversation_switch():
    blob = (
        _record(_length_prefixed("claude-opus-4-6-thinking"))
        + _record(_length_prefixed("gemini-pro-default"))
        + _record(_length_prefixed("gemini-pro-default"))
    )

    assert _model_from_blob(blob) == "gemini-pro-default"


def test_model_from_blob_returns_none_without_any_signal():
    assert _model_from_blob(b"no model information here") is None


# --- end-to-end parsing of an Antigravity app dir ---------------------------


def _int(number: int, value: int) -> bytes:
    return _encode_varint(number << 3) + _encode_varint(value)


def _ts(dt: datetime) -> bytes:
    return _int(1, int(dt.timestamp()))


def _usage(
    inp: int, out: int, cache_read: int, *, enum: int = 16, cache_write: int = 0, thinking: int = 0
):
    body = _int(1, enum) + _int(2, inp) + _int(3, out) + _int(5, cache_read)
    if cache_write:
        body += _int(4, cache_write)
    if thinking:
        body += _int(9, thinking)
    return body


def _step_payload(usage: bytes) -> bytes:
    # Usage sits a couple of private wrapper messages deep in the response.
    return _field(3, _field(7, usage) + _field(2, b"response text"))


def _gen(
    usage: bytes, *, model: str | None, display: str | None, enum: str, last_step: int, ts=None
):
    fields = _field(4, usage)
    if ts is not None:
        fields += _field(9, _field(4, _ts(ts)))
    if model is not None:
        fields += _length_prefixed(model)
    fields += _entry("model_enum", enum) + _entry("last_step_index", str(last_step))
    if display is not None:
        fields += _field(21, display.encode())
    return _record(fields)


def _write_conversation(
    root: Path, cid: str, steps: dict[int, tuple[datetime, bytes | None]], gens: list[bytes]
):
    conv = root / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(conv / f"{cid}.db")
    db.execute("CREATE TABLE trajectory_meta (trajectory_id text, cascade_id text)")
    db.execute("INSERT INTO trajectory_meta VALUES (?, ?)", (f"traj-{cid}", cid))
    db.execute("CREATE TABLE steps (idx integer PRIMARY KEY, metadata blob, step_payload blob)")
    for idx, (ts, usage) in steps.items():
        payload = _step_payload(usage) if usage is not None else _field(2, b"user turn")
        db.execute("INSERT INTO steps VALUES (?, ?, ?)", (idx, _field(1, _ts(ts)), payload))
    db.execute("CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob)")
    for idx, blob in enumerate(gens):
        db.execute("INSERT INTO gen_metadata VALUES (?, ?)", (idx, blob))
    db.commit()
    db.close()


T0 = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
PRO = _usage(3000, 900, 20000, thinking=600)
OPUS = _usage(4000, 2881, 34529, enum=26, cache_write=1500)
LATE_PRO = _usage(10, 5, 0)  # newer build: no display name on the generation
LOST = _usage(700, 70, 7000)  # generation whose response never reached a step
FAILED = _int(1, 16)  # empty usage


@pytest.fixture
def agy_root(tmp_path: Path) -> Path:
    root = tmp_path / "antigravity-cli"
    # One conversation that switches models mid-way: Gemini is dominant, but
    # the Claude call must still be attributed (and priced) as Claude.
    _write_conversation(
        root,
        "conv-a",
        steps={
            0: (T0, None),
            1: (T0, PRO),
            2: (T0.replace(hour=11), None),
            3: (T0.replace(hour=11), OPUS),
            4: (T0.replace(hour=12), PRO),
        },
        gens=[
            _gen(
                PRO,
                model="gemini-pro-default",
                display="Gemini 3.1 Pro (High)",
                enum="MODEL_PLACEHOLDER_M16",
                last_step=0,
                ts=T0,
            ),
            _gen(
                OPUS,
                model="claude-opus-4-6-thinking",
                display="Claude Opus 4.6 (Thinking)",
                enum="MODEL_PLACEHOLDER_M26",
                last_step=2,
            ),
            _gen(
                PRO,
                model="gemini-pro-default",
                display="Gemini 3.1 Pro (High)",
                enum="MODEL_PLACEHOLDER_M16",
                last_step=3,
            ),
            _gen(
                LOST,
                model="gemini-pro-default",
                display=None,
                enum="MODEL_PLACEHOLDER_M16",
                last_step=4,
                ts=T0.replace(hour=13),
            ),
            _gen(
                FAILED,
                model=None,
                display=None,
                enum="MODEL_PLACEHOLDER_M26",
                last_step=4,
                ts=T0.replace(hour=13),
            ),
        ],
    )
    # A later conversation with no display names at all: M16 is resolved from
    # what conv-a recorded; an enum never seen with a display stays a label.
    _write_conversation(
        root,
        "conv-b",
        steps={
            0: (T0.replace(day=20), None),
            1: (T0.replace(day=20), LATE_PRO),
            2: (T0.replace(day=21), _usage(1, 1, 1, enum=99)),
        },
        gens=[
            _gen(
                LATE_PRO,
                model="gemini-pro-default",
                display=None,
                enum="MODEL_PLACEHOLDER_M16",
                last_step=0,
            ),
            _gen(
                _usage(1, 1, 1, enum=99),
                model="gemini-pro-default",
                display=None,
                enum="MODEL_PLACEHOLDER_M99",
                last_step=1,
            ),
        ],
    )
    summaries = sqlite3.connect(root / "conversation_summaries.db")
    summaries.execute(
        "CREATE TABLE conversation_summaries (conversation_id text, workspace_uris text)"
    )
    summaries.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?)",
        ("conv-a", json.dumps(["file:///Users/someone/My%20Project"])),
    )
    summaries.commit()
    summaries.close()
    return root


JULY = DateRange(start=date(2026, 7, 1), end=date(2026, 7, 31))


def _events(cfg, root: Path):
    adapter = GeminiAdapter(cfg, ProviderConfig(enabled=True, source="local_or_imported_logs"))
    src = DiscoveredSource(provider="gemini", path=root, kind="antigravity_sqlite_dir", exists=True)
    # Deterministic order: ids hash tmp_path, so never tie-break on them.
    return sorted(
        adapter.parse(src, JULY),
        key=lambda e: (
            e.timestamp_start,
            e.source_parser != "antigravity_sqlite_protobuf",
            e.conversation_id,
            e.input_tokens,
        ),
    )


def test_antigravity_attributes_each_call_to_its_own_model(tmp_app_config, agy_root):
    cfg, _ = tmp_app_config
    events = _events(cfg, agy_root)
    by_model = [(e.conversation_id, e.model, e.model_alias) for e in events]
    assert by_model == [
        ("conv-a", "gemini-3.1-pro", "gemini-pro-default"),
        ("conv-a", "claude-opus-4-6-thinking", None),
        ("conv-a", "gemini-3.1-pro", "gemini-pro-default"),
        ("conv-a", "gemini-3.1-pro", "gemini-pro-default"),  # generation-only call
        ("conv-b", "gemini-3.1-pro", "gemini-pro-default"),  # learned via model_enum
        ("conv-b", "gemini-pro-default", None),  # never resolvable: label kept
    ]


def test_antigravity_usage_fields_and_context(tmp_app_config, agy_root):
    cfg, _ = tmp_app_config
    pro, opus, *_ = _events(cfg, agy_root)
    assert (pro.input_tokens, pro.output_tokens, pro.cache_read_tokens) == (3000, 900, 20000)
    assert pro.reasoning_tokens == 600
    assert pro.total_tokens == 3000 + 900 + 20000
    assert pro.project_path == "/Users/someone/My Project"
    assert pro.session_id == "traj-conv-a"
    assert pro.tool == "antigravity_cli"
    assert opus.cache_creation_tokens == 1500
    assert opus.total_tokens == 4000 + 2881 + 1500 + 34529


def test_antigravity_step_event_ids_are_stable(tmp_app_config, agy_root):
    """Step events keep the id scheme earlier releases wrote, so upgrading
    re-scans update existing DB rows in place instead of double counting."""
    cfg, _ = tmp_app_config
    path = agy_root / "conversations" / "conv-a.db"
    ids = {e.id for e in _events(cfg, agy_root) if e.source_parser == "antigravity_sqlite_protobuf"}
    assert {event_id("gemini-antigravity", str(path), str(i)) for i in (1, 3, 4)} <= ids


def test_antigravity_does_not_double_count_generation_only_calls(tmp_app_config, agy_root):
    cfg, _ = tmp_app_config
    events = _events(cfg, agy_root)
    gen_only = [e for e in events if e.source_parser == "antigravity_gen_metadata"]
    assert [(e.input_tokens, e.output_tokens) for e in gen_only] == [(700, 70)]
    assert gen_only[0].timestamp_start == T0.replace(hour=13)


def test_antigravity_resolved_models_are_priced(tmp_app_config, agy_root):
    cfg, _ = tmp_app_config
    table = PricingTable.load(default_pricing_path())
    pro = _events(cfg, agy_root)[0]
    assert estimate_cost(pro, table) == pytest.approx((3000 * 2 + 900 * 12 + 20000 * 0.20) / 1e6)


def test_antigravity_ide_dir_is_discovered(tmp_app_config, tmp_path, agy_root):
    cfg, _ = tmp_app_config
    ide = tmp_path / "antigravity"
    (ide / "conversations").mkdir(parents=True)
    pcfg = ProviderConfig(
        enabled=True,
        source="local_or_imported_logs",
        import_dir=str(tmp_path / "imports"),
        paths=[str(agy_root), str(ide)],
    )
    kinds = {s.path: s.kind for s in GeminiAdapter(cfg, pcfg).discover()}
    assert kinds[agy_root.resolve()] == kinds[ide.resolve()] == "antigravity_sqlite_dir"


@pytest.mark.parametrize(
    ("model_id", "display", "enum_display", "expected"),
    [
        ("gemini-pro-default", "Gemini 3.1 Pro (High)", None, "gemini-3.1-pro"),
        ("gemini-pro-agent", None, "Gemini 3.1 Pro (High)", "gemini-3.1-pro"),
        (None, "Claude Opus 4.6 (Thinking)", None, "claude-opus-4-6"),
        (
            "claude-opus-4-6-thinking",
            "Claude Opus 4.6 (Thinking)",
            None,
            "claude-opus-4-6-thinking",
        ),
        ("gemini-pro-default", None, None, "gemini-pro-default"),
        (None, None, None, None),
    ],
)
def test_resolve_antigravity_model(model_id, display, enum_display, expected):
    assert resolve_antigravity_model(model_id, display, enum_display) == expected


def test_antigravity_generation_aggregating_several_calls_is_not_double_counted(
    tmp_app_config, tmp_path
):
    """A generation's usage is the sum of every response step in its window.

    Real data: gen (5182, 584, 32433) == step 7 (2406, 486, 16219) +
    step 9 (2776, 98, 16214). Both steps inherit the generation's model and
    the generation adds nothing on top.
    """
    cfg, _ = tmp_app_config
    root = tmp_path / "antigravity-cli"
    first, second = _usage(2406, 486, 16219), _usage(2776, 98, 16214)
    _write_conversation(
        root,
        "conv-agg",
        steps={0: (T0, None), 7: (T0, first), 8: (T0, None), 9: (T0, second)},
        gens=[
            _gen(
                _usage(5182, 584, 32433),
                model="claude-opus-4-6-thinking",
                display=None,
                enum="MODEL_PLACEHOLDER_M26",
                last_step=8,
                ts=T0,
            )
        ],
    )
    events = _events(cfg, root)
    assert [(e.input_tokens, e.model, e.source_parser) for e in events] == [
        (2406, "claude-opus-4-6-thinking", "antigravity_sqlite_protobuf"),
        (2776, "claude-opus-4-6-thinking", "antigravity_sqlite_protobuf"),
    ]
    assert sum(e.total_tokens for e in events) == 5182 + 584 + 32433


def test_antigravity_emits_only_the_unaccounted_residual(tmp_app_config, tmp_path):
    """When a generation exceeds its steps (a response step was lost), the
    missing part is emitted once so totals still match the generation."""
    cfg, _ = tmp_app_config
    root = tmp_path / "antigravity-cli"
    _write_conversation(
        root,
        "conv-res",
        steps={0: (T0, None), 1: (T0, _usage(2135, 225, 110013))},
        gens=[
            _gen(
                _usage(100948, 1209, 110013),
                model="gemini-pro-default",
                display="Gemini 3.1 Pro (High)",
                enum="MODEL_PLACEHOLDER_M16",
                last_step=0,
                ts=T0,
            )
        ],
    )
    events = _events(cfg, root)
    assert [
        (e.source_parser, e.input_tokens, e.output_tokens, e.cache_read_tokens) for e in events
    ] == [
        ("antigravity_sqlite_protobuf", 2135, 225, 110013),
        ("antigravity_gen_metadata", 100948 - 2135, 1209 - 225, 0),
    ]
    assert {e.model for e in events} == {"gemini-3.1-pro"}
