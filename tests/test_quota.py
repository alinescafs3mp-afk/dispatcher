from __future__ import annotations

from nightshift.quota import (
    codex_effort_options,
    normalize_codex_quota,
    normalize_grok_quota,
    parse_text_quota,
)


def test_codex_multi_bucket_quota() -> None:
    payload = {
        "account": {"planType": "pro"},
        "limits": {
            "rateLimits": {
                "limitId": "codex",
                "planType": "pro",
                "primary": {"usedPercent": 73, "windowDurationMins": 10080, "resetsAt": 1800000000},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {"usedPercent": 73, "windowDurationMins": 10080, "resetsAt": 1800000000},
                },
                "gpt-reserve": {
                    "limitId": "gpt-reserve",
                    "limitName": "gpt-reserve",
                    "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1800100000},
                },
            },
        },
        "models": {"data": []},
    }
    snapshot = normalize_codex_quota("luna", payload)
    assert snapshot.available
    assert len(snapshot.windows) == 2
    by_id = {window.id: window for window in snapshot.windows}
    assert by_id["codex:primary"].left_percent == 27
    assert by_id["gpt-reserve:secondary"].left_percent == 100
    assert "Weekly" in by_id["gpt-reserve:secondary"].label


def test_codex_effort_options_exact_model() -> None:
    payload = {
        "models": {
            "data": [
                {"id": "other", "supportedReasoningEfforts": ["low"]},
                {
                    "id": "gpt-5.6-luna",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"},
                        {"value": "max"},
                    ],
                },
            ]
        }
    }
    options, model = codex_effort_options(payload, "gpt-5.6-luna")
    assert options == ["high", "max"]
    assert model == "gpt-5.6-luna"


def test_codex_effort_options_luna_fallback() -> None:
    payload = {"models": {"data": [{"model": "preview-luna", "supportedReasoningEfforts": ["none", "xhigh"]}]}}
    options, model = codex_effort_options(payload, prefer_luna=True)
    assert options == ["none", "xhigh"]
    assert model == "preview-luna"


def test_grok_weekly_billing() -> None:
    payload = {
        "billing": {
            "subscriptionTier": "SuperGrok Heavy",
            "onDemandEnabled": True,
            "config": {
                "creditUsagePercent": 12.5,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-09-09T02:00:00Z",
                },
                "prepaidBalance": {"val": 500},
            },
        },
        "auth_method": "cached_token",
    }
    snapshot = normalize_grok_quota("grok", payload)
    assert snapshot.available
    assert snapshot.windows[0].left_percent == 87.5
    assert snapshot.windows[0].window_minutes == 10080
    assert snapshot.plan_type == "SuperGrok Heavy"
    assert snapshot.account["prepaid_balance_cents"] == 500


def test_grok_legacy_billing() -> None:
    payload = {"billing": {"config": {"monthlyLimit": {"val": 1000}, "used": {"val": 250}}}}
    snapshot = normalize_grok_quota("grok", payload)
    assert snapshot.windows[0].used_percent == 25
    assert snapshot.windows[0].left_percent == 75


def test_text_quota_parser() -> None:
    snapshot = parse_text_quota(
        "grok",
        "Weekly limit: [#####.....] 27% left (resets 05:28 on 7 Sep)",
        "test",
    )
    assert snapshot.available
    assert snapshot.windows[0].left_percent == 27
    assert "05:28" in snapshot.windows[0].resets_at_text
