from __future__ import annotations

import base64
import bz2
from pathlib import Path


parts = sorted(Path(".").glob(".ru-lan-patch.[0-9][0-9]"))
if len(parts) != 6:
    raise RuntimeError(f"expected 6 patch chunks, found {len(parts)}")
payload = "".join(part.read_text(encoding="utf-8").strip() for part in parts)
source = bz2.decompress(base64.b64decode(payload))
exec(compile(source, "<russian-lan-ui-patch>", "exec"))
