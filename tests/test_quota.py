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


def test_codex_luna_reserve_metadata() -> None:
    payload = {
        "luna_reserve_requested": True,
        "limits": {
            "ordinaryUsageAllowed": False,
            "rateLimits": {
                "limitId": "codex",
                "rateLimitReachedType": "rate_limit_reached",
                "primary": {"usedPercent": 100, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "base_model_inference": {
                    "limitId": "base_model_inference",
                    "limitName": "gpt-reserve",
                    "normalModelSlug": "gpt-5.6-luna",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 60, "windowDurationMins": 10080},
                },
            },
            "rateLimitUpsell": {
                "banner_type": "luna_reserve",
                "blocked_model_slug": "gpt-5.6-luna",
            },
        },
        "models": {"data": []},
    }
    snapshot = normalize_codex_quota("luna", payload)
    assert snapshot.raw["ordinary_usage_allowed"] is False
    assert snapshot.raw["luna_reserve_requested"] is True
    assert snapshot.raw["luna_reserve_exposed"] is True
    assert snapshot.raw["luna_reserve_available"] is True
    assert snapshot.raw["luna_reserve_model"] == "gpt-reserve"
    assert snapshot.raw["luna_reserve_normal_model"] == "gpt-5.6-luna"
    assert snapshot.raw["luna_reserve_limit_ids"] == ["base_model_inference"]
    assert snapshot.raw["luna_reserve_blocked_model"] == "gpt-5.6-luna"
    assert any(window.label.startswith("gpt-reserve") for window in snapshot.windows)


def test_codex_luna_reserve_is_not_inferred_from_percentages() -> None:
    payload = {
        "luna_reserve_requested": True,
        "limits": {
            "ordinaryUsageAllowed": None,
            "rateLimits": {
                "limitId": "codex",
                "rateLimitReachedType": "rate_limit_reached",
                "primary": {"usedPercent": 100, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "base_model_inference": {
                    "limitId": "base_model_inference",
                    "limitName": "gpt-reserve",
                    "primary": {"usedPercent": 0, "windowDurationMins": 10080},
                },
            },
        },
    }
    snapshot = normalize_codex_quota("luna", payload)
    assert snapshot.raw["luna_reserve_exposed"] is True
    assert snapshot.raw["luna_reserve_available"] is False


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
