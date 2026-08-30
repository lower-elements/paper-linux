"""Git repository and worktree resource operations."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Iterable

from .config import ResourceError


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
            run_git(["--git-dir", str(repository_path), "config", f"remote.{remote['name']}.promisor", "true"])
            run_git(["--git-dir", str(repository_path), "config", f"remote.{remote['name']}.partialclonefilter", resource["filter"]])
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
                run_git(["--git-dir", str(repository_path), "rev-parse", "--verify", f"{worktree['ref']}^{{commit}}"], capture=True)
            except ResourceError:
                hint = worktree.get("source_page", "the source described in the manifest")
                output.append(f"manual   {worktree['id']} (create ref {worktree['ref']}; source: {hint})")
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


def check_repository(resource: dict[str, Any], root: Path, requested: set[str]) -> list[tuple[bool, str]]:
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
