"""Read-only navigation over the blob-oriented Ctags index."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import difflib
import json
import sqlite3
from typing import Any, Callable, Iterable

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


@dataclass(frozen=True, slots=True)
class CodeFile:
    repository: str
    revision: str
    path: str
    blob_oid: str
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class CodeOutlineItem:
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
    is_reference: bool
    enclosing_tag_id: int | None


@dataclass(frozen=True, slots=True)
class CodeFileOutline:
    file: CodeFile
    items: tuple[CodeOutlineItem, ...]
    total: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeScopeOutline:
    scope: CodeTagInfo
    children: tuple[CodeOutlineItem, ...]
    total: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeTagBounds:
    tag_id: int
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class CodeSourceRegion:
    file: CodeFile
    line_start: int
    line_end: int
    tags: tuple[CodeTagBounds, ...]
    source: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeSourceBatch:
    regions: tuple[CodeSourceRegion, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeFileSource:
    file: CodeFile
    line_start: int
    line_end: int
    source: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeEnclosingScope:
    requested_tag_id: int
    scope_tag_id: int | None
    resolution: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class CodeEnclosingSource:
    scopes: tuple[CodeEnclosingScope, ...]
    source: CodeSourceBatch


@dataclass(frozen=True, slots=True)
class CodeTagDiff:
    from_tag_id: int
    to_tag_id: int
    from_file: CodeFile
    to_file: CodeFile
    diff: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class CodeLineLocation:
    file: CodeFile
    line: int
    containing: tuple[CodeOutlineItem, ...]
    enclosing_chain: tuple[CodeOutlineItem, ...]
    nearby: tuple[CodeOutlineItem, ...]


@dataclass(frozen=True, slots=True)
class CodeOutlineChange:
    status: str
    symbol: str
    kind: str
    from_tag_id: int | None
    to_tag_id: int | None


@dataclass(frozen=True, slots=True)
class CodeOutlineComparison:
    from_file: CodeFile
    to_file: CodeFile
    changes: tuple[CodeOutlineChange, ...]
    status_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class CodeHistoryStep:
    revisions: tuple[str, ...]
    matches: tuple[CodeTagSummary, ...]
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class CodeSymbolHistory:
    repository: str
    symbol: str
    steps: tuple[CodeHistoryStep, ...]


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


def resolve_file(
    connection: sqlite3.Connection, repository: str, revision: str, path: str
) -> tuple[CodeFile, int]:
    row = connection.execute(
        """
        SELECT rp.blob_oid, analysis.id AS analysis_id
        FROM ctags_revision_paths AS rp
        JOIN ctags_analyses AS analysis
          ON analysis.repository_id = rp.repository_id
         AND analysis.blob_oid = rp.blob_oid
        WHERE rp.repository_id = ? AND rp.revision_id = ? AND rp.path = ?
        """,
        (repository, revision, path),
    ).fetchone()
    if row is None:
        raise ResourceError(
            f"file is not present in the code index: {repository}:{revision}:{path}"
        )
    return CodeFile(repository, revision, path, oid_to_hex(row["blob_oid"])), row["analysis_id"]


def _outline_items(rows: Iterable[sqlite3.Row]) -> tuple[CodeOutlineItem, ...]:
    return tuple(CodeOutlineItem(
        tag_id=row["id"], name=row["name"], qualified_name=row["qualified_name"],
        language=row["language"], kind=row["kind"],
        line_start=row["line_start"], line_end=row["line_end"],
        signature=row["signature"], typeref=row["typeref"], access=row["access"],
        is_reference=bool(row["is_reference"]),
        enclosing_tag_id=row["enclosing_tag_id"],
    ) for row in rows)


def _outline_query(
    connection: sqlite3.Connection,
    where: str,
    parameters: tuple[Any, ...],
    *,
    include_references: bool,
    limit: int,
) -> tuple[tuple[CodeOutlineItem, ...], int, bool]:
    if not 1 <= limit <= 5000:
        raise ResourceError("outline limit must be between 1 and 5000")
    reference_clause = "" if include_references else "AND tag.is_reference = 0"
    rows = connection.execute(
        f"""
        SELECT tag.id, tag.name, tag.qualified_name, parser.language,
               kind.name AS kind, tag.line_start, tag.line_end,
               tag.signature, tag.typeref, tag.access, tag.is_reference,
               tag.enclosing_tag_id,
               count(*) OVER () AS full_count
        FROM ctags_tags AS tag
        JOIN ctags_parsers AS parser ON parser.id = tag.parser_id
        JOIN ctags_kinds AS kind ON kind.id = tag.kind_id
        WHERE {where} {reference_clause}
        ORDER BY tag.line_start, coalesce(tag.line_end, tag.line_start) DESC,
                 tag.ordinal
        LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    total = rows[0]["full_count"] if rows else 0
    return _outline_items(rows[:limit]), total, len(rows) > limit


