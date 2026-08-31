"""Git repository and worktree resource operations."""

from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
import os
import subprocess
import tempfile
from typing import Any, Iterable, Mapping

from .config import ResourceError


def run_git(
    arguments: Iterable[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    strip_output: bool = True,
    environment: Mapping[str, str] | None = None,
) -> str:
    command = ["git", *arguments]
    try:
        env = os.environ.copy()
        if environment is not None:
            env.update(environment)
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            env=env,
        )
    except FileNotFoundError as error:
        raise ResourceError("git is required to populate repository resources") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else f"exit status {error.returncode}"
        raise ResourceError(f"{' '.join(command)}: {detail}") from error
    if not capture:
        return ""
    return result.stdout.strip() if strip_output else result.stdout


def remote_url(repository_path: Path, name: str) -> str | None:
    try:
        return run_git(
            ["--git-dir", str(repository_path), "remote", "get-url", name], capture=True
        )
    except ResourceError:
        return None


def revision_ref(revision_id: str) -> str:
    return f"refs/paper-resources/revisions/{revision_id}"


def git_object(repository_path: Path, expression: str) -> str | None:
    try:
        return run_git(
            ["--git-dir", str(repository_path), "rev-parse", "--verify", expression],
            capture=True,
        )
    except ResourceError:
        return None


def configure_repository(repository: dict[str, Any], root: Path) -> tuple[Path, list[str]]:
    repository_path = root / repository["path"]
    output: list[str] = []
    if not repository_path.exists():
        repository_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(["init", "--bare", str(repository_path)])
        output.append(f"initialized {repository['id']}")
    bare = run_git(
        ["--git-dir", str(repository_path), "rev-parse", "--is-bare-repository"],
        capture=True,
    )
    if bare != "true":
        raise ResourceError(f"{repository_path} is not a bare Git repository")
    remotes = [
        {"name": "origin", "url": repository["clone_url"]},
        *repository.get("remotes", []),
    ]
    for remote in remotes:
        current = remote_url(repository_path, remote["name"])
        if current is None:
            run_git(
                [
                    "--git-dir", str(repository_path), "remote", "add",
                    remote["name"], remote["url"],
                ]
            )
        elif current != remote["url"]:
            run_git(
                [
                    "--git-dir", str(repository_path), "remote", "set-url",
                    remote["name"], remote["url"],
                ]
            )
    return repository_path, output


def verify_revision_objects(
    repository_path: Path, repository_id: str, revision: dict[str, Any], commit: str
) -> None:
    context = f"{repository_id}:{revision['id']}"
    if commit != revision["commit"]:
        raise ResourceError(
            f"{context}: commit mismatch (expected {revision['commit']}, got {commit})"
        )
    tree = git_object(repository_path, f"{commit}^{{tree}}")
    if tree != revision["tree"]:
        raise ResourceError(
            f"{context}: tree mismatch (expected {revision['tree']}, got {tree})"
        )


def fetch_revision(
    repository: dict[str, Any], repository_path: Path, revision: dict[str, Any]
) -> None:
    commit = git_object(repository_path, f"{revision['commit']}^{{commit}}")
    if commit is None:
        source = revision["source"]
        arguments = ["--git-dir", str(repository_path), "fetch"]
        if repository.get("filter"):
            arguments.append(f"--filter={repository['filter']}")
        arguments.extend([source["remote"], source["ref"]])
        run_git(arguments)
        commit = git_object(repository_path, "FETCH_HEAD^{commit}")
    if commit is None:
        raise ResourceError(f"{repository['id']}:{revision['id']}: fetched no commit")
    verify_revision_objects(repository_path, repository["id"], revision, commit)
    run_git(
        [
            "--git-dir", str(repository_path), "update-ref",
            revision_ref(revision["id"]), commit,
        ]
    )


def patch_application(application: str | dict[str, Any]) -> tuple[str, int]:
    if isinstance(application, str):
        return application, 1
    return application["patch"], application.get("strip", 1)


