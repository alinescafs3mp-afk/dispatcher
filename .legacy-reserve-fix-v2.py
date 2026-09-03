from __future__ import annotations

import ast
import base64
import zlib
from pathlib import Path


wrapper = Path(".legacy-reserve-fix.py").read_text(encoding="utf-8")
tree = ast.parse(wrapper)
encoded = next(
    node.value
    for node in ast.walk(tree)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
)
source = zlib.decompress(base64.b64decode(encoded)).decode("utf-8")
old = '''replace_once(
    quota_tests,
    \'''    payload = {
        "luna_reserve_requested": True,
        "limits": {
\''',
    \'''    payload = {
        "luna_reserve_request_intent": True,
        "luna_reserve_requested": True,
        "luna_reserve_capability_supported": True,
        "limits": {
\''',
)
'''
new = '''replace_once(
    quota_tests,
    \'''def test_codex_luna_reserve_metadata() -> None:
    payload = {
        "luna_reserve_requested": True,
        "limits": {
\''',
    \'''def test_codex_luna_reserve_metadata() -> None:
    payload = {
        "luna_reserve_request_intent": True,
        "luna_reserve_requested": True,
        "luna_reserve_capability_supported": True,
        "limits": {
\''',
)
'''
if source.count(old) != 1:
    raise RuntimeError(
        "legacy Reserve repair payload has an unexpected quota-test patch shape: "
        f"found {source.count(old)} targets"
    )
source = source.replace(old, new)
exec(compile(source, "<legacy-reserve-repair-v2>", "exec"))
