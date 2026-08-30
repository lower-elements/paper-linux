"""Loading and validation for version-one Paper Resources manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ResourceError


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResourceError(f"cannot read manifest {path}: {error}") from error
    if manifest.get("version") != 1:
        raise ResourceError("manifest version must be 1")
    for key in ("documents", "repositories"):
        if not isinstance(manifest.get(key, []), list):
            raise ResourceError(f"manifest field {key!r} must be a list")
    validate_manifest(manifest)
    return manifest


def validate_relative_path(value: str, context: str) -> None:
    path = Path(value)
    if path.is_absolute() or not value or ".." in path.parts:
        raise ResourceError(f"{context} must be a non-empty relative path: {value!r}")


def validate_manifest(manifest: dict[str, Any]) -> None:
    ids: set[str] = set()
    paths: set[str] = set()
    for kind in ("documents", "repositories"):
        for resource in manifest.get(kind, []):
            resource_id = resource.get("id")
            if not isinstance(resource_id, str) or not resource_id:
                raise ResourceError(f"every {kind[:-1]} needs a non-empty id")
            if resource_id in ids:
                raise ResourceError(f"duplicate resource id: {resource_id}")
            ids.add(resource_id)
            path = resource.get("path")
            if not isinstance(path, str):
                raise ResourceError(f"{resource_id}: path must be a string")
            validate_relative_path(path, f"{resource_id}: path")
            if path in paths:
                raise ResourceError(f"duplicate resource path: {path}")
            paths.add(path)
            if kind == "documents":
                checksum = resource.get("sha256")
                if not isinstance(checksum, str) or len(checksum) != 64:
                    raise ResourceError(f"{resource_id}: sha256 must contain 64 hex digits")
                try:
                    int(checksum, 16)
                except ValueError as error:
                    raise ResourceError(f"{resource_id}: invalid sha256") from error
                extractor = resource.get("extractor")
                if extractor is not None and (
                    not isinstance(extractor, str) or not extractor
                ):
                    raise ResourceError(
                        f"{resource_id}: extractor must be a non-empty string"
                    )
            else:
                if not resource.get("clone_url"):
                    raise ResourceError(f"{resource_id}: clone_url is required")
                remote_names: set[str] = set()
                for remote in resource.get("remotes", []):
                    name = remote.get("name")
                    if not name or name in remote_names:
                        raise ResourceError(
                            f"{resource_id}: invalid or duplicate remote name"
                        )
                    remote_names.add(name)
                    fetch = remote.get("fetch")
                    if fetch is not None and (
                        not isinstance(fetch, list)
                        or not all(
                            isinstance(refspec, str) and refspec for refspec in fetch
                        )
                    ):
                        raise ResourceError(
                            f"{resource_id}: remote fetch must be a list of refspecs"
                        )
                worktree_paths: set[str] = set()
                for worktree in resource.get("worktrees", []):
                    worktree_id = worktree.get("id")
                    if not worktree_id or not worktree.get("ref"):
                        raise ResourceError(
                            f"{resource_id}: every worktree needs id and ref"
                        )
                    if worktree_id in ids:
                        raise ResourceError(f"duplicate resource id: {worktree_id}")
                    ids.add(worktree_id)
                    worktree_path = worktree.get("path")
                    if not isinstance(worktree_path, str):
                        raise ResourceError(
                            f"{resource_id}: worktree path must be a string"
                        )
                    validate_relative_path(
                        worktree_path, f"{resource_id}: worktree path"
                    )
                    if worktree_path in paths or worktree_path in worktree_paths:
                        raise ResourceError(f"duplicate resource path: {worktree_path}")
                    worktree_paths.add(worktree_path)
                paths.update(worktree_paths)
