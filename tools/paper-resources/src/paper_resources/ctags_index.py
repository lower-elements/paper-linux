"""Relational persistence for Universal Ctags blob analyses."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from . import ctags, repository_index
from .config import ResourceError


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    id: int
    profile_id: int
    input_parser_id: int | None
    tags: int
    roles: int
    qualified_names: int


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
    )
