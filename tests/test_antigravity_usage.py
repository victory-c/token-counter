from __future__ import annotations

from tokenburn.adapters.gemini import _extract_model_usage_stats, _encode_varint


def test_extract_model_usage_stats_from_nested_response_payload():
    # ModelUsageStats fields: input_tokens=2, output_tokens=3,
    # cache_write_tokens=4, cache_read_tokens=5.
    usage = (
        _encode_varint(1 << 3) + _encode_varint(71)
        + _encode_varint(2 << 3) + _encode_varint(1200)
        + _encode_varint(3 << 3) + _encode_varint(300)
        + _encode_varint(4 << 3) + _encode_varint(40)
        + _encode_varint(5 << 3) + _encode_varint(60)
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
