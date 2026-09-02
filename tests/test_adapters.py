from __future__ import annotations

from pathlib import Path

from nightshift.adapters.codex import CodexAdapter
from nightshift.adapters.grok import GrokAdapter, _first_text, _usage_from
from nightshift.config import default_settings
from nightshift.process import ProcessRunner


def test_codex_command_new_and_resume(tmp_path: Path) -> None:
    config = default_settings().agent("luna")
    config.binary_candidates = ["/bin/echo"]
    adapter = CodexAdapter(config, ProcessRunner())
    adapter.binary = "/bin/echo"
    command = adapter._command(tmp_path, tmp_path / "last", "abc-123", False, "--json")
    assert command[:3] == ["/bin/echo", "exec", "--json"]
    assert command[command.index("--sandbox"):command.index("--sandbox") + 2] == ["--sandbox", "workspace-write"]
    assert command[-3:] == ["resume", "abc-123", "-"]
    assert 'model_reasoning_effort="max"' in command


def test_codex_read_only_and_no_api_bypass(tmp_path: Path) -> None:
    config = default_settings().agent("spark")
    config.binary_candidates = ["/bin/echo"]
    adapter = CodexAdapter(config, ProcessRunner())
    adapter.binary = "/bin/echo"
    command = adapter._command(tmp_path, tmp_path / "last", None, True, "--experimental-json")
    assert "read-only" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_grok_read_only_command(tmp_path: Path) -> None:
    config = default_settings().agent("grok")
    config.binary_candidates = ["/bin/echo"]
    adapter = GrokAdapter(config, ProcessRunner())
    adapter.binary = "/bin/echo"
    command = adapter._command(tmp_path, "sid", False, "hello", True)
    assert "--permission-mode" not in command
    assert "--disallowed-tools" in command
    assert "--always-approve" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--effort" in command
    assert "xhigh" in command
    assert "--output-format" in command and "streaming-json" in command


def test_grok_write_command(tmp_path: Path) -> None:
    config = default_settings().agent("grok")
    config.binary_candidates = ["/bin/echo"]
    adapter = GrokAdapter(config, ProcessRunner())
    adapter.binary = "/bin/echo"
    command = adapter._command(tmp_path, "sid", True, "hello", False)
    assert "--always-approve" in command
    assert command[command.index("--sandbox") + 1] == "workspace"
    assert command[command.index("--resume") + 1] == "sid"


def test_grok_event_text_and_usage() -> None:
    obj = {"type": "assistant", "update": {"content": [{"text": "hello"}]}}
    assert _first_text(obj) == "hello"
    usage = _usage_from({
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
    })
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.cached_input_tokens == 60
    assert usage.output_tokens == 20
    assert usage.reasoning_tokens == 7
