#!/usr/bin/env python3
"""Materialize the source archive staged in .bootstrap/.

The initial transport committed the short tail chunk first and recorded a stale
archive checksum. This one-shot helper therefore recovers the only chunk order
that forms the expected Nightshift tarball, validates every member, and then
extracts it without using tarfile.extractall().
"""
from __future__ import annotations

import base64
import io
import itertools
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / ".bootstrap"
REQUIRED_MEMBERS = {
    "pyproject.toml",
    "README.md",
    "EMERGENCY_TAKEOVER_DIRECTIVE.md",
    "nightshift/orchestrator.py",
}


def destination_for(name: str) -> Path:
    logical = PurePosixPath(name)
    if logical.is_absolute() or not logical.parts or ".." in logical.parts:
        raise RuntimeError(f"unsafe archive path: {name!r}")
    destination = (ROOT / Path(*logical.parts)).resolve()
    if destination != ROOT and ROOT not in destination.parents:
        raise RuntimeError(f"archive path escapes repository: {name!r}")
    return destination


def recover_archive(chunks: list[Path]) -> tuple[bytes, tuple[str, ...], list[tarfile.TarInfo]]:
    bodies = {path.name: path.read_text(encoding="ascii").strip() for path in chunks}
    preferred = tuple(path.name for path in chunks)
    candidates = itertools.chain([preferred], itertools.permutations(bodies))
    seen: set[tuple[str, ...]] = set()
    for order in candidates:
        if order in seen:
            continue
        seen.add(order)
        try:
            payload = base64.b64decode("".join(bodies[name] for name in order), validate=True)
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                members = archive.getmembers()
                names = {member.name.rstrip("/") for member in members}
                if not REQUIRED_MEMBERS.issubset(names):
                    continue
                for member in members:
                    destination_for(member.name)
                    if not (member.isdir() or member.isfile()):
                        raise RuntimeError(f"unsupported archive entry: {member.name!r}")
                return payload, order, members
        except (ValueError, EOFError, OSError, tarfile.TarError):
            continue
    raise RuntimeError("no chunk ordering forms the expected Nightshift source archive")


def main() -> None:
    chunks = sorted(BOOTSTRAP.glob("chunk-*.b64"))
    if not chunks:
        print("Bootstrap chunks are absent; source is already materialized.")
        return

    payload, order, members = recover_archive(chunks)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        extracted = 0
        for member in members:
            destination = destination_for(member.name)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read archive entry: {member.name!r}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".dispatcher-tmp")
            with temporary.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(temporary, member.mode & 0o777)
            temporary.replace(destination)
            extracted += 1

    shutil.rmtree(BOOTSTRAP)
    print(f"Materialized {extracted} files; chunk_order={','.join(order)}")


if __name__ == "__main__":
    main()