def construct_revision(
    repository: dict[str, Any],
    repository_path: Path,
    revision: dict[str, Any],
    patches_by_id: dict[str, dict[str, Any]],
    root: Path,
    *,
    verify: bool = True,
) -> tuple[str, str]:
    base_revision = next(
        item for item in repository["revisions"]
        if item["id"] == revision["derived_from"]
    )
    base = git_object(repository_path, f"{revision_ref(revision['derived_from'])}^{{commit}}")
    if base is None:
        base = git_object(repository_path, f"{base_revision['commit']}^{{commit}}")
    if base is None:
        raise ResourceError(
            f"{repository['id']}:{revision['id']}: derived revision is not populated"
        )
    commit = base
    with tempfile.TemporaryDirectory(
        prefix="paper-resources-derive-", dir=repository_path.parent
    ) as temporary_parent:
        checkout = Path(temporary_parent) / "worktree"
        run_git(
            [
                "--git-dir", str(repository_path), "worktree", "add", "--detach",
                str(checkout), base,
            ]
        )
        try:
            for application in revision["patches"]:
                patch_id, strip = patch_application(application)
                patch_path = (root / patches_by_id[patch_id]["path"]).resolve()
                run_git(
                    ["-C", str(checkout), "apply", "--index", f"-p{strip}", str(patch_path)]
                )
                tree = run_git(["-C", str(checkout), "write-tree"], capture=True)
                message = (
                    f"paper-resources: apply {patch_id}\n\n"
                    f"Construct {repository['id']}:{revision['id']}.\n"
                )
                identity = {
                    "GIT_AUTHOR_NAME": "Paper Resources",
                    "GIT_AUTHOR_EMAIL": "paper-resources@invalid",
                    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_NAME": "Paper Resources",
                    "GIT_COMMITTER_EMAIL": "paper-resources@invalid",
                    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
                }
                commit = run_git(
                    ["-C", str(checkout), "commit-tree", tree, "-p", commit, "-m", message],
                    capture=True,
                    environment=identity,
                )
                run_git(["-C", str(checkout), "reset", "--hard", commit])
        finally:
            run_git(
                [
                    "--git-dir", str(repository_path), "worktree", "remove", "--force",
                    str(checkout),
                ]
            )
    tree = git_object(repository_path, f"{commit}^{{tree}}")
    if tree is None:
        raise ResourceError(f"{repository['id']}:{revision['id']}: constructed no tree")
    if verify:
        verify_revision_objects(repository_path, repository["id"], revision, commit)
    return commit, tree


def revision_order(repository: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {revision["id"]: revision for revision in repository["revisions"]}
    output: list[dict[str, Any]] = []
    visited: set[str] = set()
    def visit(revision: dict[str, Any]) -> None:
        if revision["id"] in visited:
            return
        parent = revision.get("derived_from")
        if parent is not None:
            visit(by_id[parent])
        visited.add(revision["id"])
        output.append(revision)
    for revision in repository["revisions"]:
        visit(revision)
    return output


def revision_dependencies(
    repository: dict[str, Any], revision_ids: set[str]
) -> set[str]:
    by_id = {revision["id"]: revision for revision in repository["revisions"]}
    unknown = revision_ids - by_id.keys()
    if unknown:
        raise ResourceError(
            f"unknown revision in {repository['id']}: {', '.join(sorted(unknown))}"
        )
    selected = set(revision_ids)
    pending = list(selected)
    while pending:
        parent = by_id[pending.pop()].get("derived_from")
        if parent is not None and parent not in selected:
            selected.add(parent)
            pending.append(parent)
    return selected


def validate_repository_path(path: str) -> str:
    if not path or "\0" in path:
        raise ResourceError("repository path must not be empty")
    parsed = PurePosixPath(path)
    normalized = str(parsed)
    if parsed.is_absolute() or ".." in parsed.parts or normalized in ("", "."):
        raise ResourceError(f"invalid repository-relative path: {path}")
    if normalized != path:
        raise ResourceError(
            f"repository path must be normalized as {normalized}: {path}"
        )
    return normalized


def literal_pathspec(path: str) -> str:
    return f":(top,literal){validate_repository_path(path)}"


def compare_revision_paths(
    repository_path: Path,
    from_commit: str,
    to_commit: str,
    path: str | None = None,
) -> list[tuple[str, str]]:
    arguments = [
        "--git-dir", str(repository_path), "diff", "--name-status", "-z",
        "--no-renames", from_commit, to_commit,
    ]
    if path is not None:
        arguments.extend(["--", literal_pathspec(path)])
    output = run_git(arguments, capture=True, strip_output=False)
    if not output:
        return []
    fields = output.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ResourceError("git returned an invalid changed-path summary")
    return [(fields[index], fields[index + 1]) for index in range(0, len(fields), 2)]


def revision_file_diff(
    repository_path: Path,
    from_commit: str,
    to_commit: str,
    path: str,
) -> str:
    normalized = validate_repository_path(path)
    object_types: list[str | None] = []
    for commit in (from_commit, to_commit):
        entry = run_git(
            [
                "--git-dir", str(repository_path), "ls-tree", "-z", commit,
                "--", literal_pathspec(normalized),
            ],
            capture=True,
            strip_output=False,
        )
        object_types.append(entry.split(" ", 2)[1] if entry else None)
    if object_types == [None, None]:
        raise ResourceError(f"file does not exist in either revision: {normalized}")
    if any(item not in (None, "blob") for item in object_types):
        raise ResourceError(f"path is not a file in both revisions where it exists: {normalized}")
    return run_git(
        [
            "--git-dir", str(repository_path), "diff", "--no-color",
            "--no-ext-diff", "--no-textconv", "--no-renames",
            from_commit, to_commit, "--", literal_pathspec(normalized),
        ],
        capture=True,
        strip_output=False,
    )


def worktree_status(
    repository_path: Path, revision: dict[str, Any], worktree: dict[str, Any], root: Path
) -> tuple[bool, str, str | None, bool]:
    destination = root / worktree["path"]
    if not destination.exists():
        return False, "missing", None, False
    try:
        common = run_git(
            ["-C", str(destination), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture=True,
        )
        head = run_git(["-C", str(destination), "rev-parse", "HEAD^{commit}"], capture=True)
        dirty = bool(run_git(["-C", str(destination), "status", "--porcelain"], capture=True))
    except ResourceError:
        return False, "not a Git worktree", None, False
    if Path(common).resolve() != repository_path.resolve():
        return False, "belongs to another repository", head, dirty
    if head != revision["commit"]:
        return False, "wrong revision", head, dirty
    return True, "dirty" if dirty else "current", head, dirty


def populate_worktree(
    repository_path: Path, repository: dict[str, Any], revision: dict[str, Any],
    worktree: dict[str, Any], root: Path
) -> str:
    destination = root / worktree["path"]
    ok, detail, _head, dirty = worktree_status(repository_path, revision, worktree, root)
    label = f"{repository['id']}:{revision['id']}:{worktree['id']}"
    if ok:
        return f"{'dirty' if dirty else 'ok':8} {label}"
    if destination.exists():
        if dirty:
            raise ResourceError(f"{label}: dirty worktree has {detail}")
        if detail != "wrong revision":
            raise ResourceError(f"{label}: {detail}")
        run_git(["-C", str(destination), "checkout", "--detach", revision["commit"]])
        return f"updated  {label}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        [
            "--git-dir", str(repository_path), "worktree", "add", "--detach",
            str(destination), revision_ref(revision["id"]),
        ]
    )
    return f"created  {label}"


