"""Populate Paper Linux external resources from a JSON manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable
from urllib.request import Request, urlopen

from dotenv import load_dotenv


class ResourceError(RuntimeError):
    pass


RESOURCE_ROOT_ENV = "PAPER_RESOURCES_DIR"


def load_environment(manifest_path: Path) -> None:
    """Load the repository-local .env without overriding the shell."""
    load_dotenv(
        dotenv_path=manifest_path.expanduser().resolve().parent / ".env",
        override=False,
    )


def resolve_root(manifest_path: Path, explicit_root: Path | None = None) -> Path:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    configured = os.environ.get(RESOURCE_ROOT_ENV)
    if not configured:
        raise ResourceError(
            f"{RESOURCE_ROOT_ENV} is not configured; set it in .env or pass --root PATH"
        )
    root = Path(configured).expanduser()
    if not root.is_absolute():
        root = manifest_path.expanduser().resolve().parent / root
    return root.resolve()


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
            else:
                if not resource.get("clone_url"):
                    raise ResourceError(f"{resource_id}: clone_url is required")
                remote_names: set[str] = set()
                for remote in resource.get("remotes", []):
                    name = remote.get("name")
                    if not name or name in remote_names:
                        raise ResourceError(f"{resource_id}: invalid or duplicate remote name")
                    remote_names.add(name)
                    fetch = remote.get("fetch")
                    if fetch is not None and (
                        not isinstance(fetch, list)
                        or not all(isinstance(refspec, str) and refspec for refspec in fetch)
                    ):
                        raise ResourceError(f"{resource_id}: remote fetch must be a list of refspecs")
                worktree_paths: set[str] = set()
                for worktree in resource.get("worktrees", []):
                    worktree_id = worktree.get("id")
                    if not worktree_id or not worktree.get("ref"):
                        raise ResourceError(f"{resource_id}: every worktree needs id and ref")
                    if worktree_id in ids:
                        raise ResourceError(f"duplicate resource id: {worktree_id}")
                    ids.add(worktree_id)
                    worktree_path = worktree.get("path")
                    if not isinstance(worktree_path, str):
                        raise ResourceError(f"{resource_id}: worktree path must be a string")
                    validate_relative_path(worktree_path, f"{resource_id}: worktree path")
                    if worktree_path in paths or worktree_path in worktree_paths:
                        raise ResourceError(f"duplicate resource path: {worktree_path}")
                    worktree_paths.add(worktree_path)
                paths.update(worktree_paths)


def run_git(arguments: Iterable[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as error:
        raise ResourceError("git is required to populate repository resources") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else f"exit status {error.returncode}"
        raise ResourceError(f"{' '.join(command)}: {detail}") from error
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def selected(resource: dict[str, Any], requested: set[str]) -> bool:
    return not requested or resource["id"] in requested


def selected_repository(resource: dict[str, Any], requested: set[str]) -> bool:
    return selected(resource, requested) or any(
        worktree["id"] in requested for worktree in resource.get("worktrees", [])
    )


def populate_document(resource: dict[str, Any], root: Path) -> str:
    destination = root / resource["path"]
    expected = resource["sha256"].lower()
    if destination.is_file() and sha256(destination) == expected:
        return f"ok       {resource['id']}"
    url = resource.get("url")
    if not url:
        hint = resource.get("source_page", "the source described in the manifest")
        return f"manual   {resource['id']} (place a matching file at {destination}; source: {hint})"
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


def remote_url(repository_path: Path, name: str) -> str | None:
    try:
        return run_git(
            ["--git-dir", str(repository_path), "remote", "get-url", name], capture=True
        )
    except ResourceError:
        return None


def populate_repository(resource: dict[str, Any], root: Path, requested: set[str]) -> list[str]:
    repository_path = root / resource["path"]
    output: list[str] = []
    if not repository_path.exists():
        repository_path.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--bare"]
        if resource.get("filter"):
            clone_args.append(f"--filter={resource['filter']}")
        clone_args.extend([resource["clone_url"], str(repository_path)])
        run_git(clone_args)
        output.append(f"cloned   {resource['id']}")
    elif not (repository_path / "HEAD").is_file():
        raise ResourceError(f"{repository_path} exists but is not a bare Git repository")

    declared_remotes = [{"name": "origin", "url": resource["clone_url"]}, *resource.get("remotes", [])]
    for remote in declared_remotes:
        current = remote_url(repository_path, remote["name"])
        if current is None:
            run_git(["--git-dir", str(repository_path), "remote", "add", remote["name"], remote["url"]])
        elif current != remote["url"]:
            run_git(["--git-dir", str(repository_path), "remote", "set-url", remote["name"], remote["url"]])
        fetch = remote.get("fetch")
        if fetch == []:
            continue
        fetch_arguments = ["--git-dir", str(repository_path), "fetch", "--prune"]
        if resource.get("filter"):
            fetch_arguments.append(f"--filter={resource['filter']}")
            run_git(
                [
                    "--git-dir", str(repository_path), "config",
                    f"remote.{remote['name']}.promisor", "true",
                ]
            )
            run_git(
                [
                    "--git-dir", str(repository_path), "config",
                    f"remote.{remote['name']}.partialclonefilter", resource["filter"],
                ]
            )
        fetch_arguments.append(remote["name"])
        if fetch is not None:
            fetch_arguments.extend(fetch)
        run_git(fetch_arguments)

    for worktree in resource.get("worktrees", []):
        if requested and resource["id"] not in requested and worktree["id"] not in requested:
            continue
        destination = root / worktree["path"]
        if destination.exists():
            try:
                common = run_git(["-C", str(destination), "rev-parse", "--git-common-dir"], capture=True)
            except ResourceError as error:
                raise ResourceError(f"{destination} exists but is not a Git worktree") from error
            if Path(common).resolve() != repository_path.resolve():
                raise ResourceError(f"{destination} belongs to a different Git repository")
            output.append(f"ok       {worktree['id']}")
            continue
        if worktree.get("manual"):
            try:
                run_git(
                    [
                        "--git-dir", str(repository_path), "rev-parse", "--verify",
                        f"{worktree['ref']}^{{commit}}",
                    ],
                    capture=True,
                )
            except ResourceError:
                hint = worktree.get("source_page", "the source described in the manifest")
                output.append(
                    f"manual   {worktree['id']} (create ref {worktree['ref']}; source: {hint})"
                )
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        arguments = ["--git-dir", str(repository_path), "worktree", "add"]
        branch = worktree.get("branch")
        if branch:
            arguments.extend(["-b", branch])
        else:
            arguments.append("--detach")
        arguments.extend([str(destination), worktree["ref"]])
        run_git(arguments)
        output.append(f"created  {worktree['id']}")
    if not output:
        output.append(f"ok       {resource['id']}")
    return output


def check_document(resource: dict[str, Any], root: Path) -> tuple[bool, str]:
    path = root / resource["path"]
    if not path.is_file():
        return False, f"missing  {resource['id']}"
    if sha256(path) != resource["sha256"].lower():
        return False, f"changed  {resource['id']}"
    return True, f"ok       {resource['id']}"


def check_repository(
    resource: dict[str, Any], root: Path, requested: set[str]
) -> list[tuple[bool, str]]:
    repository_path = root / resource["path"]
    if not (repository_path / "HEAD").is_file():
        return [(False, f"missing  {resource['id']}")]
    results: list[tuple[bool, str]] = [(True, f"ok       {resource['id']}")]
    for remote in [{"name": "origin", "url": resource["clone_url"]}, *resource.get("remotes", [])]:
        matches = remote_url(repository_path, remote["name"]) == remote["url"]
        results.append((matches, f"{'ok' if matches else 'changed':8} {resource['id']} remote {remote['name']}"))
    for worktree in resource.get("worktrees", []):
        if requested and resource["id"] not in requested and worktree["id"] not in requested:
            continue
        path = root / worktree["path"]
        try:
            common = run_git(["-C", str(path), "rev-parse", "--git-common-dir"], capture=True)
            matches = Path(common).resolve() == repository_path.resolve()
        except ResourceError:
            matches = False
        results.append((matches, f"{'ok' if matches else 'missing':8} {worktree['id']}"))
    return results


def print_catalog(manifest: dict[str, Any]) -> None:
    for heading, resources in (("Documents", manifest.get("documents", [])), ("Repositories", manifest.get("repositories", []))):
        print(f"{heading}:")
        for resource in resources:
            tags = ", ".join(resource.get("tags", []))
            suffix = f" [{tags}]" if tags else ""
            print(f"  {resource['id']}: {resource.get('description', '')}{suffix}")
            for worktree in resource.get("worktrees", []):
                print(f"    {worktree['id']}: {worktree['ref']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=Path("external-resources.json"))
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list catalogued resources")
    subparsers.add_parser("env", help="print the effective resource environment")
    subparsers.add_parser("path", help="print the configured resource directory")
    for command, help_text in (("populate", "fetch and prepare resources"), ("check", "check an existing resource directory")):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--root", type=Path, help="override the configured resource directory")
        subparser.add_argument("resources", nargs="*", metavar="ID", help="resource IDs (default: all)")
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        manifest = load_manifest(args.manifest)
        load_environment(args.manifest)
        if args.command == "list":
            print_catalog(manifest)
            return 0
        if args.command in ("env", "path"):
            root = resolve_root(args.manifest)
            if args.command == "env":
                print(f"{RESOURCE_ROOT_ENV}={root}")
            else:
                print(root)
            return 0
        requested = set(args.resources)
        known = {item["id"] for key in ("documents", "repositories") for item in manifest.get(key, [])}
        known.update(
            worktree["id"]
            for repository in manifest.get("repositories", [])
            for worktree in repository.get("worktrees", [])
        )
        unknown = requested - known
        if unknown:
            raise ResourceError(f"unknown resource ID(s): {', '.join(sorted(unknown))}")
        root = resolve_root(args.manifest, args.root)
        if args.command == "populate":
            root.mkdir(parents=True, exist_ok=True)
            for document in manifest.get("documents", []):
                if selected(document, requested):
                    print(populate_document(document, root))
            for repository in manifest.get("repositories", []):
                if selected_repository(repository, requested):
                    print("\n".join(populate_repository(repository, root, requested)))
            return 0
        success = True
        for document in manifest.get("documents", []):
            if selected(document, requested):
                ok, message = check_document(document, root)
                success &= ok
                print(message)
        for repository in manifest.get("repositories", []):
            if selected_repository(repository, requested):
                for ok, message in check_repository(repository, root, requested):
                    success &= ok
                    print(message)
        return 0 if success else 1
    except ResourceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
