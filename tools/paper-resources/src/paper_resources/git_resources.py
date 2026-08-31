"""Git repository and worktree resource operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
import os
import subprocess
import tempfile
from typing import Any, Iterable, Iterator, Mapping

from .config import ResourceError


@dataclass(frozen=True, slots=True)
class GitBlob:
    """One blob occurrence in a Git tree."""

    oid: bytes
    size: int
    mode: int
    path: str


class GitBlobReader:
    """Read many blobs through one persistent ``git cat-file --batch`` process."""

    def __init__(self, repository_path: Path):
        self.repository_path = repository_path
        self._errors = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                [
                    "git", "--git-dir", str(repository_path),
                    "cat-file", "--batch",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._errors,
            )
        except FileNotFoundError as error:
            self._errors.close()
            raise ResourceError(
                "git is required to access repository resources"
            ) from error
        if self._process.stdin is None or self._process.stdout is None:
            self._process.kill()
            self._process.wait()
            self._errors.close()
            raise ResourceError("cannot open Git blob protocol streams")
        self._closed = False

    def __enter__(self) -> "GitBlobReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _stderr(self) -> str:
        self._errors.flush()
        self._errors.seek(0)
        return self._errors.read().decode("utf-8", errors="replace").strip()

    def read(self, oid: bytes, expected_size: int) -> bytes:
        if self._closed:
            raise ResourceError("Git blob reader is closed")
        oid_text = oid_to_hex(oid)
        try:
            self._process.stdin.write(oid_text.encode("ascii") + b"\n")
            self._process.stdin.flush()
            header = self._process.stdout.readline()
        except BrokenPipeError as error:
            detail = self._stderr() or "broken protocol pipe"
            raise ResourceError(f"cannot request Git blob: {detail}") from error
        if not header:
            detail = self._stderr() or "Git returned no blob header"
            raise ResourceError(detail)
        fields = header.rstrip(b"\n").split()
        if len(fields) == 2 and fields[1] == b"missing":
            raise ResourceError(f"Git blob is missing: {oid_text}")
        if len(fields) != 3:
            raise ResourceError(f"Git returned an invalid blob header: {header!r}")
        returned_oid, object_type, raw_size = fields
        try:
            size = int(raw_size)
        except ValueError as error:
            raise ResourceError(
                f"Git returned an invalid blob size: {raw_size!r}"
            ) from error
        try:
            returned_oid_text = returned_oid.decode("ascii")
        except UnicodeDecodeError as error:
            raise ResourceError("Git returned a non-ASCII object ID") from error
        if returned_oid_text != oid_text or object_type != b"blob":
            raise ResourceError(f"Git returned the wrong object for blob {oid_text}")
        if size != expected_size:
            raise ResourceError(
                f"Git returned {size} bytes for a {expected_size}-byte blob"
            )
        content = self._process.stdout.read(size)
        terminator = self._process.stdout.read(1)
        if len(content) != size or terminator != b"\n":
            raise ResourceError(f"Git returned a truncated blob: {oid_text}")
        return content

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._process.stdin.close()
            returncode = self._process.wait()
            if returncode:
                detail = self._stderr() or f"exit status {returncode}"
                raise ResourceError(f"git cat-file failed: {detail}")
        finally:
            self._process.stdout.close()
            self._errors.close()


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


def run_git_bytes(arguments: Iterable[str], *, cwd: Path | None = None) -> bytes:
    """Run Git and return unmodified binary output."""
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ResourceError("git is required to access repository resources") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"exit status {error.returncode}"
        raise ResourceError(f"{' '.join(command)}: {detail}") from error
    return result.stdout


def iter_git_nul_records(
    arguments: Iterable[str], *, cwd: Path | None = None
) -> Iterator[bytes]:
    """Run Git and incrementally yield its NUL-terminated binary records."""
    command = ["git", *arguments]
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
        except FileNotFoundError as error:
            raise ResourceError(
                "git is required to access repository resources"
            ) from error
        if process.stdout is None:
            process.kill()
            process.wait()
            raise ResourceError("cannot capture Git output")
        buffer = bytearray()
        completed = False
        try:
            while chunk := process.stdout.read(64 * 1024):
                buffer.extend(chunk)
                while (separator := buffer.find(0)) >= 0:
                    yield bytes(buffer[:separator])
                    del buffer[:separator + 1]
            returncode = process.wait()
            completed = True
            if returncode:
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace").strip()
                if not detail:
                    detail = f"exit status {returncode}"
                raise ResourceError(f"{' '.join(command)}: {detail}")
            if buffer:
                raise ResourceError("git returned output without a NUL terminator")
        finally:
            process.stdout.close()
            if not completed and process.poll() is None:
                process.terminate()
                process.wait()


def oid_from_hex(value: str) -> bytes:
    """Convert a complete SHA-1 or SHA-256 Git object name to binary form."""
    if len(value) not in (40, 64):
        raise ResourceError(f"invalid Git object ID length: {value!r}")
    try:
        oid = bytes.fromhex(value)
    except ValueError as error:
        raise ResourceError(f"invalid Git object ID: {value!r}") from error
    if len(oid) not in (20, 32):
        raise ResourceError(f"invalid Git object ID: {value!r}")
    return oid


def oid_to_hex(value: bytes) -> str:
    """Convert a binary SHA-1 or SHA-256 Git object name to canonical hex."""
    if not isinstance(value, bytes) or len(value) not in (20, 32):
        raise ResourceError("a Git object ID must be a 20- or 32-byte blob")
    return value.hex()


def iter_tree_blobs(repository_path: Path, tree_oid: str | bytes) -> Iterator[GitBlob]:
    """Yield every blob occurrence in a tree in Git's deterministic path order."""
    object_name = (
        oid_to_hex(tree_oid)
        if isinstance(tree_oid, bytes)
        else oid_to_hex(oid_from_hex(tree_oid))
    )
    records = iter_git_nul_records(
        [
            "--git-dir", str(repository_path), "ls-tree", "-r", "-z", "-l",
            "--full-tree", object_name,
        ]
    )
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, object_type, raw_oid, raw_size = metadata.split()
        except ValueError as error:
            raise ResourceError("git returned an invalid tree entry") from error
        if object_type != b"blob":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResourceError(
                f"repository contains a non-UTF-8 path: {raw_path.hex()}"
            ) from error
        try:
            mode = int(raw_mode, 8)
            size = int(raw_size)
            oid_text = raw_oid.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise ResourceError(f"invalid Git tree metadata for {path}") from error
        yield GitBlob(
            oid=oid_from_hex(oid_text),
            size=size,
            mode=mode,
            path=validate_repository_path(path),
        )


def read_blob(repository_path: Path, oid: str | bytes) -> bytes:
    """Read one blob directly from a repository object database."""
    object_name = (
        oid_to_hex(oid)
        if isinstance(oid, bytes)
        else oid_to_hex(oid_from_hex(oid))
    )
    return run_git_bytes(
        ["--git-dir", str(repository_path), "cat-file", "blob", object_name]
    )


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