def outline_file(
    connection: sqlite3.Connection,
    repository: str,
    revision: str,
    path: str,
    *,
    include_references: bool = False,
    limit: int = 1000,
) -> CodeFileOutline:
    """Return a compact, source-ordered outline of one indexed file."""
    file, analysis_id = resolve_file(connection, repository, revision, path)
    items, total, truncated = _outline_query(
        connection, "tag.analysis_id = ?", (analysis_id,),
        include_references=include_references, limit=limit,
    )
    return CodeFileOutline(file, items, total, truncated)


def outline_scope(
    connection: sqlite3.Connection,
    tag_id: int,
    *,
    include_references: bool = False,
    limit: int = 1000,
) -> CodeScopeOutline:
    """Return the direct indexed children of one enclosing tag."""
    scope = inspect_tags(connection, [tag_id])[0]
    children, total, truncated = _outline_query(
        connection, "tag.enclosing_tag_id = ?", (tag_id,),
        include_references=include_references, limit=limit,
    )
    return CodeScopeOutline(scope, children, total, truncated)


def _source_lines(content: bytes) -> list[str]:
    return content.decode("utf-8", errors="replace").splitlines()


def _format_lines(lines: list[str], first_line: int, numbered: bool) -> str:
    if not numbered:
        return "\n".join(lines)
    width = max(1, len(str(first_line + len(lines) - 1)))
    return "\n".join(
        f"{number:>{width}} | {line}"
        for number, line in enumerate(lines, first_line)
    )


def read_file_source(
    connection: sqlite3.Connection,
    repository: str,
    revision: str,
    path: str,
    read_blob: Callable[[str, bytes], bytes],
    *,
    line_start: int = 1,
    line_end: int | None = None,
    numbered: bool = True,
    max_lines: int = 5000,
    max_chars: int = 200_000,
    warning: str | None = None,
) -> CodeFileSource:
    """Read a bounded line range from the indexed Git blob for one file."""
    if line_start < 1:
        raise ResourceError("source line start must be at least 1")
    if line_end is not None and line_end < line_start:
        raise ResourceError("source line end must not precede its start")
    if max_lines < 1 or max_chars < 1:
        raise ResourceError("source output limits must be positive")
    file, _analysis_id = resolve_file(connection, repository, revision, path)
    file = CodeFile(file.repository, file.revision, file.path, file.blob_oid, warning)
    lines = _source_lines(read_blob(repository, bytes.fromhex(file.blob_oid)))
    if line_start > len(lines):
        raise ResourceError(
            f"source line {line_start} is beyond the end of {repository}:{revision}:{path}"
        )
    requested_end = len(lines) if line_end is None else min(line_end, len(lines))
    actual_end = min(requested_end, line_start + max_lines - 1)
    selected = lines[line_start - 1:actual_end]
    source = _format_lines(selected, line_start, numbered)
    char_truncated = len(source) > max_chars
    if char_truncated:
        source = source[:max_chars]
    truncated = actual_end < requested_end or char_truncated
    return CodeFileSource(file, line_start, actual_end, source, truncated)


