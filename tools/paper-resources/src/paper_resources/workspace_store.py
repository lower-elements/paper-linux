"""Durable instance-local state for package patch workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterable

from . import git_resources
from .config import ResourceError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class LocalRevision:
    repository_id: str
    repository_path: str
    revision_id: str
    commit: str
    tree: str
    parent_commit: str | None
    description: str
    author: str
    index: bool
    construction_policy: str
    input_fingerprint: str
    provenance: dict[str, Any]
    created_at: str

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.revision_id,
            "description": self.description,
            "author": self.author,
            "index": self.index,
            "tags": ["local", "workspace-input"],
            "commit": self.commit,
            "tree": self.tree,
            "worktrees": [],
            "local": True,
            "construction_policy": self.construction_policy,
            "input_fingerprint": self.input_fingerprint,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    package: str
    name: str
    repository_id: str
    repository_path: str
    path: str
    branch: str
    notes_ref: str
    project_root: str
    origin_config: str
    input_fingerprint: str
    source_fingerprint: str
    export_base: str
    imported_tip: str
    state: str
    inspection: dict[str, Any]
    created_at: str
    retired_at: str | None


def _workspace(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=row["id"], package=row["package"], name=row["name"],
        repository_id=row["repository_id"],
        repository_path=row["repository_path"], path=row["path"],
        branch=row["branch"], notes_ref=row["notes_ref"],
        project_root=row["project_root"], origin_config=row["origin_config"],
        input_fingerprint=row["input_fingerprint"],
        source_fingerprint=row["source_fingerprint"],
        export_base=git_resources.oid_to_hex(row["export_base_oid"]),
        imported_tip=git_resources.oid_to_hex(row["imported_tip_oid"]),
        state=row["state"], inspection=json.loads(row["inspection"]),
        created_at=row["created_at"], retired_at=row["retired_at"],
    )


def get_workspace(
    connection: sqlite3.Connection, package: str, name: str
) -> WorkspaceRecord | None:
    row = connection.execute(
        "SELECT * FROM workspaces WHERE package = ? AND name = ? AND retired_at IS NULL",
        (package, name),
    ).fetchone()
    return _workspace(row) if row is not None else None


def get_workspace_by_id(
    connection: sqlite3.Connection, workspace_id: str
) -> WorkspaceRecord | None:
    row = connection.execute(
        "SELECT * FROM workspaces WHERE id = ? AND retired_at IS NULL",
        (workspace_id,),
    ).fetchone()
    return _workspace(row) if row is not None else None


def list_workspaces(
    connection: sqlite3.Connection,
    *,
    package: str | None = None,
    buildroot_config: str | None = None,
) -> list[WorkspaceRecord]:
    clauses = ["retired_at IS NULL"]
    parameters: list[str] = []
    if package is not None:
        clauses.append("package = ?")
        parameters.append(package)
    if buildroot_config is not None:
        clauses.append("origin_config = ?")
        parameters.append(buildroot_config)
    rows = connection.execute(
        "SELECT * FROM workspaces WHERE " + " AND ".join(clauses)
        + " ORDER BY package, name",
        parameters,
    ).fetchall()
    return [_workspace(row) for row in rows]


def create_workspace(
    connection: sqlite3.Connection, workspace: WorkspaceRecord
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO workspaces(
                id, package, name, repository_id, repository_path, path,
                branch, notes_ref, project_root, origin_config,
                input_fingerprint, source_fingerprint, export_base_oid,
                imported_tip_oid, state, inspection, created_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace.id, workspace.package, workspace.name,
                workspace.repository_id, workspace.repository_path,
                workspace.path, workspace.branch, workspace.notes_ref,
                workspace.project_root, workspace.origin_config,
                workspace.input_fingerprint, workspace.source_fingerprint,
                git_resources.oid_from_hex(workspace.export_base),
                git_resources.oid_from_hex(workspace.imported_tip),
                workspace.state, json_text(workspace.inspection),
                workspace.created_at, workspace.retired_at,
            ),
        )


def latest_export(
    connection: sqlite3.Connection, workspace_id: str
) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
    export = connection.execute(
        """
        SELECT * FROM workspace_exports
        WHERE workspace_id = ? ORDER BY id DESC LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    if export is None:
        return None
    paths = connection.execute(
        """
        SELECT * FROM workspace_export_paths
        WHERE export_id = ? ORDER BY ordinal
        """,
        (export["id"],),
    ).fetchall()
    return export, paths


def save_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    workspace_id: str | None,
    kind: str,
    phase: str,
    payload: Any,
    error: str | None = None,
) -> None:
    timestamp = now()
    with connection:
        connection.execute(
            """
            INSERT INTO workspace_operations(
                id, workspace_id, operation_kind, phase, payload, error,
                started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                phase = excluded.phase,
                payload = excluded.payload,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                operation_id, workspace_id, kind, phase, json_text(payload),
                error, timestamp, timestamp,
            ),
        )


def pending_operations(
    connection: sqlite3.Connection, workspace_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM workspace_operations
        WHERE workspace_id = ? ORDER BY started_at, id
        """,
        (workspace_id,),
    ).fetchall()


