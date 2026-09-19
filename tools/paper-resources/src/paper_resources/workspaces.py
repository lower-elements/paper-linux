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
    warnings: tuple[str, ...]


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


@dataclass(frozen=True)
class AttachWorkspaceResult:
    attached: bool
    package: str
    name: str
    buildroot_config: str
    override_file: str
    workspace_path: str
    shadowed_unmanaged_override: bool
    differing_patch_stack: bool
    warnings: tuple[str, ...]
    rebuild_commands: tuple[str, ...]


@dataclass(frozen=True)
class DetachWorkspaceResult:
    detached: bool
    package: str
    buildroot_config: str
    override_file: str | None
    warnings: tuple[str, ...]
    rebuild_commands: tuple[str, ...]


@dataclass(frozen=True)
class CloseWorkspaceResult:
    package: str
    name: str
    path: str
    force: bool
    detached_outputs: tuple[str, ...]
    discarded_development_work: bool
    retained_revisions: tuple[str, str]
    removed_branch: str
    removed_notes_ref: str


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


def _mail_metadata(
    path: Path,
) -> tuple[str, dict[str, str], bytes, str | None]:
    content = path.read_bytes()
    is_mail = bool(re.search(br"(?m)^Subject:\s+", content))
    if not is_mail:
        return (
            _fallback_subject(path), dict(_SYNTHETIC_IDENTITY), content,
            f"{path.name}: plain diff imported with a synthetic Paper Resources identity; add author, rationale, upstream status, and sign-off before export",
        )
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
        warning = None
        if not (author and email):
            warning = (
                f"{path.name}: mail patch lacks complete author metadata and was "
                "imported with a synthetic Paper Resources identity"
            )
        return message, identity, patch_path.read_bytes(), warning


