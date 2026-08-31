"""Read-only navigation over the blob-oriented Ctags index."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import sqlite3
from typing import Any, Iterable

from .config import ResourceError
from .git_resources import oid_to_hex


MAX_SEARCH_LIMIT = 200
MAX_INSPECT_TAGS = 100


@dataclass(frozen=True, slots=True)
class CodeOccurrence:
    repository: str
    revision: str
    path: str
    blob_oid: str


@dataclass(frozen=True, slots=True)
class CodeTagSummary:
    tag_id: int
    name: str
    qualified_name: str | None
    language: str
    kind: str
    line_start: int
    line_end: int | None
    is_reference: bool
    occurrences: tuple[CodeOccurrence, ...]


@dataclass(frozen=True, slots=True)
class CodeTagInfo:
    tag_id: int
    name: str
    qualified_name: str | None
    language: str
    kind: str
    line_start: int
    line_end: int | None
    signature: str | None
    typeref: str | None
    access: str | None
    scope: str | None
    scope_kind: str | None
    nth: int | None
    is_file_restricted: bool
    is_reference: bool
    enclosing_tag_id: int | None
    roles: tuple[str, ...]
    metadata: dict[str, Any] | None
    occurrences: tuple[CodeOccurrence, ...]


@dataclass(frozen=True, slots=True)
class CodeTagSearch:
    results: tuple[CodeTagSummary, ...]
    next_cursor: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeIndexDescription:
    repositories: int
    revisions: int
    blobs: int
    analyses: int
    tags: int
    paths: int
    languages: tuple[tuple[str, int], ...]
    kinds: tuple[tuple[str, int], ...]


def _values(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _add_values(
    clauses: list[str], parameters: list[Any], column: str,
    value: str | list[str] | tuple[str, ...] | None,
) -> None:
    values = _values(value)
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    clauses.append(f"{column} IN ({placeholders})")
    parameters.extend(values)


def _encode_cursor(name: str, tag_id: int) -> str:
    payload = json.dumps([name, tag_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (
            not isinstance(value, list) or len(value) != 2
            or not isinstance(value[0], str) or not isinstance(value[1], int)
        ):
            raise ValueError
        return value[0], value[1]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResourceError("invalid code-tag search cursor") from error


def _occurrences(
    connection: sqlite3.Connection,
    tag_ids: Iterable[int],
    *,
    repository: str | list[str] | None = None,
    revision: str | list[str] | None = None,
    path: str | list[str] | None = None,
    path_prefix: str | None = None,
) -> dict[int, tuple[CodeOccurrence, ...]]:
    ids = tuple(tag_ids)
    if not ids:
        return {}
    clauses = [f"tag.id IN ({', '.join('?' for _ in ids)})"]
    parameters: list[Any] = list(ids)
    _add_values(clauses, parameters, "rp.repository_id", repository)
    _add_values(clauses, parameters, "rp.revision_id", revision)
    _add_values(clauses, parameters, "rp.path", path)
    if path_prefix is not None:
        clauses.append("rp.path LIKE ? ESCAPE '\\'")
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.append(f"{escaped}%")
    rows = connection.execute(
        f"""
        SELECT tag.id AS tag_id, rp.repository_id, rp.revision_id,
               rp.path, rp.blob_oid
        FROM ctags_tags AS tag
        JOIN ctags_analyses AS analysis ON analysis.id = tag.analysis_id
        JOIN ctags_revision_paths AS rp
          ON rp.repository_id = analysis.repository_id
         AND rp.blob_oid = analysis.blob_oid
        WHERE {' AND '.join(clauses)}
        ORDER BY tag.id, rp.repository_id, rp.revision_id, rp.path
        """,
        parameters,
    )
    grouped: dict[int, list[CodeOccurrence]] = {}
    for row in rows:
        grouped.setdefault(row["tag_id"], []).append(CodeOccurrence(
            repository=row["repository_id"],
            revision=row["revision_id"],
            path=row["path"],
            blob_oid=oid_to_hex(row["blob_oid"]),
        ))
    return {tag_id: tuple(items) for tag_id, items in grouped.items()}


def search_tags(
    connection: sqlite3.Connection,
    *,
    repository: str | list[str] | None = None,
    revision: str | list[str] | None = None,
    path: str | list[str] | None = None,
    path_prefix: str | None = None,
    blob_oid: bytes | None = None,
    tag_id: int | list[int] | None = None,
    name: str | list[str] | None = None,
    name_prefix: str | None = None,
    qualified_name: str | list[str] | None = None,
    qualified_name_prefix: str | None = None,
    language: str | list[str] | None = None,
    kind: str | list[str] | None = None,
    role: str | list[str] | None = None,
    access: str | list[str] | None = None,
    scope: str | list[str] | None = None,
    scope_kind: str | list[str] | None = None,
    enclosing_tag_id: int | None = None,
    is_reference: bool | None = None,
    is_file_restricted: bool | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> CodeTagSearch:
    """Find tags with AND between fields and OR within list-valued fields."""
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ResourceError(f"code-tag search limit must be between 1 and {MAX_SEARCH_LIMIT}")
    clauses = ["1"]
    parameters: list[Any] = []
    _add_values(clauses, parameters, "rp.repository_id", repository)
    _add_values(clauses, parameters, "rp.revision_id", revision)
    _add_values(clauses, parameters, "rp.path", path)
    _add_values(clauses, parameters, "tag.name", name)
    _add_values(clauses, parameters, "tag.qualified_name", qualified_name)
    _add_values(clauses, parameters, "parser.language", language)
    _add_values(clauses, parameters, "kind.name", kind)
    _add_values(clauses, parameters, "tag.access", access)
    _add_values(clauses, parameters, "tag.scope", scope)
    _add_values(clauses, parameters, "tag.scope_kind", scope_kind)
    if tag_id is not None:
        values = (tag_id,) if isinstance(tag_id, int) else tuple(tag_id)
        if values:
            clauses.append(f"tag.id IN ({', '.join('?' for _ in values)})")
            parameters.extend(values)
    if role is not None:
        values = _values(role)
        clauses.append(
            "EXISTS (SELECT 1 FROM ctags_tag_roles tr "
            "JOIN ctags_roles r ON r.id = tr.role_id "
            f"WHERE tr.tag_id = tag.id AND r.name IN ({', '.join('?' for _ in values)}))"
        )
        parameters.extend(values)
    if path_prefix is not None:
        clauses.append("rp.path LIKE ? ESCAPE '\\'")
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.append(f"{escaped}%")
    for column, prefix in (
        ("tag.name", name_prefix), ("tag.qualified_name", qualified_name_prefix)
    ):
        if prefix is not None:
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"{escaped}%")
    if blob_oid is not None:
        clauses.append("analysis.blob_oid = ?")
        parameters.append(blob_oid)
    for column, value in (
        ("tag.enclosing_tag_id", enclosing_tag_id),
        ("tag.is_reference", None if is_reference is None else int(is_reference)),
        ("tag.is_file_restricted", None if is_file_restricted is None else int(is_file_restricted)),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    if line_start is not None:
        clauses.append("coalesce(tag.line_end, tag.line_start) >= ?")
        parameters.append(line_start)
    if line_end is not None:
        clauses.append("tag.line_start <= ?")
        parameters.append(line_end)
    if cursor is not None:
        cursor_name, cursor_id = _decode_cursor(cursor)
        clauses.append("(tag.name, tag.id) > (?, ?)")
        parameters.extend((cursor_name, cursor_id))

    rows = connection.execute(
        f"""
        SELECT DISTINCT tag.id, tag.name, tag.qualified_name,
               parser.language, kind.name AS kind,
               tag.line_start, tag.line_end, tag.is_reference
        FROM ctags_tags AS tag
        JOIN ctags_analyses AS analysis ON analysis.id = tag.analysis_id
        JOIN ctags_parsers AS parser ON parser.id = tag.parser_id
        JOIN ctags_kinds AS kind ON kind.id = tag.kind_id
        JOIN ctags_revision_paths AS rp
          ON rp.repository_id = analysis.repository_id
         AND rp.blob_oid = analysis.blob_oid
        WHERE {' AND '.join(clauses)}
        ORDER BY tag.name, tag.id
        LIMIT ?
        """,
        [*parameters, limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    occurrences = _occurrences(
        connection, (row["id"] for row in rows), repository=repository,
        revision=revision, path=path, path_prefix=path_prefix,
    )
    results = tuple(CodeTagSummary(
        tag_id=row["id"], name=row["name"], qualified_name=row["qualified_name"],
        language=row["language"], kind=row["kind"],
        line_start=row["line_start"], line_end=row["line_end"],
        is_reference=bool(row["is_reference"]),
        occurrences=occurrences.get(row["id"], ()),
    ) for row in rows)
    next_cursor = _encode_cursor(rows[-1]["name"], rows[-1]["id"]) if truncated else None
    return CodeTagSearch(results, next_cursor, truncated)


def inspect_tags(
    connection: sqlite3.Connection, tag_ids: list[int] | tuple[int, ...]
) -> tuple[CodeTagInfo, ...]:
    """Return complete normalized index information for specific tag IDs."""
    ids = tuple(dict.fromkeys(tag_ids))
    if not ids:
        return ()
    if len(ids) > MAX_INSPECT_TAGS:
        raise ResourceError(f"at most {MAX_INSPECT_TAGS} tags may be inspected at once")
    rows = connection.execute(
        f"""
        SELECT tag.*, parser.language, kind.name AS kind,
               CASE WHEN tag.metadata IS NULL THEN NULL ELSE json(tag.metadata) END AS metadata_json
        FROM ctags_tags AS tag
        JOIN ctags_parsers AS parser ON parser.id = tag.parser_id
        JOIN ctags_kinds AS kind ON kind.id = tag.kind_id
        WHERE tag.id IN ({', '.join('?' for _ in ids)})
        """,
        ids,
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    missing = [tag_id for tag_id in ids if tag_id not in by_id]
    if missing:
        raise ResourceError(f"unknown code tag ID(s): {', '.join(map(str, missing))}")
    role_rows = connection.execute(
        f"""
        SELECT tr.tag_id, role.name
        FROM ctags_tag_roles AS tr
        JOIN ctags_roles AS role ON role.id = tr.role_id
        WHERE tr.tag_id IN ({', '.join('?' for _ in ids)})
        ORDER BY tr.tag_id, role.name
        """,
        ids,
    )
    roles: dict[int, list[str]] = {}
    for row in role_rows:
        roles.setdefault(row["tag_id"], []).append(row["name"])
    occurrences = _occurrences(connection, ids)
    return tuple(CodeTagInfo(
        tag_id=tag_id, name=by_id[tag_id]["name"],
        qualified_name=by_id[tag_id]["qualified_name"],
        language=by_id[tag_id]["language"], kind=by_id[tag_id]["kind"],
        line_start=by_id[tag_id]["line_start"], line_end=by_id[tag_id]["line_end"],
        signature=by_id[tag_id]["signature"], typeref=by_id[tag_id]["typeref"],
        access=by_id[tag_id]["access"], scope=by_id[tag_id]["scope"],
        scope_kind=by_id[tag_id]["scope_kind"], nth=by_id[tag_id]["nth"],
        is_file_restricted=bool(by_id[tag_id]["is_file_restricted"]),
        is_reference=bool(by_id[tag_id]["is_reference"]),
        enclosing_tag_id=by_id[tag_id]["enclosing_tag_id"],
        roles=tuple(roles.get(tag_id, ())),
        metadata=json.loads(by_id[tag_id]["metadata_json"])
        if by_id[tag_id]["metadata_json"] is not None else None,
        occurrences=occurrences.get(tag_id, ()),
    ) for tag_id in ids)


def describe_index(connection: sqlite3.Connection) -> CodeIndexDescription:
    """Return compact index coverage and useful query facets."""
    counts = {
        name: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for name, table in (
            ("repositories", "repositories"), ("revisions", "repository_revisions"),
            ("blobs", "repository_blobs"), ("analyses", "ctags_analyses"),
            ("tags", "ctags_tags"), ("paths", "ctags_revision_paths"),
        )
    }
    languages = tuple(
        (row["language"], row["total"])
        for row in connection.execute(
            """SELECT parser.language, count(*) AS total FROM ctags_tags AS tag
               JOIN ctags_parsers AS parser ON parser.id = tag.parser_id
               GROUP BY parser.language ORDER BY total DESC, parser.language"""
        )
    )
    kinds = tuple(
        (row["kind"], row["total"])
        for row in connection.execute(
            """SELECT kind.name AS kind, count(*) AS total FROM ctags_tags AS tag
               JOIN ctags_kinds AS kind ON kind.id = tag.kind_id
               GROUP BY kind.name ORDER BY total DESC, kind.name"""
        )
    )
    return CodeIndexDescription(**counts, languages=languages, kinds=kinds)
