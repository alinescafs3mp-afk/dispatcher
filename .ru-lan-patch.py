from __future__ import annotations

import base64
import bz2
from pathlib import Path


parts = sorted(Path(".").glob(".ru-lan-patch.[0-9][0-9]"))
if len(parts) != 6:
    raise RuntimeError(f"expected 6 patch chunks, found {len(parts)}")
payload = "".join(part.read_text(encoding="utf-8").strip() for part in parts)
source = bz2.decompress(base64.b64decode(payload)).decode("utf-8")

# The patch generator itself is Python source. Correct two over-escaped
# search literals before compiling it; the repository source stores one
# backslash in the rendered TOML template, not two.
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
for old, new, label in (
    (old_server, new_server, "server template"),
    (old_hosts, new_hosts, "allowed-hosts template"),
):
    if source.count(old) != 1:
        raise RuntimeError(
            f"unexpected {label} patch shape: found {source.count(old)} targets"
        )
    source = source.replace(old, new)

exec(compile(source, "<russian-lan-ui-patch>", "exec"))
