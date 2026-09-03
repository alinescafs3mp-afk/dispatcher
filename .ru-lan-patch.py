from __future__ import annotations

import base64
import bz2
from pathlib import Path


parts = sorted(Path(".").glob(".ru-lan-patch.[0-9][0-9]"))
if len(parts) != 6:
    raise RuntimeError(f"expected 6 patch chunks, found {len(parts)}")
payload = "".join(part.read_text(encoding="utf-8").strip() for part in parts)
source = bz2.decompress(base64.b64decode(payload)).decode("utf-8")

# The patch generator itself is Python source. Correct over-escaped or
# ambiguous search literals before compiling it.
old_server = """    '[server]\\\\\\\\nhost = \\\\"127.0.0.1\\\\"\\\\\\\\nport = 8787',
    '[server]\\\\\\\\nhost = \\\\"0.0.0.0\\\\"\\\\\\\\nport = 8787',
"""
new_server = """    '[server]\\\\nhost = "127.0.0.1"\\\\nport = 8787',
    '[server]\\\\nhost = "0.0.0.0"\\\\nport = 8787',
"""
old_hosts = """    '# Exact additional Host names accepted by the dashboard (ports are optional).\\\\\\\\nallowed_hosts = []',
    '# Private LAN IP addresses are accepted automatically on a wildcard bind.\\\\\\\\n# Add exact local DNS names here when the dashboard is opened by hostname.\\\\\\\\nallowed_hosts = []',
"""
new_hosts = """    '# Exact additional Host names accepted by the dashboard (ports are optional).\\\\nallowed_hosts = []',
    '# Private LAN IP addresses are accepted automatically on a wildcard bind.\\\\n# Add exact local DNS names here when the dashboard is opened by hostname.\\\\nallowed_hosts = []',
"""
old_default_host_test = """replace_once(
    "tests/test_config.py",
    '    assert settings.project.operational_roots == ["~/.jericho"]\\n',
    '    assert settings.project.operational_roots == ["~/.jericho"]\\n'
    '    assert settings.server.host == "0.0.0.0"\\n',
)
"""
new_default_host_test = """replace_once(
    "tests/test_config.py",
    '    assert settings.agent("grok").unsafe_full_access is True\\n'
    '    assert settings.project.operational_roots == ["~/.jericho"]\\n',
    '    assert settings.agent("grok").unsafe_full_access is True\\n'
    '    assert settings.project.operational_roots == ["~/.jericho"]\\n'
    '    assert settings.server.host == "0.0.0.0"\\n',
)
"""

for old, new, label in (
    (old_server, new_server, "server template"),
    (old_hosts, new_hosts, "allowed-hosts template"),
    (old_default_host_test, new_default_host_test, "default-host regression"),
):
    if source.count(old) != 1:
        raise RuntimeError(
            f"unexpected {label} patch shape: found {source.count(old)} targets"
        )
    source = source.replace(old, new)

exec(compile(source, "<russian-lan-ui-patch>", "exec"))

main_path = Path("nightshift/__main__.py")
main_text = main_path.read_text(encoding="utf-8")
import_marker = "import asyncio\n"
if main_text.count(import_marker) != 1:
    raise RuntimeError("unexpected __main__.py import shape")
main_text = main_text.replace(import_marker, import_marker + "import contextlib\n", 1)
old_browser_fallback = """    try:
        webbrowser.open(url)
    except Exception:
        # Browser launch is a convenience and must never stop the server.
        pass
"""
new_browser_fallback = """    with contextlib.suppress(Exception):
        # Browser launch is a convenience and must never stop the server.
        webbrowser.open(url)
"""
if main_text.count(old_browser_fallback) != 1:
    raise RuntimeError("unexpected browser fallback shape")
main_path.write_text(
    main_text.replace(old_browser_fallback, new_browser_fallback, 1),
    encoding="utf-8",
)