def delete_operation(connection: sqlite3.Connection, operation_id: str) -> None:
    with connection:
        connection.execute(
            "DELETE FROM workspace_operations WHERE id = ?", (operation_id,)
        )


def get_attachment(
    connection: sqlite3.Connection,
    project_root: str,
    buildroot_config: str,
    package: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM workspace_attachments
        WHERE project_root = ? AND buildroot_config = ? AND package = ?
        """,
        (project_root, buildroot_config, package),
    ).fetchone()


def save_attachment(
    connection: sqlite3.Connection,
    *,
    project_root: str,
    buildroot_config: str,
    package: str,
    workspace_id: str,
    override_path: str,
    block_id: str,
    managed_block: bytes,
    file_created: bool,
    prior_effective: str | None = None,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO workspace_attachments(
                project_root, buildroot_config, package, workspace_id,
                override_path, block_id, managed_block, file_created,
                prior_effective, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_root, buildroot_config, package, workspace_id,
                override_path, block_id, managed_block, int(file_created),
                prior_effective, now(),
            ),
        )


def delete_attachment(
    connection: sqlite3.Connection,
    project_root: str,
    buildroot_config: str,
    package: str,
) -> None:
    with connection:
        connection.execute(
            """
            DELETE FROM workspace_attachments
            WHERE project_root = ? AND buildroot_config = ? AND package = ?
            """,
            (project_root, buildroot_config, package),
        )


def retire_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    with connection:
        connection.execute(
            """
            UPDATE workspaces SET state = 'closed', retired_at = ? WHERE id = ?
            """,
            (now(), workspace_id),
        )


def complete_attachment_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    payload: Any,
    attach: bool,
    project_root: str,
    buildroot_config: str,
    package: str,
    workspace_id: str,
    override_path: str,
    block_id: str,
    managed_block: bytes,
    file_created: bool,
    prior_effective: str | None,
) -> None:
    with connection:
        if attach:
            connection.execute(
                """
                INSERT INTO workspace_attachments(
                    project_root, buildroot_config, package, workspace_id,
                    override_path, block_id, managed_block, file_created,
                    prior_effective, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_root, buildroot_config, package, workspace_id,
                    override_path, block_id, managed_block, int(file_created),
                    prior_effective, now(),
                ),
            )
        else:
            connection.execute(
                """
                DELETE FROM workspace_attachments
                WHERE project_root = ? AND buildroot_config = ? AND package = ?
                """,
                (project_root, buildroot_config, package),
            )
        connection.execute(
            """
            UPDATE workspace_operations
            SET phase = 'complete', payload = ?, error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (json_text(payload), now(), operation_id),
        )


def complete_close_operation(
    connection: sqlite3.Connection, workspace_id: str, operation_id: str
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE workspaces SET state = 'closed', retired_at = ? WHERE id = ?
            """,
            (now(), workspace_id),
        )
        connection.execute(
            "DELETE FROM workspace_operations WHERE id = ?", (operation_id,)
        )


def _local_revision(row: sqlite3.Row) -> LocalRevision:
    return LocalRevision(
        repository_id=row["repository_id"],
        repository_path=row["repository_path"],
        revision_id=row["revision_id"],
        commit=git_resources.oid_to_hex(row["commit_oid"]),
        tree=git_resources.oid_to_hex(row["tree_oid"]),
        parent_commit=(
            git_resources.oid_to_hex(row["parent_commit_oid"])
            if row["parent_commit_oid"] is not None else None
        ),
        description=row["description"],
        author=row["author"],
        index=bool(row["index_enabled"]),
        construction_policy=row["construction_policy"],
        input_fingerprint=row["input_fingerprint"],
        provenance=json.loads(row["provenance"]),
        created_at=row["created_at"],
    )


def list_local_revisions(connection: sqlite3.Connection) -> list[LocalRevision]:
    rows = connection.execute(
        "SELECT * FROM local_revision_definitions ORDER BY repository_id, revision_id"
    ).fetchall()
    return [_local_revision(row) for row in rows]


