from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .models import QuotaSnapshot, QuotaWindow
from .process import SUBPROCESS_STREAM_LIMIT, ProcessRunner
from .redaction import redact, redact_value


class CodexAppServerError(RuntimeError):
    pass


class GrokACPError(RuntimeError):
    pass


async def _drain_stderr(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    chunks: list[str] = []
    while True:
        line = await stream.readline()
        if not line:
            break
        chunks.append(line.decode("utf-8", errors="replace").rstrip())
    return redact("\n".join(chunks))


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.close()
            await proc.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


# ---------------------------------------------------------------------------
# Codex app-server: account quota and model capability discovery


async def read_codex_account(binary: str, cwd: Path, timeout: int = 30,
                             env: dict[str, str] | None = None) -> dict[str, Any]:
    if not binary:
        raise CodexAppServerError("Codex binary not found")
    variants = [
        [binary, "app-server", "--listen", "stdio://"],
        [binary, "app-server", "--stdio"],
    ]
    last_error = ""
    for command in variants:
        try:
            return await _read_codex_account_once(command, cwd, timeout, env)
        except Exception as exc:  # compatibility fallback for older/newer CLI surfaces
            last_error = redact(str(exc))
    raise CodexAppServerError(last_error or "Codex app-server failed")


async def _read_codex_account_once(command: list[str], cwd: Path, timeout: int,
                                   env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=SUBPROCESS_STREAM_LIMIT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))

    async def send(message: dict[str, Any]) -> None:
        # Codex app-server intentionally omits the JSON-RPC header on its JSONL wire.
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def receive_id(expected: int) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CodexAppServerError(f"Timeout waiting for app-server response {expected}")
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw:
                err = await stderr_task
                raise CodexAppServerError(err or "app-server closed stdout")
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if message.get("id") == expected:
                if message.get("error"):
                    raise CodexAppServerError(redact(json.dumps(message["error"], ensure_ascii=False)))
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    try:
        await send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "sol_link_nightshift",
                    "title": "Sol Link Nightshift",
                    "version": "0.2.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        })
        initialized = await receive_id(1)
        await send({"method": "initialized", "params": {}})

        # Read responses with exactly one stdout consumer. Multiple concurrent
        # StreamReader.readline() calls are invalid and used to make quota refresh
        # fail nondeterministically under real Codex notifications.
        await send({"method": "account/read", "id": 2})
        account = await receive_id(2)
        await send({"method": "account/rateLimits/read", "id": 3})
        limits = await receive_id(3)

        models: dict[str, Any] = {}
        try:
            await send({
                "method": "model/list",
                "id": 4,
                "params": {"limit": 200, "includeHidden": True},
            })
            models = await receive_id(4)
        except CodexAppServerError:
            # Model capability discovery is useful for the reasoning picker but
            # must never hide an otherwise valid quota snapshot.
            models = {}
        return {
            "initialize": initialized,
            "account": account,
            "limits": limits,
            "models": models,
            "codex_home": initialized.get("codexHome", ""),
        }
    finally:
        await _terminate_process(proc)
        await stderr_task


def _reasoning_value(option: Any) -> str:
    if isinstance(option, str):
        return option
    if not isinstance(option, dict):
        return ""
    for key in ("reasoningEffort", "value", "id", "effort"):
        value = option.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def codex_effort_options(payload: dict[str, Any], configured_model: str = "",
                         prefer_luna: bool = False) -> tuple[list[str], str]:
    """Return model-advertised reasoning options and the matched model id.

    The server is authoritative when it exposes a matching model. Callers retain their
    configured fallback menu when no catalog entry can be matched.
    """
    models_root = payload.get("models") or {}
    rows = models_root.get("data") if isinstance(models_root, dict) else None
    if not isinstance(rows, list):
        return [], ""

    def identifiers(row: dict[str, Any]) -> list[str]:
        return [str(row.get(key) or "") for key in ("id", "model", "displayName")]

    selected: dict[str, Any] | None = None
    if configured_model:
        needle = configured_model.casefold()
        selected = next(
            (row for row in rows if isinstance(row, dict)
             and any(value.casefold() == needle for value in identifiers(row) if value)),
            None,
        )
    if selected is None and prefer_luna:
        selected = next(
            (row for row in rows if isinstance(row, dict)
             and any("luna" in value.casefold() for value in identifiers(row) if value)),
            None,
        )
    if selected is None:
        selected = next(
            (row for row in rows if isinstance(row, dict) and row.get("isDefault")),
            None,
        )
    if selected is None:
        return [], ""
    raw_options = selected.get("supportedReasoningEfforts") or []
    options: list[str] = []
    for raw in raw_options:
        value = _reasoning_value(raw).strip().lower()
        if value and value not in options:
            options.append(value)
    model_id = str(selected.get("model") or selected.get("id") or "")
    return options, model_id