def read_tagged_source(
    connection: sqlite3.Connection,
    tag_ids: list[int] | tuple[int, ...],
    read_blob: Callable[[str, bytes], bytes],
    *,
    repository: str | None = None,
    revision: str | None = None,
    path: str | None = None,
    context_lines: int = 0,
    numbered: bool = True,
    max_lines: int = 5000,
    max_chars: int = 200_000,
) -> CodeSourceBatch:
    """Read tag regions, merging overlap and reading each Git blob once."""
    if context_lines < 0:
        raise ResourceError("source context lines must be at least 0")
    if max_lines < 1 or max_chars < 1:
        raise ResourceError("source output limits must be positive")
    tags = inspect_tags(connection, tag_ids)
    grouped: dict[tuple[str, str], list[tuple[CodeTagInfo, CodeOccurrence]]] = {}
    for tag in tags:
        matching = tuple(
            occurrence for occurrence in tag.occurrences
            if (repository is None or occurrence.repository == repository)
            and (revision is None or occurrence.revision == revision)
            and (path is None or occurrence.path == path)
        )
        if not matching:
            raise ResourceError(
                f"code tag {tag.tag_id} has no occurrence matching the requested file context"
            )
        occurrence = matching[0]
        grouped.setdefault(
            (occurrence.repository, occurrence.blob_oid), []
        ).append((tag, occurrence))

    regions: list[CodeSourceRegion] = []
    remaining_lines = max_lines
    remaining_chars = max_chars
    globally_truncated = False
    for (repository_name, blob_oid), items in grouped.items():
        lines = _source_lines(read_blob(repository_name, bytes.fromhex(blob_oid)))
        candidates = sorted(((
            max(1, tag.line_start - context_lines),
            min(len(lines), (tag.line_end or tag.line_start) + context_lines),
            tag,
            occurrence,
        ) for tag, occurrence in items), key=lambda item: (item[0], item[1], item[2].tag_id))
        merged: list[tuple[int, int, list[tuple[CodeTagInfo, CodeOccurrence]]]] = []
        for start, end, tag, occurrence in candidates:
            if merged and start <= merged[-1][1] + 1:
                previous_start, previous_end, previous_tags = merged[-1]
                merged[-1] = (
                    previous_start, max(previous_end, end),
                    [*previous_tags, (tag, occurrence)],
                )
            else:
                merged.append((start, end, [(tag, occurrence)]))
        for start, requested_end, region_tags in merged:
            if remaining_lines <= 0 or remaining_chars <= 0:
                globally_truncated = True
                break
            end = min(requested_end, start + remaining_lines - 1)
            source = _format_lines(lines[start - 1:end], start, numbered)
            char_truncated = len(source) > remaining_chars
            if char_truncated:
                source = source[:remaining_chars]
            truncated = end < requested_end or char_truncated
            occurrence = region_tags[0][1]
            regions.append(CodeSourceRegion(
                file=CodeFile(
                    occurrence.repository, occurrence.revision,
                    occurrence.path, occurrence.blob_oid,
                ),
                line_start=start, line_end=end,
                tags=tuple(CodeTagBounds(
                    tag.tag_id, tag.line_start, tag.line_end or tag.line_start
                ) for tag, _occurrence in region_tags),
                source=source, truncated=truncated,
            ))
            remaining_lines -= max(0, end - start + 1)
            remaining_chars -= len(source)
            globally_truncated |= truncated
        if remaining_lines <= 0 or remaining_chars <= 0:
            globally_truncated = True
            break
    return CodeSourceBatch(tuple(regions), globally_truncated)


