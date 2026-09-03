from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nightshift.quota import read_codex_account, read_grok_billing


@pytest.mark.asyncio
async def test_codex_app_server_protocol(make_executable, tmp_path: Path) -> None:
    fake = make_executable(
        "fake-codex",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") in {"account/read", "account/rateLimits/read"} and "params" not in msg:
                    print(json.dumps({"id": msg["id"], "error": {"code": -32600, "message": "Invalid request: missing field `params`"}}), flush=True)
                    continue
                if msg.get("method") == "initialize":
                    print(json.dumps({"id": msg["id"], "result": {"codexHome": "/tmp/codex-home"}}), flush=True)
                elif msg.get("method") == "account/read":
                    print(json.dumps({"id": msg["id"], "result": {"account": {"type": "chatgpt"}}}), flush=True)
                elif msg.get("method") == "account/rateLimits/read":
                    print(json.dumps({"id": msg["id"], "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}}}}), flush=True)
                elif msg.get("method") == "model/list":
                    print(json.dumps({"id": msg["id"], "result": {"data": [{"id": "gpt-5.6-luna", "supportedReasoningEfforts": ["high", "max"]}]}}), flush=True)
            """
        ),
    )
    payload = await read_codex_account(str(fake), tmp_path, timeout=3, env={"PATH": str(Path(fake).parent) + ":/usr/bin"})
    assert payload["codex_home"] == "/tmp/codex-home"
    assert payload["limits"]["rateLimits"]["primary"]["usedPercent"] == 10
    assert payload["models"]["data"][0]["id"] == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_grok_acp_cached_token_protocol(make_executable, tmp_path: Path) -> None:
    fake = make_executable(
        "fake-grok",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            authed = False
            for line in sys.stdin:
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"authMethods":[{"id":"cached_token"}],"_meta":{"defaultAuthMethodId":"cached_token"}}}), flush=True)
                elif method == "authenticate":
                    authed = True
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
                elif method in {"x.ai/billing", "_x.ai/billing"} and not authed:
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32000,"message":"auth required"}}), flush=True)
                elif method == "x.ai/billing":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"config":{"creditUsagePercent":42,"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","end":"2026-09-09T00:00:00Z"}},"subscriptionTier":"Heavy"}}), flush=True)
                elif method == "_x.ai/billing":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32601,"message":"method not found"}}), flush=True)
            """
        ),
    )
    payload = await read_grok_billing(str(fake), tmp_path, timeout=3, env={"PATH": str(Path(fake).parent) + ":/usr/bin"})
    assert payload["auth_method"] == "cached_token"
    assert payload["billing_method"] == "x.ai/billing"
    assert payload["billing"]["config"]["creditUsagePercent"] == 42


@pytest.mark.asyncio
async def test_grok_acp_legacy_billing_method_fallback(make_executable, tmp_path: Path) -> None:
    fake = make_executable(
        "fake-grok-legacy",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
                elif method == "x.ai/billing":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32601,"message":"method not found"}}), flush=True)
                elif method == "_x.ai/billing":
                    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"config":{"creditUsagePercent":7}}}), flush=True)
            """
        ),
    )
    payload = await read_grok_billing(
        str(fake), tmp_path, timeout=3,
        env={"PATH": str(Path(fake).parent) + ":/usr/bin"},
    )
    assert payload["billing_method"] == "_x.ai/billing"
    assert payload["billing"]["config"]["creditUsagePercent"] == 7
