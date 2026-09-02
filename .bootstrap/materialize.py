#!/usr/bin/env python3
"""Materialize the checksum-verified source archive staged in .bootstrap/."""
from __future__ import annotations

import base64
import hashlib
import itertools
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

EXPECTED_SHA256 = "ba9ee5478720656c165e08de1af2cd94f026c291caaffe3b682db8290ea6efc2"
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / ".bootstrap"


def destination_for(name: str) -> Path:
    logical = PurePosixPath(name)
    if logical.is_absolute() or not logical.parts or ".." in logical.parts:
        raise RuntimeError(f"unsafe archive path: {name!r}")
    destination = (ROOT / Path(*logical.parts)).resolve()
    if destination != ROOT and ROOT not in destination.parents:
        raise RuntimeError(f"archive path escapes repository: {name!r}")
    return destination


def verified_payload(chunks: list[Path]) -> tuple[bytes, tuple[str, ...]]:
    # The initial transport committed its shorter tail chunk first. Recover the
    # intended order by checksum instead of trusting filenames or silently
    # accepting corrupt data. Five chunks means only 120 bounded candidates.
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
        except ValueError:
            continue
        if hashlib.sha256(payload).hexdigest() == EXPECTED_SHA256:
            return payload, order
    raise RuntimeError("no chunk ordering matches the staged archive checksum")


def main() -> None:
    chunks = sorted(BOOTSTRAP.glob("chunk-*.b64"))
    if not chunks:
        print("Bootstrap chunks are absent; source is already materialized.")
        return

    payload, order = verified_payload(chunks)
    archive_path = BOOTSTRAP / "source.tar.gz"
    archive_path.write_bytes(payload)
    extracted = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            destination = destination_for(member.name)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(f"unsupported archive entry: {member.name!r}")
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
