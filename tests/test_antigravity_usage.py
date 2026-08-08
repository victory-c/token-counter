from __future__ import annotations

from tokenburn.adapters.gemini import (
    _encode_varint,
    _extract_model_usage_stats,
    _model_from_blob,
)


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