def populate_repository(
    repository: dict[str, Any], patches_by_id: dict[str, dict[str, Any]], root: Path,
    *,
    revision_ids: set[str] | None = None,
    worktree_key: tuple[str, str] | None = None,
) -> list[str]:
    repository_path, output = configure_repository(repository, root)
    selected = (
        revision_dependencies(repository, revision_ids)
        if revision_ids is not None else None
    )
    for revision in revision_order(repository):
        if selected is not None and revision["id"] not in selected:
            continue
        if "source" in revision:
            fetch_revision(repository, repository_path, revision)
        else:
            commit = git_object(repository_path, f"{revision['commit']}^{{commit}}")
            if commit is None:
                commit, _tree = construct_revision(
                    repository, repository_path, revision, patches_by_id, root
                )
            else:
                verify_revision_objects(repository_path, repository["id"], revision, commit)
            run_git(
                [
                    "--git-dir", str(repository_path), "update-ref",
                    revision_ref(revision["id"]), commit,
                ]
            )
        output.append(f"ok       {repository['id']}:{revision['id']}")
    for revision in repository["revisions"]:
        if revision_ids is not None and worktree_key is None:
            continue
        if selected is not None and revision["id"] not in selected:
            continue
        for worktree in revision.get("worktrees", []):
            if worktree_key is not None and (
                revision["id"], worktree["id"]
            ) != worktree_key:
                continue
            output.append(
                populate_worktree(
                    repository_path, repository, revision, worktree, root
                )
            )
    return output


def check_repository(
    repository: dict[str, Any], root: Path,
    *,
    revision_ids: set[str] | None = None,
    worktree_key: tuple[str, str] | None = None,
) -> list[tuple[bool, str]]:
    repository_path = root / repository["path"]
    label = repository["id"]
    if not (repository_path / "HEAD").is_file():
        return [(False, f"missing  {label}")]
    results: list[tuple[bool, str]] = [(True, f"ok       {label}")]
    remotes = [
        {"name": "origin", "url": repository["clone_url"]},
        *repository.get("remotes", []),
    ]
    for remote in remotes:
        matches = remote_url(repository_path, remote["name"]) == remote["url"]
        results.append(
            (
                matches,
                f"{'ok' if matches else 'changed':8} {label} remote {remote['name']}",
            )
        )
    for revision in repository["revisions"]:
        if revision_ids is not None and revision["id"] not in revision_ids:
            continue
        commit = git_object(repository_path, f"{revision_ref(revision['id'])}^{{commit}}")
        tree = git_object(repository_path, f"{commit}^{{tree}}") if commit else None
        ok = commit == revision["commit"] and tree == revision["tree"]
        results.append((ok, f"{'ok' if ok else 'changed':8} {label}:{revision['id']}"))
        if revision_ids is not None and worktree_key is None:
            continue
        for worktree in revision.get("worktrees", []):
            if worktree_key is not None and (
                revision["id"], worktree["id"]
            ) != worktree_key:
                continue
            worktree_ok, detail, _head, _dirty = worktree_status(
                repository_path, revision, worktree, root
            )
            worktree_label = f"{label}:{revision['id']}:{worktree['id']}"
            results.append((worktree_ok, f"{'ok' if worktree_ok else 'changed':8} {worktree_label} ({detail})"))
    return results
