"""Git-backed Buildroot package patch workspace lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterable
import uuid

from . import buildroot, git_resources, patch_export, workspace_store
from .config import ResourceError


CONSTRUCTION_POLICY = "package-patches-v1"
NOTE_VERSION = 1
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "Paper Resources",
    "GIT_AUTHOR_EMAIL": "paper-resources@invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Paper Resources",
    "GIT_COMMITTER_EMAIL": "paper-resources@invalid",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}


@dataclass(frozen=True)
class WorkspaceStatus:
    id: str
    package: str
    name: str
    path: str
    repository: str
    branch: str
    notes_ref: str
    origin_config: str
    state: str
    head: str | None
    export_base: str
    imported_tip: str
    expected_branch: bool
    base_is_ancestor: bool
    linear_history: bool
    clean: bool
    ongoing_operation: str | None
    commit_count: int
    annotated_commits: int
    missing_annotations: tuple[str, ...]
    conflicting_annotations: tuple[str, ...]
    last_export_tip: str | None
    exported: bool
    changed_export_paths: tuple[str, ...]
    attachments: tuple[str, ...]
    excluded_stages: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class OpenWorkspaceResult:
    created: bool
    status: WorkspaceStatus
    local_revisions: tuple[str, str]


@dataclass(frozen=True)
class AnnotationResult:
    package: str
    name: str
    commit: str
    patch_path: str
    notes_ref: str


@dataclass(frozen=True)
class ExportWorkspaceResult:
    package: str
    name: str
    dry_run: bool
    tip: str
    patches: tuple[patch_export.PlannedPatch, ...]
    created_paths: tuple[str, ...]
    updated_paths: tuple[str, ...]
    obsolete_paths: tuple[str, ...]
    other_selected_paths: tuple[str, ...]
    synthesized_notes: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    verification: str


def validate_component(value: str, label: str) -> str:
    if not _COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ResourceError(f"invalid workspace {label}: {value!r}")
    if value.endswith(".lock") or "@{" in value:
        raise ResourceError(f"invalid workspace {label}: {value!r}")
    return value


def _run_process(
    arguments: list[str], *, cwd: Path | None = None, input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, input=input_bytes, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ResourceError(f"{arguments[0]} is required for workspace operations") from error
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", errors="replace").strip()
        raise ResourceError(f"{' '.join(arguments)}: {detail or f'exit status {result.returncode}'}")
    return result


def _git_optional(arguments: list[str], *, cwd: Path | None = None) -> str | None:
    result = _run_process(["git", *arguments], cwd=cwd, check=False)
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", errors="strict").strip()


def _commit_range(checkout: Path, base: str, tip: str = "HEAD") -> list[str]:
    value = git_resources.run_git(
        ["-C", str(checkout), "rev-list", "--reverse", f"{base}..{tip}"],
        capture=True,
    )
    return value.splitlines() if value else []


def _fallback_subject(path: Path) -> str:
    stem = path.name.removesuffix(".patch")
    stem = re.sub(r"^\d+-", "", stem)
    subject = re.sub(r"[-_]+", " ", stem).strip()
    return subject or "Imported patch"


def _mail_metadata(path: Path) -> tuple[str, dict[str, str], bytes]:
    content = path.read_bytes()
    is_mail = bool(re.search(br"(?m)^Subject:\s+", content))
    if not is_mail:
        return _fallback_subject(path), dict(_SYNTHETIC_IDENTITY), content
    with tempfile.TemporaryDirectory(prefix="paper-mailinfo-") as temporary:
        message_path = Path(temporary) / "message"
        patch_path = Path(temporary) / "patch"
        result = _run_process(
            ["git", "mailinfo", "--encoding=UTF-8", str(message_path), str(patch_path)],
            input_bytes=content,
        )
        fields: dict[str, str] = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(": ")
            if separator:
                fields[key] = value
        subject = fields.get("Subject") or _fallback_subject(path)
        body = message_path.read_text(encoding="utf-8", errors="replace").strip()
        message = subject + ("\n\n" + body if body else "")
        author = fields.get("Author")
        email = fields.get("Email")
        date = fields.get("Date")
        identity = dict(_SYNTHETIC_IDENTITY)
        if author and email:
            identity["GIT_AUTHOR_NAME"] = author
            identity["GIT_AUTHOR_EMAIL"] = email
        if date:
            identity["GIT_AUTHOR_DATE"] = date
        return message, identity, patch_path.read_bytes()


def _apply_patch_commit(checkout: Path, parent: str, patch: buildroot.PatchInput) -> str:
    patch_path = Path(patch.resolved_path)
    message, identity, content = _mail_metadata(patch_path)
    with tempfile.NamedTemporaryFile(
        prefix="paper-workspace-patch-", suffix=".patch", delete=False
    ) as temporary:
        temporary.write(content)
        apply_path = Path(temporary.name)
    try:
        git_resources.run_git(
            ["-C", str(checkout), "apply", "--index", "-p1", str(apply_path)]
        )
    except ResourceError as error:
        raise ResourceError(f"cannot apply {patch.path} ({patch.stage}): {error}") from error
    finally:
        apply_path.unlink(missing_ok=True)
    tree = git_resources.run_git(
        ["-C", str(checkout), "write-tree"], capture=True
    )
    commit = git_resources.run_git(
        ["-C", str(checkout), "commit-tree", tree, "-p", parent, "-m", message],
        capture=True,
        environment=identity,
    )
    git_resources.run_git(["-C", str(checkout), "reset", "--hard", commit])
    return commit


def _revision_id(package: str, fingerprint: str, stage: str) -> str:
    return f"{package}-workspace-{fingerprint[:16]}-{stage}"


def _protect_revision(repository_path: Path, revision_id: str, commit: str) -> None:
    git_resources.run_git([
        "--git-dir", str(repository_path), "update-ref",
        git_resources.revision_ref(revision_id), commit,
    ])


def _construct(
    inspection: buildroot.PackageInspection,
    repository_path: Path,
) -> tuple[str, str, list[tuple[str, buildroot.PatchInput]]]:
    binding = inspection.source_binding
    if binding is None:
        raise ResourceError(
            "package source is not bound; specify --repository and --revision"
        )
    base = binding.commit
    editable_commits: list[tuple[str, buildroot.PatchInput]] = []
    temporary_parent = repository_path.parent
    with tempfile.TemporaryDirectory(
        prefix="paper-workspace-import-", dir=temporary_parent
    ) as temporary:
        checkout = Path(temporary) / "checkout"
        git_resources.run_git([
            "--git-dir", str(repository_path), "worktree", "add", "--detach",
            str(checkout), base,
        ])
        try:
            for patch in inspection.prerequisite_patches:
                base = _apply_patch_commit(checkout, base, patch)
            export_base = base
            for patch in inspection.editable_patches:
                base = _apply_patch_commit(checkout, base, patch)
                editable_commits.append((base, patch))
            imported_tip = base
        finally:
            git_resources.run_git([
                "--git-dir", str(repository_path), "worktree", "remove", "--force",
                str(checkout),
            ])
    return export_base, imported_tip, editable_commits


def _note(patch_path: str) -> str:
    return json.dumps(
        {"version": NOTE_VERSION, "patch_path": patch_path},
        sort_keys=True, separators=(",", ":"),
    )


def _write_note(repository_path: Path, ref: str, commit: str, value: str) -> None:
    git_resources.run_git([
        "--git-dir", str(repository_path), "notes", f"--ref={ref}",
        "add", "-f", "-m", value, commit,
    ])


def read_note(repository_path: Path, ref: str, commit: str) -> tuple[str, ...]:
    raw = _git_optional([
        "--git-dir", str(repository_path), "notes", f"--ref={ref}", "show", commit,
    ])
    if raw is None:
        return ()
    paths: list[str] = []
    # cat_sort_uniq/concatenate may preserve more than one JSON record.
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return ("<malformed>",)
        if (
            not isinstance(value, dict) or value.get("version") != NOTE_VERSION
            or not isinstance(value.get("patch_path"), str)
        ):
            return ("<malformed>",)
        paths.append(value["patch_path"])
    return tuple(dict.fromkeys(paths))


def notes_digest(repository_path: Path, ref: str, commits: Iterable[str]) -> str:
    values = [(commit, read_note(repository_path, ref, commit)) for commit in commits]
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _configure_note_rewrites(repository_path: Path, checkout: Path, notes_ref: str) -> None:
    extension = _git_optional([
        "--git-dir", str(repository_path), "config", "--bool",
        "extensions.worktreeConfig",
    ])
    if extension not in (None, "true"):
        raise ResourceError("repository explicitly disables extensions.worktreeConfig")
    if extension is None:
        bare = _git_optional([
            "--git-dir", str(repository_path), "config", "--get", "core.bare"
        ])
        git_resources.run_git([
            "--git-dir", str(repository_path), "config",
            "extensions.worktreeConfig", "true",
        ])
        if bare is not None:
            git_resources.run_git([
                "--git-dir", str(repository_path), "config", "--worktree",
                "core.bare", bare,
            ])
            git_resources.run_git([
                "--git-dir", str(repository_path), "config", "--unset", "core.bare"
            ])
    git_resources.run_git([
        "-C", str(checkout), "config", "--worktree", "core.bare", "false"
    ])
    for key, value in (
        ("notes.rewrite.amend", "true"),
        ("notes.rewrite.rebase", "true"),
        ("notes.rewriteMode", "cat_sort_uniq"),
        ("notes.rewriteRef", notes_ref),
    ):
        git_resources.run_git([
            "-C", str(checkout), "config", "--worktree", key, value
        ])


def _inspection_dict(inspection: buildroot.PackageInspection) -> dict[str, Any]:
    return asdict(inspection)


def _path_hash(path: Path) -> bytes:
    return hashlib.sha256(path.read_bytes()).digest()


def open_workspace(
    connection: sqlite3.Connection,
    inspection: buildroot.PackageInspection,
    repositories: dict[str, dict[str, Any]],
    resource_root: Path,
    name: str,
) -> OpenWorkspaceResult:
    package = validate_component(inspection.package, "package")
    name = validate_component(name, "name")
    existing = workspace_store.get_workspace(connection, package, name)
    if existing is not None:
        if (
            existing.project_root != inspection.project_root
            or existing.origin_config != inspection.buildroot_config
            or existing.input_fingerprint != inspection.input_fingerprint
        ):
            raise ResourceError(
                f"workspace {package}/{name} already exists with a different origin"
            )
        return OpenWorkspaceResult(
            False, get_workspace_status(connection, existing),
            (
                _revision_id(package, inspection.input_fingerprint, "base"),
                _revision_id(package, inspection.input_fingerprint, "imported"),
            ),
        )
    binding = inspection.source_binding
    if binding is None:
        raise ResourceError(
            "package source cannot be resolved automatically; specify repository and revision"
        )
    repository = repositories.get(binding.repository)
    if repository is None:
        raise ResourceError(f"unknown repository ID: {binding.repository}")
    repository_path = (resource_root / repository["path"]).resolve()
    if not (repository_path / "HEAD").is_file():
        raise ResourceError(f"repository is not populated: {binding.repository}")
    workspace_id = uuid.uuid4().hex
    checkout = (resource_root / "workspaces" / package / name).resolve()
    branch = f"workspace/{package}/{name}"
    notes_ref = f"refs/notes/paper-workspaces/{workspace_id}"
    if checkout.exists():
        raise ResourceError(f"workspace path already exists and will not be adopted: {checkout}")
    if git_resources.git_object(repository_path, f"refs/heads/{branch}") is not None:
        raise ResourceError(f"workspace branch already exists and will not be overwritten: {branch}")
    if git_resources.git_object(repository_path, notes_ref) is not None:
        raise ResourceError(f"workspace notes ref already exists: {notes_ref}")

    export_base, imported_tip, editable_commits = _construct(
        inspection, repository_path
    )
    base_revision = _revision_id(package, inspection.input_fingerprint, "base")
    imported_revision = _revision_id(
        package, inspection.input_fingerprint, "imported"
    )
    base_tree = git_resources.git_object(repository_path, f"{export_base}^{{tree}}")
    imported_tree = git_resources.git_object(repository_path, f"{imported_tip}^{{tree}}")
    assert base_tree is not None and imported_tree is not None
    _protect_revision(repository_path, base_revision, export_base)
    _protect_revision(repository_path, imported_revision, imported_tip)
    for commit, patch in editable_commits:
        _write_note(repository_path, notes_ref, commit, _note(patch.path))

    checkout.parent.mkdir(parents=True, exist_ok=True)
    try:
        git_resources.run_git([
            "--git-dir", str(repository_path), "worktree", "add", "-b", branch,
            str(checkout), imported_tip,
        ])
        _configure_note_rewrites(repository_path, checkout, notes_ref)
    except Exception:
        if checkout.exists():
            try:
                git_resources.run_git([
                    "--git-dir", str(repository_path), "worktree", "remove", "--force",
                    str(checkout),
                ])
            except ResourceError:
                pass
        git_resources.run_git([
            "--git-dir", str(repository_path), "update-ref", "-d",
            f"refs/heads/{branch}",
        ])
        git_resources.run_git([
            "--git-dir", str(repository_path), "update-ref", "-d", notes_ref,
        ])
        raise

    created = workspace_store.now()
    for revision_id, commit, tree, parent, stage in (
        (base_revision, export_base, base_tree, binding.commit, "export-base"),
        (imported_revision, imported_tip, imported_tree, export_base, "imported-tip"),
    ):
        workspace_store.register_local_revision(
            connection,
            workspace_store.LocalRevision(
                binding.repository, repository["path"], revision_id, commit, tree,
                parent, f"{package} workspace {stage}", "Paper Resources", True,
                CONSTRUCTION_POLICY, inspection.input_fingerprint,
                {
                    "kind": stage,
                    "package": package,
                    "buildroot_config": inspection.buildroot_config,
                    "source_binding": asdict(binding),
                    "patches": [
                        asdict(item)
                        for item in (
                            inspection.prerequisite_patches
                            if stage == "export-base"
                            else inspection.editable_patches
                        )
                    ],
                },
                created,
            ),
        )
    workspace = workspace_store.WorkspaceRecord(
        workspace_id, package, name, binding.repository, repository["path"],
        str(checkout), branch, notes_ref, inspection.project_root,
        inspection.buildroot_config, inspection.input_fingerprint,
        inspection.source_fingerprint, export_base, imported_tip, "ready",
        _inspection_dict(inspection), created, None,
    )
    workspace_store.create_workspace(connection, workspace)
    digest = notes_digest(
        repository_path, notes_ref, [commit for commit, _patch in editable_commits]
    )
    workspace_store.record_export(
        connection, workspace_id=workspace_id, kind="import",
        tip=imported_tip, notes_digest=digest, verification="imported-stack",
        paths=[
            (commit, patch.path, _path_hash(Path(patch.resolved_path)))
            for commit, patch in editable_commits
        ],
    )
    return OpenWorkspaceResult(
        True, get_workspace_status(connection, workspace),
        (base_revision, imported_revision),
    )


def _ongoing_operation(checkout: Path) -> str | None:
    for name, label in (
        ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"),
    ):
        path = _git_optional(["-C", str(checkout), "rev-parse", "--git-path", name])
        if path and Path(path).exists():
            return label
    return None


def get_workspace_status(
    connection: sqlite3.Connection, workspace: workspace_store.WorkspaceRecord
) -> WorkspaceStatus:
    checkout = Path(workspace.path)
    repository_path = Path(workspace.repository_path)
    if not repository_path.is_absolute():
        # Persisted paths are resource-root relative; infer root from checkout.
        repository_path = checkout.parents[2] / repository_path
    warnings: list[str] = []
    blockers: list[str] = []
    head = _git_optional(["-C", str(checkout), "rev-parse", "HEAD^{commit}"])
    branch = _git_optional(["-C", str(checkout), "symbolic-ref", "--short", "HEAD"])
    expected_branch = branch == workspace.branch
    if not expected_branch:
        blockers.append(
            f"checkout must be on {workspace.branch}; switch it explicitly before continuing"
        )
    base_is_ancestor = False
    linear = False
    commits: list[str] = []
    if head is not None:
        ancestor = _run_process(
            ["git", "-C", str(checkout), "merge-base", "--is-ancestor",
             workspace.export_base, head], check=False,
        )
        base_is_ancestor = ancestor.returncode == 0
        if not base_is_ancestor:
            blockers.append("recorded export base is no longer an ancestor of HEAD")
        else:
            commits = _commit_range(checkout, workspace.export_base, head)
            merges = git_resources.run_git([
                "-C", str(checkout), "rev-list", "--merges",
                f"{workspace.export_base}..{head}",
            ], capture=True)
            linear = not bool(merges)
            if not linear:
                blockers.append("export range contains merge commits")
    else:
        blockers.append("workspace path is not a valid Git checkout")
    raw_status = _git_optional([
        "-C", str(checkout), "status", "--porcelain=v1", "--ignored"
    ])
    clean = raw_status == ""
    if raw_status is None:
        clean = False
    if not clean:
        blockers.append("worktree or index has staged, unstaged, untracked, or ignored changes")
    operation = _ongoing_operation(checkout) if checkout.exists() else None
    if operation:
        blockers.append(f"Git {operation} operation is in progress")
    annotated = 0
    missing: list[str] = []
    conflicts: list[str] = []
    for commit in commits:
        paths = read_note(repository_path, workspace.notes_ref, commit)
        if not paths:
            missing.append(commit)
        elif paths == ("<malformed>",) or len(paths) > 1:
            conflicts.append(commit)
        else:
            annotated += 1
    latest = workspace_store.latest_export(connection, workspace.id)
    last_tip: str | None = None
    exported = False
    changed_paths: list[str] = []
    if latest is not None:
        export, paths = latest
        last_tip = git_resources.oid_to_hex(export["tip_oid"])
        for item in paths:
            path = Path(workspace.project_root) / item["path"]
            if not path.is_file() or _path_hash(path) != item["content_sha256"]:
                changed_paths.append(item["path"])
        current_digest = notes_digest(repository_path, workspace.notes_ref, commits)
        exported = (
            head == last_tip
            and current_digest == export["notes_digest"]
            and not changed_paths
            and clean
        )
    attachments = tuple(
        f"{row['buildroot_config']}:{row['package']}"
        for row in connection.execute(
            """
            SELECT buildroot_config, package FROM workspace_attachments
            WHERE workspace_id = ? ORDER BY buildroot_config, package
            """,
            (workspace.id,),
        )
    )
    pending = workspace_store.pending_operations(connection, workspace.id)
    if pending:
        blockers.append(
            "unresolved workspace operation(s): "
            + ", ".join(f"{item['operation_kind']}:{item['phase']}" for item in pending)
        )
    excluded = tuple(workspace.inspection.get("excluded_stages", []))
    if excluded:
        warnings.append("workspace omits reported Buildroot preparation hooks")
    return WorkspaceStatus(
        workspace.id, workspace.package, workspace.name, workspace.path,
        workspace.repository_id, workspace.branch, workspace.notes_ref,
        workspace.origin_config, workspace.state, head, workspace.export_base,
        workspace.imported_tip, expected_branch, base_is_ancestor, linear, clean,
        operation, len(commits), annotated, tuple(missing), tuple(conflicts),
        last_tip, exported, tuple(changed_paths), attachments, excluded,
        tuple(warnings), tuple(blockers),
    )


def annotate_workspace_commit(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    commit: str,
    patch_path: str,
) -> AnnotationResult:
    checkout = Path(workspace.path)
    repository_path = Path(workspace.repository_path)
    if not repository_path.is_absolute():
        repository_path = checkout.parents[2] / repository_path
    resolved_commit = _git_optional([
        "-C", str(checkout), "rev-parse", "--verify", f"{commit}^{{commit}}"
    ])
    if resolved_commit is None:
        raise ResourceError(f"unknown workspace commit: {commit}")
    commits = set(_commit_range(checkout, workspace.export_base))
    if resolved_commit not in commits:
        raise ResourceError("commit is outside the workspace export range")
    pure = PurePosixPath(patch_path)
    if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".patch":
        raise ResourceError("patch_path must be a safe project-relative .patch path")
    project_root = Path(workspace.project_root).resolve()
    destination = (project_root / Path(*pure.parts)).resolve()
    try:
        relative = destination.relative_to(project_root).as_posix()
    except ValueError as error:
        raise ResourceError("patch_path escapes the Paper Linux project") from error
    allowed = False
    for directory in workspace.inspection.get("patch_directories", []):
        if directory.get("stage") != "editable" or not directory.get("selected"):
            continue
        root = Path(directory["resolved_path"]).resolve()
        try:
            destination.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise ResourceError("patch_path is outside the selected writable patch directories")
    _write_note(repository_path, workspace.notes_ref, resolved_commit, _note(relative))
    return AnnotationResult(
        workspace.package, workspace.name, resolved_commit, relative,
        workspace.notes_ref,
    )


def _selected_editable_directories(
    inspection: buildroot.PackageInspection,
) -> tuple[buildroot.PatchDirectory, ...]:
    return tuple(
        item for item in inspection.patch_directories
        if item.stage == "editable" and item.selected
    )


def _verify_export_inputs(
    workspace: workspace_store.WorkspaceRecord,
    current: buildroot.PackageInspection,
) -> None:
    recorded = workspace.inspection
    if current.source_fingerprint != workspace.source_fingerprint:
        raise ResourceError("normal package source changed; reopen the workspace")
    current_prerequisites = [asdict(item) for item in current.prerequisite_patches]
    if current_prerequisites != recorded.get("prerequisite_patches", []):
        raise ResourceError("prerequisite patch inputs changed; reopen the workspace")
    current_directories = [
        (item.path, item.stage, item.selected)
        for item in current.patch_directories
    ]
    recorded_directories = [
        (item["path"], item["stage"], item["selected"])
        for item in recorded.get("patch_directories", [])
    ]
    if current_directories != recorded_directories:
        raise ResourceError("Buildroot patch-directory selection changed; reopen the workspace")
    if (
        list(current.pre_patch_hooks) != recorded.get("pre_patch_hooks", [])
        or list(current.post_patch_hooks) != recorded.get("post_patch_hooks", [])
    ):
        raise ResourceError("Buildroot patch hook inputs changed; reopen the workspace")


def _format_patch(checkout: Path, commit: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(
        [
            "git", "-C", str(checkout),
            "-c", "format.signature=", "-c", "format.useAutoBase=false",
            "format-patch", "-1", commit, "--stdout", "--no-signature",
            "--no-notes", "--no-numbered", "--subject-prefix=PATCH",
            "--full-index", "--binary",
        ],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ResourceError(f"cannot format commit {commit[:12]}: {detail}")
    content = result.stdout
    header = content.split(b"\n---\n", 1)[0]
    missing: list[str] = []
    for label, pattern in (
        ("From", br"(?m)^From: .+"),
        ("Subject", br"(?m)^Subject: .+"),
        ("Signed-off-by", br"(?mi)^Signed-off-by: .+"),
    ):
        if re.search(pattern, header) is None:
            missing.append(label)
    if missing:
        raise ResourceError(
            f"commit {commit[:12]} cannot be exported: missing required "
            f"project patch metadata ({', '.join(missing)}); amend the commit message"
        )
    return content


def _replay_export(
    repository_path: Path,
    base: str,
    tip: str,
    patches: Iterable[bytes],
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="paper-workspace-replay-", dir=repository_path.parent
    ) as temporary:
        checkout = Path(temporary) / "checkout"
        git_resources.run_git([
            "--git-dir", str(repository_path), "worktree", "add", "--detach",
            str(checkout), base,
        ])
        try:
            for index, content in enumerate(patches, start=1):
                result = _run_process(
                    ["git", "-C", str(checkout), "apply", "--index", "-p1"],
                    input_bytes=content, check=False,
                )
                if result.returncode:
                    detail = result.stderr.decode("utf-8", errors="replace").strip()
                    raise ResourceError(
                        f"exported-series replay failed at patch {index}: {detail}"
                    )
            replay_tree = git_resources.run_git(
                ["-C", str(checkout), "write-tree"], capture=True
            )
        finally:
            git_resources.run_git([
                "--git-dir", str(repository_path), "worktree", "remove", "--force",
                str(checkout),
            ])
    tip_tree = git_resources.git_object(repository_path, f"{tip}^{{tree}}")
    if replay_tree != tip_tree:
        raise ResourceError(
            "exported-series replay tree does not equal the workspace tip tree"
        )


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recover_export_operation(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    operation: sqlite3.Row,
) -> None:
    payload = json.loads(operation["payload"])
    repository_path = Path(payload["repository_path"])
    for note in reversed(payload.get("notes", [])):
        current = _git_optional([
            "--git-dir", str(repository_path), "notes",
            f"--ref={workspace.notes_ref}", "show", note["commit"],
        ])
        if current == note["new"]:
            if note["old"] is None:
                _run_process([
                    "git", "--git-dir", str(repository_path), "notes",
                    f"--ref={workspace.notes_ref}", "remove", note["commit"],
                ], check=False)
            else:
                _write_note(
                    repository_path, workspace.notes_ref, note["commit"], note["old"]
                )
        elif current != note["old"]:
            raise ResourceError(
                f"cannot recover export: note for {note['commit'][:12]} changed externally"
            )
    for item in reversed(payload.get("files", [])):
        destination = Path(item["destination"])
        staged = Path(item["staged"])
        backup = Path(item["backup"]) if item.get("backup") else None
        if item.get("published"):
            if not destination.is_file() or _sha256_hex(destination) != item["new_hash"]:
                raise ResourceError(
                    f"cannot recover export: destination changed externally: {destination}"
                )
            if backup is not None:
                os.replace(backup, destination)
            else:
                destination.unlink()
        elif backup is not None:
            backup.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
    workspace_store.delete_operation(connection, operation["id"])


def recover_workspace_operations(
    connection: sqlite3.Connection, workspace: workspace_store.WorkspaceRecord
) -> None:
    for operation in workspace_store.pending_operations(connection, workspace.id):
        if operation["operation_kind"] != "export":
            raise ResourceError(
                f"workspace has unresolved {operation['operation_kind']} operation "
                f"{operation['id']}"
            )
        if operation["phase"] == "complete":
            payload = json.loads(operation["payload"])
            for item in payload.get("files", []):
                Path(item["staged"]).unlink(missing_ok=True)
                if item.get("backup"):
                    Path(item["backup"]).unlink(missing_ok=True)
            workspace_store.delete_operation(connection, operation["id"])
        else:
            _recover_export_operation(connection, workspace, operation)


def export_workspace(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    current_inspection: buildroot.PackageInspection,
    *,
    dry_run: bool = False,
) -> ExportWorkspaceResult:
    recover_workspace_operations(connection, workspace)
    _verify_export_inputs(workspace, current_inspection)
    status = get_workspace_status(connection, workspace)
    if status.blockers:
        raise ResourceError("cannot export workspace: " + "; ".join(status.blockers))
    assert status.head is not None
    checkout = Path(workspace.path)
    repository_path = Path(workspace.repository_path)
    if not repository_path.is_absolute():
        repository_path = checkout.parents[2] / repository_path
    commits = _commit_range(checkout, workspace.export_base, status.head)
    commit_inputs: list[patch_export.ExportCommit] = []
    for commit in commits:
        empty = _run_process([
            "git", "-C", str(checkout), "diff-tree", "--quiet",
            f"{commit}^", commit,
        ], check=False)
        if empty.returncode == 0:
            raise ResourceError(f"empty commit is not exportable: {commit[:12]}")
        subject = git_resources.run_git([
            "-C", str(checkout), "show", "-s", "--format=%s", commit,
        ], capture=True)
        commit_inputs.append(patch_export.ExportCommit(
            commit, subject, read_note(repository_path, workspace.notes_ref, commit)
        ))
    directories = _selected_editable_directories(current_inspection)
    if not directories:
        raise ResourceError("package has no selected editable patch directory")
    existing: list[str] = []
    for directory in directories:
        root = Path(directory.resolved_path)
        if root.is_dir():
            existing.extend(
                (Path(current_inspection.project_root) / item).relative_to(
                    current_inspection.project_root
                ).as_posix()
                for item in sorted(root.glob("*.patch"))
            )
    latest = workspace_store.latest_export(connection, workspace.id)
    managed_rows = latest[1] if latest is not None else []
    managed = {row["path"]: row for row in managed_rows}
    plan = patch_export.plan_export(
        commit_inputs,
        directories=[item.path for item in directories],
        existing_paths=existing,
        managed_paths=managed,
    )
    generated = {
        item.path: _format_patch(checkout, item.commit) for item in plan.patches
    }
    _replay_export(
        repository_path, workspace.export_base, status.head,
        [generated[item.path] for item in plan.patches],
    )
    project_root = Path(current_inspection.project_root)
    created: list[str] = []
    updated: list[str] = []
    for item in plan.patches:
        destination = project_root / item.path
        record = managed.get(item.path)
        new_digest = hashlib.sha256(generated[item.path]).digest()
        if record is None:
            if destination.exists():
                raise ResourceError(
                    f"refusing to overwrite unrelated patch destination: {item.path}"
                )
            created.append(item.path)
        else:
            if not destination.is_file():
                raise ResourceError(
                    f"managed patch destination was removed externally: {item.path}"
                )
            current_digest = _path_hash(destination)
            if current_digest != record["content_sha256"] and current_digest != new_digest:
                raise ResourceError(
                    f"managed patch destination changed externally: {item.path}"
                )
            updated.append(item.path)
    synthesized = tuple(
        (item.commit, item.path) for item in plan.patches if item.synthesized
    )
    if dry_run:
        return ExportWorkspaceResult(
            workspace.package, workspace.name, True, status.head, plan.patches,
            tuple(created), tuple(updated), plan.obsolete_paths,
            plan.other_selected_paths, synthesized, plan.warnings,
            "exported-series replay equals workspace tip tree",
        )

    # Recheck the Git identities immediately before staging external writes.
    rechecked = get_workspace_status(connection, workspace)
    if rechecked.head != status.head or rechecked.blockers:
        raise ResourceError("workspace changed while export was being planned; retry")
    operation_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "repository_path": str(repository_path), "files": [], "notes": [],
    }
    completed = False
    try:
        for item in plan.patches:
            destination = project_root / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = destination.parent / f".{destination.name}.paper-{operation_id}.new"
            backup = (
                destination.parent / f".{destination.name}.paper-{operation_id}.bak"
                if destination.exists() else None
            )
            with staged.open("xb") as handle:
                handle.write(generated[item.path])
                handle.flush()
                os.fsync(handle.fileno())
            staged.chmod(0o644)
            if backup is not None:
                shutil.copy2(destination, backup)
            payload["files"].append({
                "path": item.path, "destination": str(destination),
                "staged": str(staged), "backup": str(backup) if backup else None,
                "old_hash": _sha256_hex(destination) if destination.exists() else None,
                "new_hash": hashlib.sha256(generated[item.path]).hexdigest(),
                "published": False,
            })
        for commit, path in synthesized:
            old = _git_optional([
                "--git-dir", str(repository_path), "notes",
                f"--ref={workspace.notes_ref}", "show", commit,
            ])
            payload["notes"].append({
                "commit": commit, "old": old, "new": _note(path), "written": False,
            })
        workspace_store.save_operation(
            connection, operation_id=operation_id, workspace_id=workspace.id,
            kind="export", phase="prepared", payload=payload,
        )
        workspace_store.save_operation(
            connection, operation_id=operation_id, workspace_id=workspace.id,
            kind="export", phase="publishing", payload=payload,
        )
        for item in payload["files"]:
            destination = Path(item["destination"])
            current_hash = _sha256_hex(destination) if destination.exists() else None
            if current_hash != item["old_hash"]:
                raise ResourceError(
                    f"patch destination changed before publish: {item['path']}"
                )
            os.replace(item["staged"], destination)
            item["published"] = True
            workspace_store.save_operation(
                connection, operation_id=operation_id, workspace_id=workspace.id,
                kind="export", phase="publishing", payload=payload,
            )
        for note in payload["notes"]:
            _write_note(
                repository_path, workspace.notes_ref, note["commit"], note["new"]
            )
            note["written"] = True
            workspace_store.save_operation(
                connection, operation_id=operation_id, workspace_id=workspace.id,
                kind="export", phase="notes", payload=payload,
            )
        final_commits = _commit_range(checkout, workspace.export_base, status.head)
        final_digest = notes_digest(repository_path, workspace.notes_ref, final_commits)
        workspace_store.complete_export_operation(
            connection, operation_id=operation_id, payload=payload,
            workspace_id=workspace.id, kind="export", tip=status.head,
            notes_digest=final_digest,
            verification="exported-series replay equals workspace tip tree",
            paths=[
                (
                    item.commit, item.path,
                    hashlib.sha256(generated[item.path]).digest(),
                )
                for item in plan.patches
            ],
            obsolete_paths=plan.obsolete_paths,
        )
        completed = True
        for item in payload["files"]:
            if item["backup"]:
                try:
                    Path(item["backup"]).unlink(missing_ok=True)
                except OSError:
                    pass
        workspace_store.delete_operation(connection, operation_id)
    except Exception as error:
        if not completed:
            workspace_store.save_operation(
                connection, operation_id=operation_id, workspace_id=workspace.id,
                kind="export", phase="failed", payload=payload, error=str(error),
            )
        raise
    return ExportWorkspaceResult(
        workspace.package, workspace.name, False, status.head, plan.patches,
        tuple(created), tuple(updated), plan.obsolete_paths,
        plan.other_selected_paths, synthesized, plan.warnings,
        "exported-series replay equals workspace tip tree",
    )