def _window_label(limit_id: str, limit_name: str, window: dict[str, Any], suffix: str) -> str:
    mins = window.get("windowDurationMins")
    if limit_name:
        base = limit_name
    elif "reserve" in limit_id.lower():
        base = "gpt-reserve"
    else:
        base = "Codex"
    if mins == 10080:
        window_name = "Weekly limit"
    elif mins == 300:
        window_name = "5-hour limit"
    elif mins:
        window_name = f"{mins}-minute limit"
    else:
        window_name = suffix.capitalize() + " limit"
    return f"{base} {window_name}".strip()


def _format_epoch(value: Any) -> tuple[int | None, str]:
    if not value:
        return None, ""
    try:
        epoch = int(value)
        return epoch, datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None, str(value)


def normalize_codex_quota(agent_id: str, payload: dict[str, Any]) -> QuotaSnapshot:
    limits_result = payload.get("limits") or {}
    root = limits_result.get("rateLimits") or limits_result
    by_id = limits_result.get("rateLimitsByLimitId") or {}
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(by_id, dict) and by_id:
        for limit_id, value in by_id.items():
            if isinstance(value, dict):
                entries.append((str(limit_id), value))
    if isinstance(root, dict):
        root_id = str(root.get("limitId") or "codex")
        if not any(key == root_id for key, _ in entries):
            entries.insert(0, (root_id, root))

    windows: list[QuotaWindow] = []
    seen: set[tuple[str, str, int | None]] = set()
    plan_type = ""
    reached_type = None
    for limit_id, value in entries:
        plan_type = plan_type or str(value.get("planType") or "")
        reached_type = reached_type or value.get("rateLimitReachedType")
        name = str(value.get("limitName") or "")
        for suffix in ("primary", "secondary"):
            window = value.get(suffix)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            used_float = float(used) if used is not None else None
            resets_at, reset_text = _format_epoch(window.get("resetsAt"))
            key = (limit_id, suffix, resets_at)
            if key in seen:
                continue
            seen.add(key)
            windows.append(QuotaWindow(
                id=f"{limit_id}:{suffix}",
                label=_window_label(limit_id, name, window, suffix),
                used_percent=used_float,
                left_percent=max(0.0, 100.0 - used_float) if used_float is not None else None,
                window_minutes=int(window.get("windowDurationMins"))
                if window.get("windowDurationMins") else None,
                resets_at=resets_at,
                resets_at_text=reset_text,
                source="codex app-server",
            ))
    account = payload.get("account") or {}
    return QuotaSnapshot(
        agent_id=agent_id,
        available=bool(windows),
        account=redact_value(account) if isinstance(account, dict) else {},
        windows=windows,
        plan_type=plan_type,
        reached_type=reached_type,
        message="" if windows else "No quota windows returned by Codex app-server",
        raw={
            "codex_home": payload.get("codex_home", ""),
            "model_count": len((payload.get("models") or {}).get("data") or []),
        },
    )


# ---------------------------------------------------------------------------
# Grok ACP: subscription credits / billing


async def read_grok_billing(binary: str, cwd: Path, timeout: int = 30,
                            env: dict[str, str] | None = None) -> dict[str, Any]:
    if not binary:
        raise GrokACPError("Grok Build binary not found")
    variants = [
        [binary, "--no-auto-update", "agent", "stdio"],
        [binary, "agent", "stdio"],
        [binary, "agent", "--no-leader", "stdio"],
    ]
    last_error = ""
    for command in variants:
        try:
            return await _read_grok_billing_once(command, cwd, timeout, env)
        except Exception as exc:
            last_error = redact(str(exc))
    raise GrokACPError(last_error or "Grok ACP billing request failed")


