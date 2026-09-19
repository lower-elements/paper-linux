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
