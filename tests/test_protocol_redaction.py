from __future__ import annotations

import pytest

from nightshift.protocol import compact_text, extract_json_dict, limit_like
from nightshift.redaction import contains_secret, redact, redact_value, secret_findings


def test_marker_json_wins() -> None:
    text = 'noise {"wrong": 1}\n<SOL_LINK_JSON>{"action":"done","summary":"ok"}</SOL_LINK_JSON>'
    assert extract_json_dict(text)["action"] == "done"


def test_marked_json_outweighs_later_unmarked_object() -> None:
    text = '<SOL_LINK_JSON>{"action":"done"}</SOL_LINK_JSON> trailing {"action":"dispatch"}'
    assert extract_json_dict(text)["action"] == "done"


def test_fenced_and_balanced_json() -> None:
    assert extract_json_dict('```json\n{"a":{"b":"}"}}\n```') == {"a": {"b": "}"}}
    assert extract_json_dict('prefix {"x": 1} suffix')["x"] == 1


def test_no_json_raises() -> None:
    with pytest.raises(ValueError):
        extract_json_dict("plain text")


def test_limit_detection() -> None:
    assert limit_like("Weekly limit reached")
    assert limit_like("gpt-reserve 10% left")
    assert not limit_like("all tests passed")


def test_compaction_retains_head_and_tail() -> None:
    text = "HEAD" + ("x" * 3000) + "TAIL"
    result = compact_text(text, 1000)
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert "compacted" in result


def test_secret_redaction_and_recursive_value() -> None:
    token = "ghp_123456789012345678901234567890123456"
    assert contains_secret(token)
    assert "GitHub token" in secret_findings(token)
    assert token not in redact(f"token={token}")
    nested = redact_value({"a": [token], "accessToken": "opaque-value"})
    assert nested["a"][0] == "***REDACTED***"
    assert nested["accessToken"] == "***REDACTED***"


def test_private_key_redaction() -> None:
    block = "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----"
    assert contains_secret(block)
    assert "BEGIN PRIVATE" not in redact(block)