def _enclosing_tag(
    connection: sqlite3.Connection, tag_id: int
) -> tuple[int, str] | None:
    row = connection.execute(
        "SELECT analysis_id, line_start, line_end, enclosing_tag_id FROM ctags_tags WHERE id = ?",
        (tag_id,),
    ).fetchone()
    if row is None:
        raise ResourceError(f"unknown code tag ID: {tag_id}")
    if row["enclosing_tag_id"] is not None:
        return row["enclosing_tag_id"], "ctags"
    child_end = row["line_end"] or row["line_start"]
    fallback = connection.execute(
        """
        SELECT id
        FROM ctags_tags
        WHERE analysis_id = ? AND id <> ? AND is_reference = 0
          AND line_end IS NOT NULL
          AND line_start <= ? AND line_end >= ?
        ORDER BY (line_end - line_start), line_start DESC, ordinal
        LIMIT 1
        """,
        (row["analysis_id"], tag_id, row["line_start"], child_end),
    ).fetchone()
    return (fallback["id"], "containing-range") if fallback is not None else None


def read_enclosing_source(
    connection: sqlite3.Connection,
    tag_ids: list[int] | tuple[int, ...],
    read_blob: Callable[[str, bytes], bytes],
    *,
    levels: int = 1,
    context_lines: int = 0,
    numbered: bool = True,
    max_lines: int = 5000,
    max_chars: int = 200_000,
) -> CodeEnclosingSource:
    """Zoom out to enclosing tags without silently falling back to whole files."""
    if levels < 1:
        raise ResourceError("enclosing scope levels must be at least 1")
    ids = tuple(dict.fromkeys(tag_ids))
    inspect_tags(connection, ids)
    resolutions: list[CodeEnclosingScope] = []
    scope_ids: list[int] = []
    for requested in ids:
        current = requested
        methods: list[str] = []
        for _level in range(levels):
            enclosing = _enclosing_tag(connection, current)
            if enclosing is None:
                break
            current, method = enclosing
            methods.append(method)
        if not methods:
            resolutions.append(CodeEnclosingScope(
                requested, None, None,
                "tag has no indexed enclosing scope",
            ))
            continue
        scope_ids.append(current)
        resolutions.append(CodeEnclosingScope(
            requested, current, "+".join(methods),
            None if len(methods) == levels else "outermost indexed scope reached",
        ))
    source = read_tagged_source(
        connection, list(dict.fromkeys(scope_ids)), read_blob,
        context_lines=context_lines, numbered=numbered,
        max_lines=max_lines, max_chars=max_chars,
    ) if scope_ids else CodeSourceBatch((), False)
    return CodeEnclosingSource(tuple(resolutions), source)


def _select_occurrence(
    tag: CodeTagInfo,
    *,
    repository: str | None,
    revision: str | None,
    path: str | None,
) -> CodeOccurrence:
    for occurrence in tag.occurrences:
        if (
            (repository is None or occurrence.repository == repository)
            and (revision is None or occurrence.revision == revision)
            and (path is None or occurrence.path == path)
        ):
            return occurrence
    raise ResourceError(
        f"code tag {tag.tag_id} has no occurrence matching the requested file context"
    )


def diff_tagged_source(
    connection: sqlite3.Connection,
    from_tag_id: int,
    to_tag_id: int,
    read_blob: Callable[[str, bytes], bytes],
    *,
    from_repository: str | None = None,
    from_revision: str | None = None,
    from_path: str | None = None,
    to_repository: str | None = None,
    to_revision: str | None = None,
    to_path: str | None = None,
    context_lines: int = 3,
    max_chars: int = 200_000,
) -> CodeTagDiff:
    """Create a bounded unified diff between exactly two tagged regions."""
    if context_lines < 0:
        raise ResourceError("diff context lines must be at least 0")
    if max_chars < 1:
        raise ResourceError("diff output limit must be positive")
    inspected = {
        tag.tag_id: tag
        for tag in inspect_tags(connection, [from_tag_id, to_tag_id])
    }
    from_tag = inspected[from_tag_id]
    to_tag = inspected[to_tag_id]
    from_occurrence = _select_occurrence(
        from_tag, repository=from_repository, revision=from_revision, path=from_path
    )
    to_occurrence = _select_occurrence(
        to_tag, repository=to_repository, revision=to_revision, path=to_path
    )

    def region(tag: CodeTagInfo, occurrence: CodeOccurrence) -> list[str]:
        content = read_blob(
            occurrence.repository, bytes.fromhex(occurrence.blob_oid)
        ).decode("utf-8", errors="replace").splitlines(keepends=True)
        return content[tag.line_start - 1:(tag.line_end or tag.line_start)]

    from_label = (
        f"{from_occurrence.repository}@{from_occurrence.revision}:"
        f"{from_occurrence.path}:{from_tag.line_start}"
    )
    to_label = (
        f"{to_occurrence.repository}@{to_occurrence.revision}:"
        f"{to_occurrence.path}:{to_tag.line_start}"
    )
    diff = "".join(difflib.unified_diff(
        region(from_tag, from_occurrence), region(to_tag, to_occurrence),
        fromfile=from_label, tofile=to_label, n=context_lines,
    ))
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars]
    return CodeTagDiff(
        from_tag_id, to_tag_id,
        CodeFile(
            from_occurrence.repository, from_occurrence.revision,
            from_occurrence.path, from_occurrence.blob_oid,
        ),
        CodeFile(
            to_occurrence.repository, to_occurrence.revision,
            to_occurrence.path, to_occurrence.blob_oid,
        ),
        diff, truncated,
    )


