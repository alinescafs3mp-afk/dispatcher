#!/usr/bin/env python3
"""One-shot, checksum-verified source materializer for repository bootstrap."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

EXPECTED_SHA256 = "b8038ca8122d7a565cfe4dcfdd7557a7edc4be30368b9075c9913f084a6ed115"
ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".stage"
OLD_STAGE = ROOT / ".bootstrap"


def destination_for(name: str) -> Path:
    logical = PurePosixPath(name)
    if logical.is_absolute() or not logical.parts or ".." in logical.parts:
        raise RuntimeError(f"unsafe archive path: {name!r}")
    destination = (ROOT / Path(*logical.parts)).resolve()
    if destination != ROOT and ROOT not in destination.parents:
        raise RuntimeError(f"archive path escapes repository: {name!r}")
    return destination


def main() -> None:
    chunks = sorted(STAGE.glob("chunk-*.b64"))
    if not chunks:
        print("Stage is absent; source is already materialized.")
        return
    encoded = "".join(path.read_text(encoding="ascii").strip() for path in chunks)
    payload = base64.b64decode(encoded, validate=True)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != EXPECTED_SHA256:
        raise RuntimeError(f"archive checksum mismatch: {actual}")

    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
        members = archive.getmembers()
        for member in members:
            destination_for(member.name)
            if not (member.isdir() or member.isfile()):
                raise RuntimeError(f"unsupported archive entry: {member.name!r}")
        for member in members:
            destination = destination_for(member.name)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read archive entry: {member.name!r}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".nightshift-tmp")
            with temporary.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(temporary, member.mode & 0o777)
            temporary.replace(destination)
            extracted += 1

    shutil.rmtree(STAGE)
    if OLD_STAGE.exists():
        shutil.rmtree(OLD_STAGE)
    print(f"Materialized {extracted} files; sha256={actual}")


if __name__ == "__main__":
    main()