def _apply_patch_commit(
    checkout: Path, parent: str, patch: buildroot.PatchInput
) -> tuple[str, str | None]:
    patch_path = Path(patch.resolved_path)
    message, identity, content, warning = _mail_metadata(patch_path)
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
    return commit, warning


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
) -> tuple[str, str, list[tuple[str, buildroot.PatchInput]], list[str]]:
    binding = inspection.source_binding
    if binding is None:
        raise ResourceError(
            "package source is not bound; specify --repository and --revision"
        )
    base = binding.commit
    editable_commits: list[tuple[str, buildroot.PatchInput]] = []
    warnings: list[str] = []
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
                base, warning = _apply_patch_commit(checkout, base, patch)
                if warning:
                    warnings.append(warning)
            export_base = base
            for patch in inspection.editable_patches:
                base, warning = _apply_patch_commit(checkout, base, patch)
                if warning:
                    warnings.append(warning)
                editable_commits.append((base, patch))
            imported_tip = base
        finally:
            git_resources.run_git([
                "--git-dir", str(repository_path), "worktree", "remove", "--force",
                str(checkout),
            ])
    return export_base, imported_tip, editable_commits, warnings


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
        if existing.state != "ready":
            raise ResourceError(
                f"workspace {package}/{name} has an unresolved {existing.state} operation"
            )
        existing_status = get_workspace_status(connection, existing)
        return OpenWorkspaceResult(
            False, existing_status,
            (
                _revision_id(package, inspection.input_fingerprint, "base"),
                _revision_id(package, inspection.input_fingerprint, "imported"),
            ), existing_status.warnings,
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

    export_base, imported_tip, editable_commits, import_warnings = _construct(
        inspection, repository_path
    )
    base_revision = _revision_id(package, inspection.input_fingerprint, "base")
    imported_revision = _revision_id(
        package, inspection.input_fingerprint, "imported"
    )
    base_tree = git_resources.git_object(repository_path, f"{export_base}^{{tree}}")
    imported_tree = git_resources.git_object(repository_path, f"{imported_tip}^{{tree}}")
    assert base_tree is not None and imported_tree is not None
    checkout.parent.mkdir(parents=True, exist_ok=True)
    try:
        for commit, patch in editable_commits:
            _write_note(repository_path, notes_ref, commit, _note(patch.path))
        git_resources.run_git([
            "--git-dir", str(repository_path), "worktree", "add", "-b", branch,
            str(checkout), imported_tip,
        ])
        _configure_note_rewrites(repository_path, checkout, notes_ref)
        _protect_revision(repository_path, base_revision, export_base)
        _protect_revision(repository_path, imported_revision, imported_tip)

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
        inspection_record = _inspection_dict(inspection)
        inspection_record["import_warnings"] = import_warnings
        workspace = workspace_store.WorkspaceRecord(
            workspace_id, package, name, binding.repository, repository["path"],
            str(checkout), branch, notes_ref, inspection.project_root,
            inspection.buildroot_config, inspection.input_fingerprint,
            inspection.source_fingerprint, export_base, imported_tip, "opening",
            inspection_record, created, None,
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
        workspace_store.set_workspace_state(connection, workspace_id, "ready")
        workspace = workspace_store.get_workspace_by_id(connection, workspace_id)
        assert workspace is not None
    except Exception:
        workspace_store.delete_failed_workspace(connection, workspace_id)
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
    return OpenWorkspaceResult(
        True, get_workspace_status(connection, workspace),
        (base_revision, imported_revision), tuple(import_warnings),
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
    warnings.extend(workspace.inspection.get("import_warnings", []))
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


def _optional_sha256(path: Path) -> str | None:
    return _sha256_hex(path) if path.is_file() else None


def _recover_file_operation(
    connection: sqlite3.Connection, operation: sqlite3.Row
) -> None:
    payload = json.loads(operation["payload"])
    item = payload["file"]
    destination = Path(item["destination"])
    staged = Path(item["staged"])
    backup = Path(item["backup"]) if item.get("backup") else None
    if item.get("published"):
        remove_destination = item.get("remove_destination", False)
        current_hash = _optional_sha256(destination)
        if remove_destination and current_hash is None:
            pass
        elif current_hash != item["new_hash"]:
            raise ResourceError(
                f"cannot recover {operation['operation_kind']}: override file "
                f"changed externally: {destination}"
            )
        if backup is None:
            destination.unlink(missing_ok=True)
        else:
            os.replace(backup, destination)
    elif backup is not None:
        backup.unlink(missing_ok=True)
    staged.unlink(missing_ok=True)
    workspace_store.delete_operation(connection, operation["id"])


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
            if operation["operation_kind"] in {"attach", "detach"}:
                if operation["phase"] == "complete":
                    payload = json.loads(operation["payload"])
                    Path(payload["file"]["staged"]).unlink(missing_ok=True)
                    if payload["file"].get("backup"):
                        Path(payload["file"]["backup"]).unlink(missing_ok=True)
                    workspace_store.delete_operation(connection, operation["id"])
                else:
                    _recover_file_operation(connection, operation)
                continue
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


def _make_path_value(path: str) -> str:
    if not path.startswith("/"):
        raise ResourceError("workspace override path must be absolute")
    if any(character in path for character in "\n\r\t\x00#$\\"):
        raise ResourceError(
            "workspace path contains characters unsupported by Buildroot Make overrides"
        )
    return path.replace(" ", "\\ ")


def _managed_block(
    block_id: str, variable_prefix: str, workspace_path: str, needs_separator: bool
) -> bytes:
    separator = "\n" if needs_separator else ""
    text = (
        f"{separator}# >>> paper-resources workspace {block_id}\n"
        f"{variable_prefix}_OVERRIDE_SRCDIR = {_make_path_value(workspace_path)}\n"
        f"# <<< paper-resources workspace {block_id}\n"
    )
    return text.encode("utf-8")


def _publish_override_change(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    *,
    kind: str,
    destination: Path,
    new_content: bytes,
) -> tuple[str, dict[str, Any]]:
    operation_id = uuid.uuid4().hex
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.parent / f".{destination.name}.paper-{operation_id}.new"
    backup = (
        destination.parent / f".{destination.name}.paper-{operation_id}.bak"
        if destination.exists() else None
    )
    with staged.open("xb") as handle:
        handle.write(new_content)
        handle.flush()
        os.fsync(handle.fileno())
    if destination.exists():
        staged.chmod(destination.stat().st_mode & 0o777)
        assert backup is not None
        shutil.copy2(destination, backup)
    else:
        staged.chmod(0o644)
    payload = {"file": {
        "destination": str(destination), "staged": str(staged),
        "backup": str(backup) if backup else None,
        "old_hash": _optional_sha256(destination),
        "new_hash": hashlib.sha256(new_content).hexdigest(),
        "published": False,
    }}
    workspace_store.save_operation(
        connection, operation_id=operation_id, workspace_id=workspace.id,
        kind=kind, phase="prepared", payload=payload,
    )
    if _optional_sha256(destination) != payload["file"]["old_hash"]:
        raise ResourceError(f"override file changed before {kind}: {destination}")
    os.replace(staged, destination)
    payload["file"]["published"] = True
    workspace_store.save_operation(
        connection, operation_id=operation_id, workspace_id=workspace.id,
        kind=kind, phase="published", payload=payload,
    )
    return operation_id, payload


def _finish_file_operation(
    connection: sqlite3.Connection, operation_id: str, payload: dict[str, Any]
) -> None:
    item = payload["file"]
    if item.get("backup"):
        try:
            Path(item["backup"]).unlink(missing_ok=True)
        except OSError:
            return
    workspace_store.delete_operation(connection, operation_id)


def attach_workspace(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    inspection: buildroot.PackageInspection,
) -> AttachWorkspaceResult:
    recover_workspace_operations(connection, workspace)
    if inspection.source_fingerprint != workspace.source_fingerprint:
        raise ResourceError(
            "destination configuration resolves a different normal package source"
        )
    project_root = Path(workspace.project_root)
    existing = workspace_store.get_attachment(
        connection, workspace.project_root, inspection.buildroot_config,
        inspection.package,
    )
    commands = (
        f"just build {inspection.buildroot_config} {inspection.package}-rebuild",
        f"just build {inspection.buildroot_config} {inspection.package}-reconfigure",
    )
    if existing is not None:
        if existing["workspace_id"] != workspace.id:
            raise ResourceError(
                "a different manager-owned workspace is attached; detach it first"
            )
        override = Path(existing["override_path"])
        content = override.read_bytes() if override.is_file() else b""
        block = bytes(existing["managed_block"])
        if content.count(block) != 1:
            raise ResourceError("managed override block was changed manually")
        effective = buildroot.effective_source_override(
            project_root, inspection.buildroot_config, inspection.variable_prefix
        )
        if effective != workspace.path:
            raise ResourceError("recorded attachment is not the effective Buildroot override")
        return AttachWorkspaceResult(
            False, workspace.package, workspace.name,
            inspection.buildroot_config, str(override), workspace.path,
            existing["prior_effective"] is not None, False, (), commands,
        )
    override = Path(inspection.override_file)
    original_exists = override.is_file()
    original = override.read_bytes() if original_exists else b""
    block_id = uuid.uuid4().hex
    block = _managed_block(
        block_id, inspection.variable_prefix, workspace.path,
        bool(original and not original.endswith(b"\n")),
    )
    if block in original or b"# >>> paper-resources workspace " in original and (
        f"{inspection.variable_prefix}_OVERRIDE_SRCDIR".encode() in original
    ):
        raise ResourceError("override file already contains an unrecorded manager block")
    shadowed = inspection.effective_override is not None
    operation_id, payload = _publish_override_change(
        connection, workspace, kind="attach", destination=override,
        new_content=original + block,
    )
    try:
        effective = buildroot.effective_source_override(
            project_root, inspection.buildroot_config, inspection.variable_prefix
        )
        if effective != workspace.path:
            raise ResourceError(
                f"managed override was not effective (Buildroot reports {effective!r})"
            )
        workspace_store.complete_attachment_operation(
            connection, operation_id=operation_id, payload=payload, attach=True,
            project_root=workspace.project_root,
            buildroot_config=inspection.buildroot_config,
            package=inspection.package, workspace_id=workspace.id,
            override_path=str(override), block_id=block_id,
            managed_block=block, file_created=not original_exists,
            prior_effective=inspection.effective_override,
        )
    except Exception:
        operation = next(
            item for item in workspace_store.pending_operations(connection, workspace.id)
            if item["id"] == operation_id
        )
        _recover_file_operation(connection, operation)
        raise
    _finish_file_operation(connection, operation_id, payload)
    warnings = [
        "Buildroot rsync -au may retain deleted files or timestamp effects; clean the package build directory when needed",
        "workspace attachment bypasses normal download, extraction, patching, and omitted preparation hooks",
    ]
    if shadowed:
        warnings.append("the managed block shadows a preexisting unmanaged override")
    recorded_stack = [
        (item["path"], item["sha256"])
        for item in workspace.inspection.get("editable_patches", [])
    ]
    current_stack = [(item.path, item.sha256) for item in inspection.editable_patches]
    differs = recorded_stack != current_stack
    if differs:
        warnings.append(
            "destination configuration has a different patch stack; attachment uses the workspace as-is"
        )
    return AttachWorkspaceResult(
        True, workspace.package, workspace.name, inspection.buildroot_config,
        str(override), workspace.path, shadowed, differs, tuple(warnings), commands,
    )


def detach_workspace(
    connection: sqlite3.Connection,
    *,
    project_root: str,
    buildroot_config: str,
    package: str,
    workspace: workspace_store.WorkspaceRecord | None = None,
) -> DetachWorkspaceResult:
    attachment = workspace_store.get_attachment(
        connection, project_root, buildroot_config, package
    )
    commands = (
        f"just build {buildroot_config} {package}-dirclean",
        f"just build {buildroot_config} {package}",
    )
    if attachment is None:
        return DetachWorkspaceResult(False, package, buildroot_config, None, (), commands)
    if workspace is None:
        workspace = workspace_store.get_workspace_by_id(
            connection, attachment["workspace_id"]
        )
    if workspace is None:
        raise ResourceError("attachment refers to an unavailable workspace record")
    recover_workspace_operations(connection, workspace)
    override = Path(attachment["override_path"])
    if not override.is_file():
        raise ResourceError("managed override file was removed externally")
    content = override.read_bytes()
    block = bytes(attachment["managed_block"])
    if content.count(block) != 1:
        raise ResourceError("managed override block was changed manually")
    restored = content.replace(block, b"", 1)
    operation_id, payload = _publish_override_change(
        connection, workspace, kind="detach", destination=override,
        new_content=restored,
    )
    try:
        if not restored and attachment["file_created"]:
            if _sha256_hex(override) != hashlib.sha256(restored).hexdigest():
                raise ResourceError("override file changed before removing empty owned file")
            override.unlink()
            payload["file"]["remove_destination"] = True
            workspace_store.save_operation(
                connection, operation_id=operation_id, workspace_id=workspace.id,
                kind="detach", phase="published", payload=payload,
            )
        effective = buildroot.effective_source_override(
            Path(project_root), buildroot_config,
            package.replace("-", "_").upper(),
        )
        if effective != attachment["prior_effective"]:
            raise ResourceError(
                f"detached override did not restore the prior effective value "
                f"(expected {attachment['prior_effective']!r}, got {effective!r})"
            )
        workspace_store.complete_attachment_operation(
            connection, operation_id=operation_id, payload=payload, attach=False,
            project_root=project_root, buildroot_config=buildroot_config,
            package=package, workspace_id=workspace.id,
            override_path=str(override), block_id=attachment["block_id"],
            managed_block=block, file_created=bool(attachment["file_created"]),
            prior_effective=attachment["prior_effective"],
        )
    except Exception:
        operation = next(
            item for item in workspace_store.pending_operations(connection, workspace.id)
            if item["id"] == operation_id
        )
        _recover_file_operation(connection, operation)
        raise
    _finish_file_operation(connection, operation_id, payload)
    return DetachWorkspaceResult(
        True, package, buildroot_config, str(override),
        (
            "detaching does not clean build outputs; validate exported patches with a fresh normal patch/build cycle",
        ),
        commands,
    )


def _verify_attachment_block(attachment: sqlite3.Row) -> None:
    override = Path(attachment["override_path"])
    if not override.is_file():
        raise ResourceError(
            f"cannot close workspace: managed override was removed: {override}"
        )
    block = bytes(attachment["managed_block"])
    if override.read_bytes().count(block) != 1:
        raise ResourceError(
            f"cannot close workspace: managed override block changed: {override}"
        )


def _common_git_directory(checkout: Path) -> Path | None:
    value = _git_optional([
        "-C", str(checkout), "rev-parse", "--path-format=absolute",
        "--git-common-dir",
    ])
    return Path(value).resolve() if value else None


def _registered_worktree_paths(repository_path: Path) -> set[Path]:
    value = git_resources.run_git([
        "--git-dir", str(repository_path), "worktree", "list", "--porcelain",
    ], capture=True)
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in value.splitlines() if line.startswith("worktree ")
    }


def close_workspace(
    connection: sqlite3.Connection,
    workspace: workspace_store.WorkspaceRecord,
    *,
    force: bool = False,
) -> CloseWorkspaceResult:
    pending = workspace_store.pending_operations(connection, workspace.id)
    close_operation = next(
        (item for item in pending if item["operation_kind"] == "close"), None
    )
    for operation in pending:
        if operation is close_operation:
            continue
        if operation["operation_kind"] == "export":
            if operation["phase"] == "complete":
                workspace_store.delete_operation(connection, operation["id"])
            else:
                _recover_export_operation(connection, workspace, operation)
        elif operation["operation_kind"] in {"attach", "detach"}:
            if operation["phase"] == "complete":
                workspace_store.delete_operation(connection, operation["id"])
            else:
                _recover_file_operation(connection, operation)
        else:
            raise ResourceError(
                f"workspace has unresolved {operation['operation_kind']} operation"
            )
    repository_path = Path(workspace.repository_path)
    checkout = Path(workspace.path)
    if not repository_path.is_absolute():
        repository_path = checkout.parents[2] / repository_path
    detached: list[str] = []
    discarded = False
    if close_operation is None:
        status = get_workspace_status(connection, workspace)
        if not force:
            reasons = list(status.blockers)
            if not status.exported:
                reasons.append("workspace commits, notes, or patch outputs are not exported")
            if status.changed_export_paths:
                reasons.append(
                    "recorded patch outputs changed: "
                    + ", ".join(status.changed_export_paths)
                )
            if reasons:
                raise ResourceError("cannot close workspace: " + "; ".join(reasons))
        discarded = force and (not status.exported or not status.clean)
        attachments = connection.execute(
            """
            SELECT * FROM workspace_attachments
            WHERE workspace_id = ? ORDER BY buildroot_config, package
            """,
            (workspace.id,),
        ).fetchall()
        # Preflight every restoration before changing any attachment.
        for attachment in attachments:
            _verify_attachment_block(attachment)
        for attachment in attachments:
            result = detach_workspace(
                connection, project_root=attachment["project_root"],
                buildroot_config=attachment["buildroot_config"],
                package=attachment["package"], workspace=workspace,
            )
            if result.detached:
                detached.append(
                    f"{attachment['buildroot_config']}:{attachment['package']}"
                )
        operation_id = uuid.uuid4().hex
        payload = {
            "force": force, "detached": detached,
            "discarded": discarded, "phase": "prepared",
        }
        workspace_store.save_operation(
            connection, operation_id=operation_id, workspace_id=workspace.id,
            kind="close", phase="prepared", payload=payload,
        )
        phase = "prepared"
    else:
        operation_id = close_operation["id"]
        payload = json.loads(close_operation["payload"])
        force = bool(payload["force"])
        detached = list(payload.get("detached", []))
        discarded = bool(payload.get("discarded", False))
        phase = close_operation["phase"]

    if phase == "prepared":
        common = _common_git_directory(checkout)
        registered = _registered_worktree_paths(repository_path)
        if common is None and checkout.resolve() not in registered:
            # A previous attempt removed the worktree before persisting phase.
            pass
        elif common != repository_path.resolve():
            raise ResourceError(
                "refusing to remove a path not owned by the recorded shared repository"
            )
        else:
            git_resources.run_git([
                "--git-dir", str(repository_path), "worktree", "remove", "--force",
                str(checkout),
            ])
        payload["phase"] = "worktree-removed"
        workspace_store.save_operation(
            connection, operation_id=operation_id, workspace_id=workspace.id,
            kind="close", phase="worktree-removed", payload=payload,
        )
        phase = "worktree-removed"
    if phase == "worktree-removed":
        git_resources.run_git([
            "--git-dir", str(repository_path), "update-ref", "-d",
            f"refs/heads/{workspace.branch}",
        ])
        git_resources.run_git([
            "--git-dir", str(repository_path), "update-ref", "-d",
            workspace.notes_ref,
        ])
        payload["phase"] = "refs-removed"
        workspace_store.save_operation(
            connection, operation_id=operation_id, workspace_id=workspace.id,
            kind="close", phase="refs-removed", payload=payload,
        )
    workspace_store.complete_close_operation(
        connection, workspace.id, operation_id
    )
    fingerprint = workspace.input_fingerprint
    return CloseWorkspaceResult(
        workspace.package, workspace.name, workspace.path, force,
        tuple(detached), discarded,
        (
            _revision_id(workspace.package, fingerprint, "base"),
            _revision_id(workspace.package, fingerprint, "imported"),
        ),
        workspace.branch, workspace.notes_ref,
    )
