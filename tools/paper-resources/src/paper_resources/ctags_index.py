"""Relational persistence for Universal Ctags blob analyses."""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import ExitStack
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from . import ctags, git_resources, repository_index
from .config import ResourceError


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    id: int
    profile_id: int
    input_parser_id: int | None
    tags: int
    roles: int
    qualified_names: int
    enclosing_links: int


@dataclass(frozen=True, slots=True)
class CodeRevisionOutcome:
    repository_id: str
    revision_id: str
    status: str
    paths: int
    indexed: int
    reused: int
    unrecognized: int
    failed: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeIndexReport:
    revisions: tuple[CodeRevisionOutcome, ...]

    @property
    def failed(self) -> bool:
        return any(revision.failed for revision in self.revisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revisions": [asdict(revision) for revision in self.revisions],
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=True)
class NormalizedTag:
    name: str
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
    metadata: str | None


def _optional_text(fields: dict[str, Any], name: str) -> str | None:
    value = fields.pop(name, None)
    if value is not None and not isinstance(value, str):
        raise ResourceError(f"Ctags emitted a non-text {name} field")
    return value


def _optional_integer(
    fields: dict[str, Any], name: str, *, minimum: int
) -> int | None:
    value = fields.pop(name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResourceError(
            f"Ctags emitted an invalid {name} field: {value!r}"
        )
    return value


def normalize_tag(event: ctags.CtagsTag) -> NormalizedTag:
    """Promote queryable fields and encode the remaining Ctags data as JSON."""
    fields = dict(event.fields)
    line_start = _optional_integer(fields, "line", minimum=1)
    if line_start is None:
        raise ResourceError(f"Ctags emitted {event.name!r} without a source line")
    line_end = _optional_integer(fields, "end", minimum=line_start)
    nth = _optional_integer(fields, "nth", minimum=0)
    signature = _optional_text(fields, "signature")
    typeref = _optional_text(fields, "typeref")
    access = _optional_text(fields, "access")
    scope = _optional_text(fields, "scope")
    scope_kind = _optional_text(fields, "scopeKind")

    file_restricted = fields.pop("file", False)
    if not isinstance(file_restricted, bool):
        raise ResourceError("Ctags emitted a non-boolean file field")

    ignored_extras = {"fileScope", "qualified", "reference"}
    remaining_extras = [
        extra for extra in event.extras if extra not in ignored_extras
    ]
    metadata: dict[str, Any] = fields
    if remaining_extras:
        metadata["extras"] = remaining_extras

    try:
        encoded_metadata = (
            json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if metadata
            else None
        )
    except (TypeError, ValueError) as error:
        raise ResourceError(
            f"Ctags emitted non-JSON metadata for {event.name!r}: {error}"
        ) from error

    return NormalizedTag(
        name=event.name,
        line_start=line_start,
        line_end=line_end,
        signature=signature,
        typeref=typeref,
        access=access,
        scope=scope,
        scope_kind=scope_kind,
        nth=nth,
        is_file_restricted=file_restricted or "fileScope" in event.extras,
        is_reference=(
            "reference" in event.extras
            or any(role != "def" for role in event.roles)
        ),
        metadata=encoded_metadata,
    )


def _pairing_key(event: ctags.CtagsTag) -> tuple[object, ...]:
    """Identify ordinary and qualified records describing the same tag."""
    try:
        fields = json.dumps(
            event.fields,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ResourceError(
            f"Ctags emitted non-JSON fields for {event.name!r}: {error}"
        ) from error
    return (
        event.parser_id,
        event.kind_id,
        event.roles,
        tuple(extra for extra in event.extras if extra != "qualified"),
        fields,
    )


def resolve_enclosing_tags(
    connection: sqlite3.Connection, analysis_id: int
) -> int:
    """Resolve reported scopes to enclosing tags where a best match exists."""
    connection.execute(
        "UPDATE ctags_tags SET enclosing_tag_id = NULL WHERE analysis_id = ?",
        (analysis_id,),
    )
    rows = connection.execute(
        """
        SELECT
            ctags_tags.id,
            ctags_tags.name,
            ctags_tags.qualified_name,
            ctags_tags.line_start,
            ctags_tags.line_end,
            ctags_tags.scope,
            ctags_tags.scope_kind,
            ctags_kinds.name AS kind
        FROM ctags_tags
        JOIN ctags_kinds ON ctags_kinds.id = ctags_tags.kind_id
        WHERE ctags_tags.analysis_id = ?
        ORDER BY ctags_tags.ordinal
        """,
        (analysis_id,),
    ).fetchall()
    candidates_by_scope: dict[tuple[str, str], dict[int, sqlite3.Row]] = (
        defaultdict(dict)
    )
    for row in rows:
        candidates_by_scope[(row["kind"], row["name"])][row["id"]] = row
        if row["qualified_name"] is not None:
            candidates_by_scope[
                (row["kind"], row["qualified_name"])
            ][row["id"]] = row

    links: list[tuple[int, int]] = []
    for child in rows:
        scope = child["scope"]
        scope_kind = child["scope_kind"]
        if scope is None or scope_kind is None:
            continue
        candidates = [
            candidate
            for candidate in candidates_by_scope.get(
                (scope_kind, scope), {}
            ).values()
            if candidate["id"] != child["id"]
        ]
        if not candidates:
            continue

        qualified = [
            candidate
            for candidate in candidates
            if candidate["qualified_name"] == scope
        ]
        if qualified:
            candidates = qualified

        containing = [
            candidate
            for candidate in candidates
            if candidate["line_end"] is not None
            and candidate["line_start"] <= child["line_start"] <= candidate["line_end"]
        ]
        if containing:
            candidates = containing
            shortest_span = min(
                candidate["line_end"] - candidate["line_start"]
                for candidate in candidates
            )
            candidates = [
                candidate
                for candidate in candidates
                if candidate["line_end"] - candidate["line_start"]
                == shortest_span
            ]

        if len(candidates) > 1:
            nearest_line = max(
                (
                    candidate["line_start"]
                    for candidate in candidates
                    if candidate["line_start"] <= child["line_start"]
                ),
                default=None,
            )
            if nearest_line is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate["line_start"] == nearest_line
                ]
        if len(candidates) == 1:
            links.append((candidates[0]["id"], child["id"]))

    connection.executemany(
        "UPDATE ctags_tags SET enclosing_tag_id = ? WHERE id = ?",
        links,
    )
    return len(links)


def store_blob_analysis(
    connection: sqlite3.Connection,
    session: ctags.CtagsSession,
    repository_id: str,
    oid: bytes,
    input_name: str,
    content: bytes,
) -> StoredAnalysis:
    """Replace one blob's current analysis atomically with normalized Ctags data."""
    if not input_name or "\0" in input_name:
        raise ResourceError(
            "Ctags analysis input name must not be empty or contain NUL"
        )
    repository_index.register_blob(connection, repository_id, oid, len(content))

    events = session.analyze(input_name, content)
    analysis_id: int | None = None
    profile_id: int | None = None
    completed: ctags.CtagsCompleted | None = None
    tag_count = 0
    role_count = 0
    qualified_names = 0
    enclosing_links = 0
    ordinary_tags: dict[tuple[object, ...], deque[int]] = defaultdict(deque)
    pending_qualified: dict[tuple[object, ...], deque[str]] = defaultdict(deque)
    try:
        for event in events:
            if isinstance(event, ctags.CtagsProfile):
                if profile_id is not None:
                    raise ResourceError("Ctags emitted more than one profile per blob")
                profile_id = event.id
                connection.execute(
                    """
                    DELETE FROM ctags_analyses
                    WHERE repository_id = ? AND blob_oid = ?
                    """,
                    (repository_id, oid),
                )
                row = connection.execute(
                    """
                    INSERT INTO ctags_analyses(
                        repository_id, blob_oid, profile_id, input_name,
                        input_parser_id
                    ) VALUES (?, ?, ?, ?, NULL)
                    RETURNING id
                    """,
                    (repository_id, oid, profile_id, input_name),
                ).fetchone()
                if row is None:
                    raise ResourceError("SQLite did not return a Ctags analysis ID")
                analysis_id = int(row[0])
                continue

            if isinstance(event, ctags.CtagsTag):
                if analysis_id is None or profile_id is None:
                    raise ResourceError("Ctags emitted a tag before its profile")
                pairing_key = _pairing_key(event)
                if "qualified" in event.extras:
                    candidates = ordinary_tags[pairing_key]
                    if candidates:
                        tag_id = candidates.popleft()
                        connection.execute(
                            "UPDATE ctags_tags SET qualified_name = ? WHERE id = ?",
                            (event.name, tag_id),
                        )
                        qualified_names += 1
                    else:
                        pending_qualified[pairing_key].append(event.name)
                    continue
                tag = normalize_tag(event)
                qualified_name: str | None = None
                candidates = pending_qualified[pairing_key]
                if candidates:
                    qualified_name = candidates.popleft()
                    qualified_names += 1
                row = connection.execute(
                    """
                    INSERT INTO ctags_tags(
                        analysis_id, profile_id, parser_id, kind_id, ordinal,
                        name, qualified_name, line_start, line_end, signature,
                        typeref, access, scope, scope_kind, nth,
                        is_file_restricted, is_reference, enclosing_tag_id,
                        metadata
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        NULL, jsonb(?)
                    )
                    RETURNING id
                    """,
                    (
                        analysis_id,
                        profile_id,
                        event.parser_id,
                        event.kind_id,
                        tag_count,
                        tag.name,
                        qualified_name,
                        tag.line_start,
                        tag.line_end,
                        tag.signature,
                        tag.typeref,
                        tag.access,
                        tag.scope,
                        tag.scope_kind,
                        tag.nth,
                        tag.is_file_restricted,
                        tag.is_reference,
                        tag.metadata,
                    ),
                ).fetchone()
                if row is None:
                    raise ResourceError("SQLite did not return a Ctags tag ID")
                tag_id = int(row[0])
                if qualified_name is None:
                    ordinary_tags[pairing_key].append(tag_id)
                connection.executemany(
                    """
                    INSERT INTO ctags_tag_roles(tag_id, kind_id, role_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (tag_id, event.kind_id, role_id)
                        for role_id in event.role_ids
                    ],
                )
                tag_count += 1
                role_count += len(event.role_ids)
                continue

            if isinstance(event, ctags.CtagsCompleted):
                if analysis_id is None or profile_id is None:
                    raise ResourceError("Ctags completed a blob before its profile")
                if event.profile_id != profile_id:
                    raise ResourceError("Ctags changed profiles while analysing a blob")
                if any(pending_qualified.values()):
                    raise ResourceError(
                        "Ctags emitted a qualified tag without an ordinary counterpart"
                    )
                enclosing_links = resolve_enclosing_tags(connection, analysis_id)
                connection.execute(
                    """
                    UPDATE ctags_analyses SET input_parser_id = ? WHERE id = ?
                    """,
                    (event.input_parser_id, analysis_id),
                )
                completed = event
    except sqlite3.Error as error:
        events.close()
        raise ResourceError(f"cannot store Ctags analysis: {error}") from error
    except BaseException:
        events.close()
        raise

    if completed is None or analysis_id is None or profile_id is None:
        raise ResourceError("Ctags did not complete the blob analysis")
    return StoredAnalysis(
        id=analysis_id,
        profile_id=profile_id,
        input_parser_id=completed.input_parser_id,
        tags=tag_count,
        roles=role_count,
        qualified_names=qualified_names,
        enclosing_links=enclosing_links,
    )


def index_repositories(
    repositories: list[dict[str, Any]],
    root: Path,
    connection: sqlite3.Connection,
    *,
    executable: str = "ctags",
) -> CodeIndexReport:
    """Index unique blobs reachable from manifest-selected revisions."""
    repository_index.synchronize_catalog(connection, repositories)
    with connection:
        connection.execute(
            """
            DELETE FROM ctags_revision_paths
            WHERE NOT EXISTS (
                SELECT 1 FROM repository_revisions
                WHERE repository_revisions.repository_id =
                    ctags_revision_paths.repository_id
                AND repository_revisions.id = ctags_revision_paths.revision_id
                AND repository_revisions.index_enabled = 1
            )
            """
        )

    selected = [
        (repository, revision)
        for repository in repositories
        for revision in repository.get("revisions", [])
        if revision["index"]
    ]
    if not selected:
        return CodeIndexReport(())

    outcomes: list[CodeRevisionOutcome] = []
    with ctags.CtagsSession(connection, executable) as session, ExitStack() as stack:
        profile = session.ensure_profile()
        readers: dict[str, git_resources.GitBlobReader] = {}
        for repository, revision in selected:
            repository_id = repository["id"]
            revision_id = revision["id"]
            paths = 0
            indexed = 0
            reused = 0
            unrecognized = 0
            failed = 0
            unavailable = False
            warnings: list[str] = []
            counted_oids: set[bytes] = set()
            seen_paths: set[str] = set()
            path_rows: list[tuple[str, str, str, bytes, int]] = []
            try:
                for blob in repository_index.iter_revision_blobs(
                    repository, revision, root
                ):
                    analysis = connection.execute(
                        """
                        SELECT
                            ctags_analyses.profile_id,
                            ctags_analyses.input_name,
                            ctags_parsers.language
                        FROM ctags_analyses
                        LEFT JOIN ctags_parsers
                            ON ctags_parsers.id = ctags_analyses.input_parser_id
                        WHERE ctags_analyses.repository_id = ?
                            AND ctags_analyses.blob_oid = ?
                        """,
                        (repository_id, blob.oid),
                    ).fetchone()
                    current = (
                        analysis is not None
                        and analysis["profile_id"] == profile.id
                    )
                    try:
                        if not current:
                            reader = readers.get(repository_id)
                            if reader is None:
                                reader = stack.enter_context(
                                    git_resources.GitBlobReader(
                                        root / repository["path"]
                                    )
                                )
                                readers[repository_id] = reader
                            content = reader.read(blob.oid, blob.size)
                            stored = store_blob_analysis(
                                connection,
                                session,
                                repository_id,
                                blob.oid,
                                blob.path,
                                content,
                            )
                            actual_language = connection.execute(
                                """
                                SELECT language FROM ctags_parsers WHERE id = ?
                                """,
                                (stored.input_parser_id,),
                            ).fetchone()
                            actual_language = (
                                actual_language[0]
                                if actual_language is not None
                                else None
                            )
                            input_name = blob.path
                            if blob.oid not in counted_oids:
                                indexed += 1
                        else:
                            actual_language = analysis["language"]
                            input_name = analysis["input_name"]
                            if blob.oid not in counted_oids:
                                reused += 1

                        if blob.oid not in counted_oids and actual_language is None:
                            unrecognized += 1

                        expected_language = session.expected_language(blob.path)
                        if (
                            input_name != blob.path
                            and expected_language != actual_language
                            and len(warnings) < 100
                        ):
                            warnings.append(
                                f"{blob.path}: reused {input_name} analysis as "
                                f"{actual_language or 'unrecognized'}, expected "
                                f"{expected_language or 'unrecognized'}"
                            )
                        counted_oids.add(blob.oid)
                        seen_paths.add(blob.path)
                        path_rows.append(
                            (
                                repository_id,
                                revision_id,
                                blob.path,
                                blob.oid,
                                blob.mode,
                            )
                        )
                        paths += 1
                    except (ResourceError, OSError, sqlite3.Error) as error:
                        failed += 1
                        if len(warnings) < 100:
                            warnings.append(f"{blob.path}: {error}")

                with connection:
                    connection.executemany(
                        """
                        INSERT INTO ctags_revision_paths(
                            repository_id, revision_id, path, blob_oid, mode
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(repository_id, revision_id, path) DO UPDATE SET
                            blob_oid = excluded.blob_oid,
                            mode = excluded.mode
                        """,
                        path_rows,
                    )
                    if failed == 0:
                        indexed_paths = {
                            row[0]
                            for row in connection.execute(
                                """
                                SELECT path FROM ctags_revision_paths
                                WHERE repository_id = ? AND revision_id = ?
                                """,
                                (repository_id, revision_id),
                            )
                        }
                        connection.executemany(
                            """
                            DELETE FROM ctags_revision_paths
                            WHERE repository_id = ? AND revision_id = ? AND path = ?
                            """,
                            [
                                (repository_id, revision_id, path)
                                for path in sorted(indexed_paths - seen_paths)
                            ],
                        )
            except ResourceError as error:
                message = str(error)
                if paths == 0 and (
                    message.startswith("repository is not populated:")
                    or message.startswith("revision is not populated:")
                ):
                    unavailable = True
                else:
                    failed += 1
                warnings.append(message)
            except (OSError, sqlite3.Error) as error:
                failed += 1
                warnings.append(str(error))

            if unavailable:
                status = "skipped"
            elif failed == 0:
                status = "ok"
            elif paths:
                status = "partial"
            else:
                status = "failed"
            outcomes.append(
                CodeRevisionOutcome(
                    repository_id,
                    revision_id,
                    status,
                    paths,
                    indexed,
                    reused,
                    unrecognized,
                    failed,
                    tuple(warnings),
                )
            )
    return CodeIndexReport(tuple(outcomes))
