"""Loading and validation for Paper Resources manifests."""

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
    if manifest.get("version") != 2:
        raise ResourceError("manifest version must be 2")
    for key in ("documents", "patches", "repositories"):
        if not isinstance(manifest.get(key, []), list):
            raise ResourceError(f"manifest field {key!r} must be a list")
    validate_manifest_v2(manifest)
    return manifest


def validate_relative_path(value: str, context: str) -> None:
    path = Path(value)
    if path.is_absolute() or not value or ".." in path.parts:
        raise ResourceError(f"{context} must be a non-empty relative path: {value!r}")


def validate_sha256(value: Any, context: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ResourceError(f"{context}: sha256 must contain 64 hex digits")
    try:
        int(value, 16)
    except ValueError as error:
        raise ResourceError(f"{context}: invalid sha256") from error


def validate_object_id(value: Any, context: str) -> None:
    if not isinstance(value, str) or len(value) != 40:
        raise ResourceError(f"{context} must contain a full 40-digit Git object ID")
    try:
        int(value, 16)
    except ValueError as error:
        raise ResourceError(f"{context} contains an invalid Git object ID") from error


def validate_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResourceError(f"{context} needs a non-empty id")
    if any(not (character.isalnum() or character in ".+_-") for character in value):
        raise ResourceError(f"{context} has an unsafe id: {value!r}")
    return value


def validate_manifest_v2(manifest: dict[str, Any]) -> None:
    paths: set[str] = set()
    global_ids: set[str] = set()

    for kind in ("documents", "patches"):
        for resource in manifest.get(kind, []):
            resource_id = validate_id(resource.get("id"), kind[:-1])
            if resource_id in global_ids:
                raise ResourceError(f"duplicate resource id: {resource_id}")
            global_ids.add(resource_id)
            path = resource.get("path")
            if not isinstance(path, str):
                raise ResourceError(f"{resource_id}: path must be a string")
            validate_relative_path(path, f"{resource_id}: path")
            if path in paths:
                raise ResourceError(f"duplicate resource path: {path}")
            paths.add(path)
            validate_sha256(resource.get("sha256"), resource_id)
            if not isinstance(resource.get("description"), str) or not resource["description"]:
                raise ResourceError(f"{resource_id}: description is required")
            if kind == "patches" and (
                not isinstance(resource.get("author"), str) or not resource["author"]
            ):
                raise ResourceError(f"{resource_id}: author is required")

    repository_ids: set[str] = set()
    for repository in manifest.get("repositories", []):
        repository_id = validate_id(repository.get("id"), "repository")
        if repository_id in repository_ids or repository_id in global_ids:
            raise ResourceError(f"duplicate resource id: {repository_id}")
        repository_ids.add(repository_id)
        global_ids.add(repository_id)
        path = repository.get("path")
        if not isinstance(path, str):
            raise ResourceError(f"{repository_id}: path must be a string")
        validate_relative_path(path, f"{repository_id}: path")
        if path in paths:
            raise ResourceError(f"duplicate resource path: {path}")
        paths.add(path)
        if not repository.get("clone_url"):
            raise ResourceError(f"{repository_id}: clone_url is required")
        remote_names = {"origin"}
        for remote in repository.get("remotes", []):
            name = remote.get("name")
            if not isinstance(name, str) or not name or name in remote_names:
                raise ResourceError(f"{repository_id}: invalid or duplicate remote name")
            if not remote.get("url"):
                raise ResourceError(f"{repository_id}: remote {name} needs a URL")
            remote_names.add(name)

        revisions = repository.get("revisions", [])
        if not isinstance(revisions, list):
            raise ResourceError(f"{repository_id}: revisions must be a list")
        revision_ids: set[str] = set()
        for revision in revisions:
            revision_id = validate_id(revision.get("id"), f"{repository_id}: revision")
            if revision_id in revision_ids:
                raise ResourceError(f"{repository_id}: duplicate revision id: {revision_id}")
            revision_ids.add(revision_id)
            for field in ("description", "author"):
                if not isinstance(revision.get(field), str) or not revision[field]:
                    raise ResourceError(f"{repository_id}:{revision_id}: {field} is required")
            validate_object_id(revision.get("commit"), f"{repository_id}:{revision_id}: commit")
            validate_object_id(revision.get("tree"), f"{repository_id}:{revision_id}: tree")
            source = revision.get("source")
            patches = revision.get("patches")
            if (source is None) == (patches is None):
                raise ResourceError(
                    f"{repository_id}:{revision_id}: exactly one of source or patches is required"
                )
            if source is not None:
                if not isinstance(source, dict) or source.get("remote") not in remote_names or not source.get("ref"):
                    raise ResourceError(f"{repository_id}:{revision_id}: invalid source")
            if patches is not None:
                if not revision.get("derived_from"):
                    raise ResourceError(f"{repository_id}:{revision_id}: patched revision needs derived_from")
                if not isinstance(patches, list) or not patches:
                    raise ResourceError(f"{repository_id}:{revision_id}: patches must be non-empty")
                used: set[str] = set()
                for application in patches:
                    patch_id = application if isinstance(application, str) else application.get("patch") if isinstance(application, dict) else None
                    if patch_id not in {patch["id"] for patch in manifest.get("patches", [])}:
                        raise ResourceError(f"{repository_id}:{revision_id}: unknown patch {patch_id!r}")
                    if patch_id in used:
                        raise ResourceError(f"{repository_id}:{revision_id}: duplicate patch {patch_id}")
                    used.add(patch_id)
                    if isinstance(application, dict) and (
                        not isinstance(application.get("strip", 1), int) or application.get("strip", 1) < 0
                    ):
                        raise ResourceError(f"{repository_id}:{revision_id}: invalid patch strip level")
            reference_base = revision.get("reference_base")
            if reference_base is not None and (
                not isinstance(reference_base, dict)
                or not reference_base.get("revision")
                or not reference_base.get("reason")
            ):
                raise ResourceError(f"{repository_id}:{revision_id}: invalid reference_base")
            worktree_ids: set[str] = set()
            for worktree in revision.get("worktrees", []):
                worktree_id = validate_id(worktree.get("id"), f"{repository_id}:{revision_id}: worktree")
                if worktree_id in worktree_ids:
                    raise ResourceError(f"{repository_id}:{revision_id}: duplicate worktree id: {worktree_id}")
                worktree_ids.add(worktree_id)
                worktree_path = worktree.get("path")
                if not isinstance(worktree_path, str):
                    raise ResourceError(f"{repository_id}:{revision_id}:{worktree_id}: path must be a string")
                validate_relative_path(worktree_path, f"{repository_id}:{revision_id}:{worktree_id}: path")
                if worktree_path in paths:
                    raise ResourceError(f"duplicate resource path: {worktree_path}")
                paths.add(worktree_path)

        for revision in revisions:
            revision_id = revision["id"]
            for field in ("derived_from",):
                target = revision.get(field)
                if target is not None and target not in revision_ids:
                    raise ResourceError(f"{repository_id}:{revision_id}: unknown {field} revision {target}")
            reference_base = revision.get("reference_base")
            if reference_base is not None and reference_base["revision"] not in revision_ids:
                raise ResourceError(f"{repository_id}:{revision_id}: unknown reference base {reference_base['revision']}")

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {revision["id"]: revision for revision in revisions}
        def visit(revision_id: str) -> None:
            if revision_id in visiting:
                raise ResourceError(f"{repository_id}: derived_from cycle involving {revision_id}")
            if revision_id in visited:
                return
            visiting.add(revision_id)
            parent = by_id[revision_id].get("derived_from")
            if parent is not None:
                visit(parent)
            visiting.remove(revision_id)
            visited.add(revision_id)
        for revision_id in revision_ids:
            visit(revision_id)
