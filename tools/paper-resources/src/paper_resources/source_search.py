"""Bounded, revision-aware source and path discovery over Git objects."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
import subprocess

from .config import ResourceError
from .code_navigation import CodeOutlineItem
from .git_resources import (
    GitBlob,
    iter_tree_blobs,
    oid_to_hex,
    read_blob,
    tree_blob_at_path,
    validate_repository_path,
)


@dataclass(frozen=True, slots=True)
class FileOccurrence:
    revision: str
    path: str


@dataclass(frozen=True, slots=True)
class RevisionFileMatch:
    blob_oid: str
    size: int
    occurrences: tuple[FileOccurrence, ...]


@dataclass(frozen=True, slots=True)
class RevisionFileSearch:
    repository: str
    revisions: tuple[str, ...]
    total: int
    offset: int
    limit: int
    results: tuple[RevisionFileMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class TextOccurrence:
    revision: str
    path: str


@dataclass(frozen=True, slots=True)
class SourceTextMatch:
    blob_oid: str
    line: int
    text: str
    line_start: int
    line_end: int
    source: str
    occurrences: tuple[TextOccurrence, ...]
    containing: tuple[CodeOutlineItem, ...] = ()
    enclosing_chain: tuple[CodeOutlineItem, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceTextSearch:
    repository: str
    revisions: tuple[str, ...]
    query: str
    regex: bool
    case_sensitive: bool
    total: int
    offset: int
    limit: int
    results: tuple[SourceTextMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class BlobOccurrences:
    repository: str
    source_revision: str
    source_path: str
    blob_oid: str
    size: int
    searched_revisions: tuple[str, ...]
    occurrences: tuple[FileOccurrence, ...]


def _matches_path(
    path: str, *, path_prefix: str | None, globs: tuple[str, ...]
) -> bool:
    if path_prefix is not None and not (
        path == path_prefix or path.startswith(path_prefix.rstrip("/") + "/")
    ):
        return False
    return not globs or any(fnmatchcase(path, pattern) for pattern in globs)


def find_files(
    repository_path: Path,
    repository: str,
    revisions: tuple[tuple[str, str, str], ...],
    *,
    path_prefix: str | None = None,
    globs: tuple[str, ...] = (),
    offset: int = 0,
    limit: int = 200,
) -> RevisionFileSearch:
    """Find paths in revisions, grouping occurrences of identical blobs."""
    if offset < 0 or limit < 1:
        raise ResourceError("file search offset and limit must be nonnegative and positive")
    prefix = validate_repository_path(path_prefix) if path_prefix else None
    grouped: dict[bytes, tuple[int, list[FileOccurrence]]] = {}
    for revision_id, _commit_oid, tree_oid in revisions:
        for blob in iter_tree_blobs(repository_path, tree_oid):
            if not _matches_path(blob.path, path_prefix=prefix, globs=globs):
                continue
            size, occurrences = grouped.setdefault(blob.oid, (blob.size, []))
            if size != blob.size:
                raise ResourceError("Git returned inconsistent sizes for one blob")
            occurrences.append(FileOccurrence(revision_id, blob.path))
    results = tuple(
        RevisionFileMatch(oid_to_hex(oid), size, tuple(occurrences))
        for oid, (size, occurrences) in sorted(
            grouped.items(), key=lambda item: (
                item[1][1][0].path, item[1][1][0].revision, item[0]
            )
        )
    )
    return RevisionFileSearch(
        repository, tuple(item[0] for item in revisions), len(results), offset, limit,
        results[offset:offset + limit], offset + limit < len(results),
    )


def _git_grep(
    repository_path: Path,
    commit: str,
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
    path_prefix: str | None,
    globs: tuple[str, ...],
) -> list[tuple[str, int, str]]:
    arguments = [
        "git", "--git-dir", str(repository_path), "grep", "-n", "-z", "-I",
        "-E" if regex else "-F",
    ]
    if not case_sensitive:
        arguments.append("-i")
    arguments.extend(["-e", query, commit, "--"])
    if path_prefix:
        arguments.append(validate_repository_path(path_prefix))
    arguments.extend(f":(glob){pattern}" for pattern in globs)
    try:
        completed = subprocess.run(arguments, check=False, capture_output=True)
    except FileNotFoundError as error:
        raise ResourceError("git is required to search repository resources") from error
    if completed.returncode == 1:
        return []
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ResourceError(f"git grep failed: {detail or completed.returncode}")
    output = completed.stdout
    matches: list[tuple[str, int, str]] = []
    position = 0
    prefix = commit.encode("ascii") + b":"
    while position < len(output):
        path_end = output.find(b"\0", position)
        line_end = output.find(b"\0", path_end + 1)
        text_end = output.find(b"\n", line_end + 1)
        if path_end < 0 or line_end < 0 or text_end < 0:
            raise ResourceError("git grep returned malformed output")
        raw_path = output[position:path_end]
        if raw_path.startswith(prefix):
            raw_path = raw_path[len(prefix):]
        try:
            path = validate_repository_path(raw_path.decode("utf-8"))
            line = int(output[path_end + 1:line_end])
        except (UnicodeDecodeError, ValueError) as error:
            raise ResourceError("git grep returned invalid path or line data") from error
        text = output[line_end + 1:text_end].decode("utf-8", errors="replace")
        matches.append((path, line, text))
        position = text_end + 1
    return matches


def search_text(
    repository_path: Path,
    repository: str,
    revisions: tuple[tuple[str, str, str], ...],
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = True,
    path_prefix: str | None = None,
    globs: tuple[str, ...] = (),
    context_lines: int = 0,
    offset: int = 0,
    limit: int = 50,
) -> SourceTextSearch:
    """Search pinned revisions and collapse identical blob/line matches."""
    if not query:
        raise ResourceError("source search query must not be empty")
    if context_lines < 0 or offset < 0 or limit < 1:
        raise ResourceError("source search bounds must be nonnegative and limit positive")
    blob_by_revision_path: dict[tuple[str, str], GitBlob] = {}
    grouped: dict[tuple[bytes, int, str], list[TextOccurrence]] = {}
    for revision_id, commit, tree_oid in revisions:
        for path, line, text in _git_grep(
            repository_path, commit, query, regex=regex,
            case_sensitive=case_sensitive, path_prefix=path_prefix, globs=globs,
        ):
            key = (revision_id, path)
            blob = blob_by_revision_path.get(key)
            if blob is None:
                blob = tree_blob_at_path(repository_path, tree_oid, path)
                if blob is not None:
                    blob_by_revision_path[key] = blob
            if blob is None:
                raise ResourceError(f"git grep returned a path absent from its tree: {path}")
            grouped.setdefault((blob.oid, line, text), []).append(
                TextOccurrence(revision_id, path)
            )
    raw_results = sorted(
        grouped.items(), key=lambda item: (
            item[1][0].path, item[0][1], item[1][0].revision, item[0][0]
        )
    )
    results: list[SourceTextMatch] = []
    for (oid, line, text), occurrences in raw_results[offset:offset + limit]:
        source_lines = read_blob(repository_path, oid).decode(
            "utf-8", errors="replace"
        ).splitlines()
        start = max(1, line - context_lines)
        end = min(len(source_lines), line + context_lines)
        width = len(str(end))
        source = "\n".join(
            f"{number:>{width}} | {source_lines[number - 1]}"
            for number in range(start, end + 1)
        )
        results.append(SourceTextMatch(
            oid_to_hex(oid), line, text, start, end, source, tuple(occurrences)
        ))
    return SourceTextSearch(
        repository, tuple(item[0] for item in revisions), query, regex,
        case_sensitive, len(raw_results), offset, limit, tuple(results),
        offset + limit < len(raw_results),
    )


def find_blob_occurrences(
    repository_path: Path,
    repository: str,
    revisions: tuple[tuple[str, str, str], ...],
    source_revision: str,
    source_path: str,
) -> BlobOccurrences:
    """Find every path containing the source file's exact blob."""
    normalized = validate_repository_path(source_path)
    source = next((item for item in revisions if item[0] == source_revision), None)
    if source is None:
        raise ResourceError(f"source revision was not selected: {source_revision}")
    source_blob = tree_blob_at_path(repository_path, source[2], normalized)
    if source_blob is None:
        raise ResourceError(
            f"file does not exist: {repository}:{source_revision}:{normalized}"
        )
    occurrences = tuple(
        FileOccurrence(revision_id, blob.path)
        for revision_id, _commit, tree in revisions
        for blob in iter_tree_blobs(repository_path, tree)
        if blob.oid == source_blob.oid
    )
    return BlobOccurrences(
        repository, source_revision, normalized, oid_to_hex(source_blob.oid),
        source_blob.size, tuple(item[0] for item in revisions), occurrences,
    )