def find_references(
    connection: sqlite3.Connection,
    symbol: str,
    **filters: Any,
) -> CodeTagSearch:
    """Best-effort Ctags reference lookup for a name or qualified name."""
    if not symbol:
        raise ResourceError("reference symbol must not be empty")
    field = "qualified_name" if "::" in symbol else "name"
    return search_tags(
        connection, **{field: symbol, "is_reference": True, **filters}
    )


def _outline_rows_for_ids(
    connection: sqlite3.Connection, tag_ids: Iterable[int]
) -> tuple[CodeOutlineItem, ...]:
    ids = tuple(tag_ids)
    if not ids:
        return ()
    rows = connection.execute(
        f"""
        SELECT tag.id, tag.name, tag.qualified_name, parser.language,
               kind.name AS kind, tag.line_start, tag.line_end,
               tag.signature, tag.typeref, tag.access, tag.is_reference,
               tag.enclosing_tag_id
        FROM ctags_tags AS tag
        JOIN ctags_parsers AS parser ON parser.id = tag.parser_id
        JOIN ctags_kinds AS kind ON kind.id = tag.kind_id
        WHERE tag.id IN ({', '.join('?' for _ in ids)})
        """,
        ids,
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return _outline_items(by_id[tag_id] for tag_id in ids if tag_id in by_id)


def locate_at_line(
    connection: sqlite3.Connection,
    repository: str,
    revision: str,
    path: str,
    line: int,
    *,
    nearby_limit: int = 5,
) -> CodeLineLocation:
    """Locate containing tags and their scope chain at one source line."""
    if line < 1:
        raise ResourceError("source line must be at least 1")
    if not 1 <= nearby_limit <= 50:
        raise ResourceError("nearby tag limit must be between 1 and 50")
    file, analysis_id = resolve_file(connection, repository, revision, path)
    containing_ids = [
        row["id"] for row in connection.execute(
            """
            SELECT id FROM ctags_tags
            WHERE analysis_id = ? AND is_reference = 0
              AND line_start <= ? AND coalesce(line_end, line_start) >= ?
            ORDER BY (coalesce(line_end, line_start) - line_start), line_start DESC
            """,
            (analysis_id, line, line),
        )
    ]
    chain_ids: list[int] = []
    if containing_ids:
        current = containing_ids[0]
        seen = {current}
        while True:
            row = connection.execute(
                "SELECT enclosing_tag_id FROM ctags_tags WHERE id = ?", (current,)
            ).fetchone()
            if row is None or row["enclosing_tag_id"] is None:
                break
            current = row["enclosing_tag_id"]
            if current in seen:
                break
            seen.add(current)
            chain_ids.append(current)
    nearby_ids: list[int] = []
    if not containing_ids:
        nearby_ids = [
            row["id"] for row in connection.execute(
                """
                SELECT id FROM ctags_tags
                WHERE analysis_id = ? AND is_reference = 0
                ORDER BY abs(line_start - ?), line_start, ordinal
                LIMIT ?
                """,
                (analysis_id, line, nearby_limit),
            )
        ]
    return CodeLineLocation(
        file, line, _outline_rows_for_ids(connection, containing_ids),
        _outline_rows_for_ids(connection, chain_ids),
        _outline_rows_for_ids(connection, nearby_ids),
    )


def compare_outlines(
    connection: sqlite3.Connection,
    from_repository: str,
    from_revision: str,
    from_path: str,
    to_repository: str,
    to_revision: str,
    to_path: str,
    read_blob: Callable[[str, bytes], bytes],
) -> CodeOutlineComparison:
    """Compare definitions in two files without producing a whole-file diff."""
    before = outline_file(
        connection, from_repository, from_revision, from_path, limit=5000
    )
    after = outline_file(
        connection, to_repository, to_revision, to_path, limit=5000
    )
    if before.truncated or after.truncated:
        raise ResourceError("file outline is too large to compare")

    def grouped(items: tuple[CodeOutlineItem, ...]) -> dict[tuple[str, str], list[CodeOutlineItem]]:
        result: dict[tuple[str, str], list[CodeOutlineItem]] = {}
        for item in items:
            result.setdefault((item.qualified_name or item.name, item.kind), []).append(item)
        return result

    before_by_key = grouped(before.items)
    after_by_key = grouped(after.items)
    before_lines = _source_lines(read_blob(
        from_repository, bytes.fromhex(before.file.blob_oid)
    ))
    after_lines = _source_lines(read_blob(
        to_repository, bytes.fromhex(after.file.blob_oid)
    ))

    def body(lines: list[str], item: CodeOutlineItem) -> tuple[str, ...]:
        return tuple(lines[item.line_start - 1:(item.line_end or item.line_start)])

    changes: list[CodeOutlineChange] = []
    for symbol, kind in sorted(before_by_key.keys() | after_by_key.keys()):
        old = before_by_key.get((symbol, kind), [])
        new = after_by_key.get((symbol, kind), [])
        if len(old) > 1 or len(new) > 1:
            status = "ambiguous"
        elif not old:
            status = "added"
        elif not new:
            status = "removed"
        elif body(before_lines, old[0]) == body(after_lines, new[0]):
            status = "unchanged"
        else:
            status = "changed"
        changes.append(CodeOutlineChange(
            status, symbol, kind,
            old[0].tag_id if len(old) == 1 else None,
            new[0].tag_id if len(new) == 1 else None,
        ))
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.status] = counts.get(change.status, 0) + 1
    return CodeOutlineComparison(before.file, after.file, tuple(changes), counts)


def trace_history(
    connection: sqlite3.Connection,
    repository: str,
    revisions: list[str] | tuple[str, ...],
    symbol: str,
    *,
    path: str | None = None,
    qualified: bool = False,
) -> CodeSymbolHistory:
    """Trace exact Ctags matches in manifest revision order, collapsing reuse."""
    if not symbol:
        raise ResourceError("history symbol must not be empty")
    steps: list[CodeHistoryStep] = []
    for revision in revisions:
        result = search_tags(
            connection, repository=repository, revision=revision, path=path,
            qualified_name=symbol if qualified else None,
            name=None if qualified else symbol, limit=200,
        )
        matches = result.results
        if result.truncated:
            raise ResourceError(
                f"too many matches to trace {symbol!r} in {repository}:{revision}"
            )
        identity = tuple(tag.tag_id for tag in matches)
        if steps and tuple(tag.tag_id for tag in steps[-1].matches) == identity:
            previous = steps[-1]
            steps[-1] = CodeHistoryStep(
                (*previous.revisions, revision), previous.matches,
                previous.ambiguous,
            )
        else:
            steps.append(CodeHistoryStep(
                (revision,), matches, len(matches) > 1,
            ))
    return CodeSymbolHistory(repository, symbol, tuple(steps))
