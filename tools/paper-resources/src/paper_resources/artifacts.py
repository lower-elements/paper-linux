"""Fetch and verify file-backed resources."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.request import Request, urlopen

from .config import ResourceError


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def populate_file(resource: dict[str, Any], root: Path) -> str:
    destination = root / resource["path"]
    expected = resource["sha256"].lower()
    if destination.is_file() and sha256(destination) == expected:
        return f"ok       {resource['id']}"
    url = resource.get("url")
    if not url:
        hint = resource.get("source_page", "the source described in the manifest")
        return (
            f"manual   {resource['id']} (place a matching file at {destination}; "
            f"source: {hint})"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Paper-Linux-resource-tool/1"})
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urlopen(request) as response:
                while block := response.read(1024 * 1024):
                    temporary.write(block)
        actual = sha256(temporary_path)
        if actual != expected:
            raise ResourceError(
                f"{resource['id']}: checksum mismatch (expected {expected}, got {actual})"
            )
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return f"fetched  {resource['id']}"


def check_file(resource: dict[str, Any], root: Path) -> tuple[bool, str]:
    path = root / resource["path"]
    if not path.is_file():
        return False, f"missing  {resource['id']}"
    if sha256(path) != resource["sha256"].lower():
        return False, f"changed  {resource['id']}"
    return True, f"ok       {resource['id']}"