async def _read_grok_billing_once(command: list[str], cwd: Path, timeout: int,
                                  env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=SUBPROCESS_STREAM_LIMIT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))

    async def send(request_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def receive(expected: int, request_timeout: int | None = None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + (request_timeout or timeout)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise GrokACPError(f"Timeout waiting for Grok ACP response {expected}")
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw:
                err = await stderr_task
                raise GrokACPError(err or "Grok ACP closed stdout")
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if message.get("id") != expected:
                continue
            if message.get("error"):
                error = message["error"]
                raise GrokACPError(redact(json.dumps(error, ensure_ascii=False)))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def billing(request_id: int, method: str) -> dict[str, Any]:
        await send(request_id, method, {})
        return await receive(request_id)

    async def billing_any(start_id: int) -> tuple[dict[str, Any], str, int]:
        errors: list[str] = []
        request_id = start_id
        # The extension is not part of the public ACP core. Grok Build releases
        # have exposed both spellings, so negotiate instead of pinning one.
        for method in ("x.ai/billing", "_x.ai/billing"):
            try:
                return await billing(request_id, method), method, request_id + 1
            except GrokACPError as exc:
                errors.append(f"{method}: {exc}")
                request_id += 1
        raise GrokACPError("; ".join(errors))

    try:
        await send(1, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {
                "name": "sol_link_nightshift",
                "title": "Sol Link Nightshift",
                "version": "0.2.0",
            },
        })
        initialized = await receive(1)
        try:
            data, billing_method, next_id = await billing_any(2)
            return {
                "initialize": initialized,
                "billing": data,
                "billing_method": billing_method,
            }
        except GrokACPError as first_error:
            # Valid consumer logins normally advertise `cached_token`. Authenticate with
            # that local credential reference, never by extracting or printing a token.
            methods = initialized.get("authMethods") or []
            ids = [str(item.get("id")) for item in methods if isinstance(item, dict) and item.get("id")]
            meta = initialized.get("_meta") if isinstance(initialized.get("_meta"), dict) else {}
            preferred = str(meta.get("defaultAuthMethodId") or "")
            method_id = preferred if preferred in ids else ("cached_token" if "cached_token" in ids else "")
            if not method_id:
                raise first_error
            # Unauthenticated negotiation used request ids 2 and 3. Keep ids monotonic
            # because some ACP implementations reject reuse on a live connection.
            await send(4, "authenticate", {"methodId": method_id, "_meta": {"headless": True}})
            await receive(4, request_timeout=min(timeout, 15))
            data, billing_method, _ = await billing_any(5)
            return {
                "initialize": initialized,
                "billing": data,
                "auth_method": method_id,
                "billing_method": billing_method,
            }
    finally:
        await _terminate_process(proc)
        await stderr_task


def _parse_rfc3339_epoch(value: Any) -> tuple[int | None, str]:
    if not isinstance(value, str) or not value:
        return None, ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        epoch = int(parsed.timestamp())
        return epoch, parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return None, value


def _cent_value(value: Any) -> int | None:
    if isinstance(value, dict):
        raw = value.get("val", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def normalize_grok_quota(agent_id: str, payload: dict[str, Any]) -> QuotaSnapshot:
    billing = payload.get("billing") if isinstance(payload.get("billing"), dict) else payload
    billing_method = str(payload.get("billing_method") or "x.ai/billing")
    config = billing.get("config") if isinstance(billing, dict) else None
    config = config if isinstance(config, dict) else {}
    windows: list[QuotaWindow] = []

    used_raw = config.get("creditUsagePercent")
    used: float | None = None
    if used_raw is not None:
        try:
            used = max(0.0, min(100.0, float(used_raw)))
        except (TypeError, ValueError):
            used = None
    if used is None:
        monthly_limit = _cent_value(config.get("monthlyLimit"))
        legacy_used = _cent_value(config.get("used"))
        if monthly_limit and legacy_used is not None:
            used = max(0.0, min(100.0, legacy_used * 100.0 / monthly_limit))

    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    period_type = str(period.get("type") or "")
    if "WEEKLY" in period_type.upper():
        label = "Grok weekly credits"
        minutes = 10080
    elif "MONTHLY" in period_type.upper():
        label = "Grok monthly credits"
        minutes = None
    else:
        label = "Grok included credits"
        minutes = None
    reset_epoch, reset_text = _parse_rfc3339_epoch(
        period.get("end") or config.get("billingPeriodEnd")
    )
    if used is not None:
        windows.append(QuotaWindow(
            id="grok:included",
            label=label,
            used_percent=used,
            left_percent=max(0.0, 100.0 - used),
            window_minutes=minutes,
            resets_at=reset_epoch,
            resets_at_text=reset_text,
            source=f"grok ACP {billing_method}",
        ))

    on_demand_cap = _cent_value(config.get("onDemandCap"))
    on_demand_used = _cent_value(config.get("onDemandUsed"))
    if on_demand_cap and on_demand_used is not None:
        od_used = max(0.0, min(100.0, on_demand_used * 100.0 / on_demand_cap))
        windows.append(QuotaWindow(
            id="grok:on-demand",
            label="Grok on-demand cap",
            used_percent=od_used,
            left_percent=max(0.0, 100.0 - od_used),
            resets_at=reset_epoch,
            resets_at_text=reset_text,
            source=f"grok ACP {billing_method}",
        ))

    tier = str(billing.get("subscriptionTier") or "") if isinstance(billing, dict) else ""
    account = {
        "subscription_tier": tier,
        "on_demand_enabled": billing.get("onDemandEnabled") if isinstance(billing, dict) else None,
        "unified_billing": config.get("isUnifiedBillingUser"),
        "prepaid_balance_cents": _cent_value(config.get("prepaidBalance")),
        "period_type": period_type,
    }
    return QuotaSnapshot(
        agent_id=agent_id,
        available=bool(windows),
        account=account,
        windows=windows,
        plan_type=tier,
        message="" if windows else "Grok billing responded, but no percentage limit was available",
        raw={"source": billing_method, "auth_method": payload.get("auth_method", "")},
    )


# ---------------------------------------------------------------------------
# Last-resort configurable text quota command


_GROK_BAR_RE = re.compile(
    r"(?P<label>[^\n:]{2,80}(?:limit|usage)[^\n:]*)\s*:\s*(?:\[[^\]]+\]\s*)?"
    r"(?P<pct>\d+(?:\.\d+)?)%\s*(?P<direction>left|used)?"
    r"(?:\s*\(?(?:resets?|reset)\s*(?P<reset>[^\n\)]+)\)?)?",
    re.I,
)


def parse_text_quota(agent_id: str, text: str, source: str) -> QuotaSnapshot:
    windows: list[QuotaWindow] = []
    for index, match in enumerate(_GROK_BAR_RE.finditer(redact(text))):
        pct = float(match.group("pct"))
        direction = (match.group("direction") or "left").lower()
        used = pct if direction == "used" else 100.0 - pct
        left = pct if direction == "left" else 100.0 - pct
        windows.append(QuotaWindow(
            id=f"text:{index}",
            label=match.group("label").strip(),
            used_percent=max(0.0, min(100.0, used)),
            left_percent=max(0.0, min(100.0, left)),
            resets_at_text=(match.group("reset") or "").strip(),
            source=source,
        ))
    return QuotaSnapshot(
        agent_id=agent_id,
        available=bool(windows),
        windows=windows,
        message="" if windows else "No structured subscription quota command is configured",
    )


async def read_configured_quota(config: AgentConfig, runner: ProcessRunner,
                                cwd: Path) -> QuotaSnapshot:
    if not config.quota_command:
        return QuotaSnapshot(
            agent_id=config.id,
            available=False,
            message="This CLI exposes no configured fallback quota command. "
                    "Nightshift will still detect live limit errors.",
        )
    binary = config.resolve_binary()
    command = [part.replace("{binary}", binary).replace("{cwd}", str(cwd))
               for part in config.quota_command]
    lines: list[str] = []

    async def sink(_stream: str, line: str) -> None:
        lines.append(redact(line))

    result = await runner.run(
        f"quota:{config.id}", command, cwd, timeout=60,
        env=config.subprocess_env(), on_line=sink,
    )
    text = "\n".join(lines)
    snapshot = parse_text_quota(config.id, text, "configured CLI command")
    if result.returncode != 0 and not snapshot.available:
        snapshot.message = redact((result.stderr or result.stdout or "quota command failed")[-1000:])
    return snapshot
