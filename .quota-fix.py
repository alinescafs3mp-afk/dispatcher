from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one replacement target, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


quota = Path("nightshift/quota.py")
replace_once(
    quota,
    '        await send({"method": "account/read", "id": 2})\n',
    '        await send({"method": "account/read", "id": 2, "params": {}})\n',
)
replace_once(
    quota,
    '        await send({"method": "account/rateLimits/read", "id": 3})\n',
    '        await send({"method": "account/rateLimits/read", "id": 3, "params": {}})\n',
)
replace_once(
    quota,
    '''    if used is None:
        monthly_limit = _cent_value(config.get("monthlyLimit"))
        legacy_used = _cent_value(config.get("used"))
        if monthly_limit and legacy_used is not None:
            used = max(0.0, min(100.0, legacy_used * 100.0 / monthly_limit))

    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
''',
    '''    if used is None:
        monthly_limit = _cent_value(config.get("monthlyLimit"))
        legacy_used = _cent_value(config.get("used"))
        if monthly_limit and legacy_used is not None:
            used = max(0.0, min(100.0, legacy_used * 100.0 / monthly_limit))

    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    if used is None and period:
        # Grok's credits backend uses proto3 JSON. A scalar percentage of zero
        # may therefore be omitted while the current usage period is still
        # present. Match the official pager: this is an available 0% window,
        # not a failed billing read.
        used = 0.0
''',
)

jsonrpc_tests = Path("tests/test_jsonrpc_quota.py")
replace_once(
    jsonrpc_tests,
    '''            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "initialize":
''',
    '''            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") in {"account/read", "account/rateLimits/read"} and "params" not in msg:
                    print(json.dumps({"id": msg["id"], "error": {"code": -32600, "message": "Invalid request: missing field `params`"}}), flush=True)
                    continue
                if msg.get("method") == "initialize":
''',
)

quota_tests = Path("tests/test_quota.py")
replace_once(
    quota_tests,
    '''

def test_grok_legacy_billing() -> None:
''',
    '''

def test_grok_proto3_omitted_zero_percentage() -> None:
    payload = {
        "billing": {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-09-01T00:00:00Z",
                    "end": "2026-09-08T00:00:00Z",
                },
                "prepaidBalance": {},
                "isUnifiedBillingUser": True,
            }
        }
    }
    snapshot = normalize_grok_quota("grok", payload)
    assert snapshot.available
    assert snapshot.message == ""
    assert snapshot.windows[0].used_percent == 0
    assert snapshot.windows[0].left_percent == 100
    assert snapshot.windows[0].window_minutes == 10080
    assert snapshot.account["prepaid_balance_cents"] == 0


def test_grok_legacy_billing() -> None:
''',
)

print("quota protocol repair applied")
