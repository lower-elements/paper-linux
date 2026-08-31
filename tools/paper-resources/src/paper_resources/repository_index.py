"""Manifest-backed repository catalog and Git object access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from . import git_resources
from .config import ResourceError


@dataclass(frozen=True, slots=True)
class RepositoryCatalogSummary:
    repositories: int
    revisions: int


@dataclass(frozen=True, slots=True)
class RevisionBlob:
    repository_id: str
    revision_id: str
    oid: bytes
    size: int
    mode: int
    path: str


def synchronize_catalog(
    connection: sqlite3.Connection, repositories: list[dict[str, Any]]
) -> RepositoryCatalogSummary:
    """Make the small manifest-backed repository catalog current."""
    repository_ids = {repository["id"] for repository in repositories}
    revision_count = sum(
        len(repository.get("revisions", [])) for repository in repositories
    )
    with connection:
        indexed_repository_ids = {
            row[0] for row in connection.execute("SELECT id FROM repositories")
        }
        connection.executemany(
            "DELETE FROM repositories WHERE id = ?",
            [(item,) for item in sorted(indexed_repository_ids - repository_ids)],
        )

        for repository in repositories:
            repository_id = repository["id"]
            connection.execute(
                """
                INSERT INTO repositories(id, path) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET path = excluded.path
                """,
                (repository_id, repository["path"]),
            )

            revisions = repository.get("revisions", [])
            revision_ids = {revision["id"] for revision in revisions}
            indexed_revision_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM repository_revisions WHERE repository_id = ?",
                    (repository_id,),
                )
            }
            connection.executemany(
                "DELETE FROM repository_revisions WHERE repository_id = ? AND id = ?",
                [
                    (repository_id, revision_id)
                    for revision_id in sorted(indexed_revision_ids - revision_ids)
                ],
            )

            for revision in revisions:
                revision_id = revision["id"]
                commit_oid = git_resources.oid_from_hex(revision["commit"])
                tree_oid = git_resources.oid_from_hex(revision["tree"])
                existing = connection.execute(
                    """
                    SELECT commit_oid, tree_oid
                    FROM repository_revisions
                    WHERE repository_id = ? AND id = ?
                    """,
                    (repository_id, revision_id),
                ).fetchone()
                if existing is not None and (
                    existing["commit_oid"] != commit_oid
                    or existing["tree_oid"] != tree_oid
                ):
                    connection.execute(
                        """
                        DELETE FROM repository_revisions
                        WHERE repository_id = ? AND id = ?
                        """,
                        (repository_id, revision_id),
                    )
                    existing = None
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO repository_revisions(
                            repository_id, id, commit_oid, tree_oid,
                            author, description
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repository_id,
                            revision_id,
                            commit_oid,
                            tree_oid,
                            revision.get("author"),
                            revision.get("description"),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE repository_revisions
                        SET author = ?, description = ?
                        WHERE repository_id = ? AND id = ?
                        """,
                        (
                            revision.get("author"),
                            revision.get("description"),
                            repository_id,
                            revision_id,
                        ),
                    )

    return RepositoryCatalogSummary(len(repositories), revision_count)


def iter_revision_blobs(
    repository: dict[str, Any], revision: dict[str, Any], root: Path
) -> Iterator[RevisionBlob]:
    """Yield blob occurrences from one available, manifest-pinned revision."""
    repository_id = repository["id"]
    revision_id = revision["id"]
    repository_path = root / repository["path"]
    if not (repository_path / "HEAD").is_file():
        raise ResourceError(f"repository is not populated: {repository_id}")
    commit = git_resources.git_object(
        repository_path, f"{revision['commit']}^{{commit}}"
    )
    if commit is None:
        raise ResourceError(
            f"revision is not populated: {repository_id}:{revision_id}"
        )
    git_resources.verify_revision_objects(
        repository_path, repository_id, revision, commit
    )
    for blob in git_resources.iter_tree_blobs(repository_path, revision["tree"]):
        yield RevisionBlob(
            repository_id=repository_id,
            revision_id=revision_id,
            oid=blob.oid,
            size=blob.size,
            mode=blob.mode,
            path=blob.path,
        )


def register_blob(
    connection: sqlite3.Connection,
    repository_id: str,
    oid: bytes,
    size: int,
) -> bool:
    """Register an encountered blob, returning whether it was newly inserted."""
    oid_text = git_resources.oid_to_hex(oid)
    if size < 0:
        raise ResourceError(f"invalid size for Git blob {oid_text}: {size}")
    existing = connection.execute(
        """
        SELECT size FROM repository_blobs
        WHERE repository_id = ? AND oid = ?
        """,
        (repository_id, oid),
    ).fetchone()
    if existing is not None:
        if existing["size"] != size:
            raise ResourceError(
                f"Git blob size mismatch for {repository_id}:{oid_text} "
                f"(expected {existing['size']}, got {size})"
            )
        return False
    connection.execute(
        """
        INSERT INTO repository_blobs(repository_id, oid, size)
        VALUES (?, ?, ?)
        """,
        (repository_id, oid, size),
    )
    return True


def read_blob(
    repository: dict[str, Any], root: Path, oid: str | bytes
) -> bytes:
    """Read a blob from a populated manifest repository without a worktree."""
    repository_path = root / repository["path"]
    if not (repository_path / "HEAD").is_file():
        raise ResourceError(f"repository is not populated: {repository['id']}")
    return git_resources.read_blob(repository_path, oid)