def register_local_revision(
    connection: sqlite3.Connection,
    revision: LocalRevision,
) -> None:
    values = (
        revision.repository_id,
        revision.repository_path,
        revision.revision_id,
        git_resources.oid_from_hex(revision.commit),
        git_resources.oid_from_hex(revision.tree),
        git_resources.oid_from_hex(revision.parent_commit)
        if revision.parent_commit else None,
        revision.description,
        revision.author,
        int(revision.index),
        revision.construction_policy,
        revision.input_fingerprint,
        json_text(revision.provenance),
        revision.created_at,
    )
    with connection:
        existing = connection.execute(
            """
            SELECT commit_oid, tree_oid, input_fingerprint
            FROM local_revision_definitions
            WHERE repository_id = ? AND revision_id = ?
            """,
            (revision.repository_id, revision.revision_id),
        ).fetchone()
        if existing is not None:
            identity = (
                git_resources.oid_to_hex(existing["commit_oid"]),
                git_resources.oid_to_hex(existing["tree_oid"]),
                existing["input_fingerprint"],
            )
            expected = (revision.commit, revision.tree, revision.input_fingerprint)
            if identity != expected:
                raise ResourceError(
                    f"local revision is immutable: {revision.repository_id}:{revision.revision_id}"
                )
            return
        connection.execute(
            """
            INSERT INTO local_revision_definitions(
                repository_id, repository_path, revision_id, commit_oid, tree_oid,
                parent_commit_oid, description, author, index_enabled,
                construction_policy, input_fingerprint, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO repositories(id, path) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET path = excluded.path
            """,
            (revision.repository_id, revision.repository_path),
        )
        connection.execute(
            """
            INSERT INTO repository_revisions(
                repository_id, id, commit_oid, tree_oid, author, description,
                index_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository_id, id) DO UPDATE SET
                commit_oid = excluded.commit_oid,
                tree_oid = excluded.tree_oid,
                author = excluded.author,
                description = excluded.description,
                index_enabled = excluded.index_enabled
            """,
            (
                revision.repository_id, revision.revision_id,
                git_resources.oid_from_hex(revision.commit),
                git_resources.oid_from_hex(revision.tree), revision.author,
                revision.description, int(revision.index),
            ),
        )


def save_source_binding(
    connection: sqlite3.Connection,
    *,
    project_root: str,
    buildroot_config: str,
    package: str,
    source_fingerprint: str,
    repository_id: str,
    revision_id: str,
    correspondence: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO package_source_bindings(
                project_root, buildroot_config, package, source_fingerprint,
                repository_id, revision_id, correspondence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_root, buildroot_config, package, source_fingerprint)
            DO UPDATE SET repository_id = excluded.repository_id,
                revision_id = excluded.revision_id,
                correspondence = excluded.correspondence
            """,
            (
                project_root, buildroot_config, package, source_fingerprint,
                repository_id, revision_id, correspondence, now(),
            ),
        )


def get_source_binding(
    connection: sqlite3.Connection,
    *,
    project_root: str,
    buildroot_config: str,
    package: str,
    source_fingerprint: str,
) -> tuple[str, str, str] | None:
    row = connection.execute(
        """
        SELECT repository_id, revision_id, correspondence
        FROM package_source_bindings
        WHERE project_root = ? AND buildroot_config = ? AND package = ?
          AND source_fingerprint = ?
        """,
        (project_root, buildroot_config, package, source_fingerprint),
    ).fetchone()
    if row is None:
        return None
    return row["repository_id"], row["revision_id"], row["correspondence"]


def record_export(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    kind: str,
    tip: str,
    notes_digest: str,
    verification: str,
    paths: Iterable[tuple[str, str, bytes]],
    obsolete_paths: Iterable[str] = (),
) -> int:
    with connection:
        return _record_export(
            connection, workspace_id=workspace_id, kind=kind, tip=tip,
            notes_digest=notes_digest, verification=verification, paths=paths,
            obsolete_paths=obsolete_paths,
        )


def _record_export(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    kind: str,
    tip: str,
    notes_digest: str,
    verification: str,
    paths: Iterable[tuple[str, str, bytes]],
    obsolete_paths: Iterable[str],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO workspace_exports(
            workspace_id, kind, tip_oid, notes_digest, verification,
            obsolete_paths, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workspace_id, kind, git_resources.oid_from_hex(tip), notes_digest,
            verification, json_text(list(obsolete_paths)), now(),
        ),
    )
    export_id = cursor.lastrowid
    assert export_id is not None
    connection.executemany(
        """
        INSERT INTO workspace_export_paths(
            export_id, ordinal, commit_oid, path, content_sha256
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                export_id, ordinal, git_resources.oid_from_hex(commit),
                path, digest,
            )
            for ordinal, (commit, path, digest) in enumerate(paths)
        ],
    )
    return export_id


def complete_export_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    payload: Any,
    workspace_id: str,
    kind: str,
    tip: str,
    notes_digest: str,
    verification: str,
    paths: Iterable[tuple[str, str, bytes]],
    obsolete_paths: Iterable[str],
) -> int:
    """Record an export and mark its journal complete in one transaction."""
    with connection:
        export_id = _record_export(
            connection, workspace_id=workspace_id, kind=kind, tip=tip,
            notes_digest=notes_digest, verification=verification, paths=paths,
            obsolete_paths=obsolete_paths,
        )
        connection.execute(
            """
            UPDATE workspace_operations
            SET phase = 'complete', payload = ?, error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (json_text(payload), now(), operation_id),
        )
    return export_id
