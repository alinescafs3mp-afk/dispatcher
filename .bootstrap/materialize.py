#!/usr/bin/env python3
"""Materialize the verified source archive staged in .bootstrap/.

This is a one-shot transport helper for the initial repository population.
It refuses path traversal, special files, malformed base64, and checksum drift.
"""
from __future__ import annotations

import base64
import hashlib
import io
import shutil
import tarfile
from pathlib import Path, PurePosixPath

EXPECTED_SHA256 = "ba9ee5478720656c165e08de1af2cd94f026c291caaffe3b682db8290ea6efc2"
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / ".bootstrap"


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RuntimeError(f"unsafe archive path: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise RuntimeError(f"unsupported archive entry: {member.name!r}")
        destination = (ROOT / Path(*path.parts)).resolve()
        if destination != ROOT and ROOT not in destination.parents:
            raise RuntimeError(f"archive path escapes repository: {member.name!r}")
    return members


def main() -> None:
    chunks = sorted(BOOTSTRAP.glob("chunk-*.b64"))
    if not chunks:
        print("No bootstrap chunks found; repository is already materialized.")
        return

    encoded = "".join(path.read_text(encoding="ascii").strip() for path in chunks)
    payload = base64.b64decode(encoded, validate=True)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != EXPECTED_SHA256:
        raise RuntimeError(f"archive checksum mismatch: {actual}")

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = _safe_members(archive)
        archive.extractall(ROOT, members=members, filter="data")

    shutil.rmtree(BOOTSTRAP)
    print(f"Materialized {len(members)} archive entries; sha256={actual}")


if __name__ == "__main__":
    main()
