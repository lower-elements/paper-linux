"""Bounded Git history and blame queries for pinned source revisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from .config import ResourceError
from .git_resources import validate_repository_path


@dataclass(frozen=True, slots=True)
class CommitInfo:
    commit: str
    authored_at: str
    author: str
    author_email: str
    subject: str


@dataclass(frozen=True, slots=True)
class FileHistory:
    repository: str
    revision: str
    path: str
    commits: tuple[CommitInfo, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class HistorySearch:
    repository: str
    revision: str
    query: str
    regex: bool
    path: str | None
    commits: tuple[CommitInfo, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class BlameLine:
    line: int
    original_line: int
    commit: str
    author: str
    author_email: str
    authored_at: int
    summary: str
    text: str


@dataclass(frozen=True, slots=True)
class FileBlame:
    repository: str
    revision: str
    path: str
    line_start: int
    line_end: int
    lines: tuple[BlameLine, ...]


def _run(repository_path: Path, arguments: list[str]) -> bytes:
    command = ["git", "--git-dir", str(repository_path), *arguments]
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except FileNotFoundError as error:
        raise ResourceError("git is required to inspect repository history") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ResourceError(f"Git history query failed: {detail or result.returncode}")
    return result.stdout


def _commits(
    repository_path: Path, arguments: list[str], limit: int
) -> tuple[tuple[CommitInfo, ...], bool]:
    if limit < 1:
        raise ResourceError("history limit must be positive")
    format_string = "%H%x1f%aI%x1f%an%x1f%ae%x1f%s%x1e"
    output = _run(repository_path, [
        "log", f"--max-count={limit + 1}", f"--format={format_string}",
        *arguments,
    ]).decode("utf-8", errors="replace")
    records = []
    for raw in output.split("\x1e"):
        raw = raw.strip("\r\n")
        if not raw:
            continue
        fields = raw.split("\x1f", 4)
        if len(fields) != 5:
            raise ResourceError("git log returned malformed metadata")
        records.append(CommitInfo(*fields))
    return tuple(records[:limit]), len(records) > limit


def file_history(
    repository_path: Path,
    repository: str,
    revision: str,
    commit: str,
    path: str,
    *,
    follow: bool = True,
    limit: int = 50,
) -> FileHistory:
    normalized = validate_repository_path(path)
    arguments = ["--follow"] if follow else []
    arguments.append(commit)
    arguments.extend(["--", normalized])
    commits, truncated = _commits(repository_path, arguments, limit)
    return FileHistory(repository, revision, normalized, commits, truncated)


def search_history(
    repository_path: Path,
    repository: str,
    revision: str,
    commit: str,
    query: str,
    *,
    regex: bool = False,
    path: str | None = None,
    limit: int = 50,
) -> HistorySearch:
    if not query:
        raise ResourceError("history search query must not be empty")
    normalized = validate_repository_path(path) if path else None
    arguments = [f"-G{query}" if regex else f"-S{query}", commit]
    if normalized:
        arguments.extend(["--", normalized])
    commits, truncated = _commits(repository_path, arguments, limit)
    return HistorySearch(
        repository, revision, query, regex, normalized, commits, truncated
    )


_BLAME_HEADER = re.compile(r"^([0-9a-f]+) (\d+) (\d+)(?: (\d+))?$")


def blame_lines(
    repository_path: Path,
    repository: str,
    revision: str,
    commit: str,
    path: str,
    *,
    line_start: int,
    line_end: int,
) -> FileBlame:
    if line_start < 1 or line_end < line_start or line_end - line_start >= 500:
        raise ResourceError("blame range must contain between 1 and 500 lines")
    normalized = validate_repository_path(path)
    output = _run(repository_path, [
        "blame", "--line-porcelain", "-L", f"{line_start},{line_end}",
        commit, "--", normalized,
    ]).decode("utf-8", errors="replace")
    lines: list[BlameLine] = []
    current: dict[str, str | int] | None = None
    for raw in output.splitlines():
        header = _BLAME_HEADER.match(raw)
        if header:
            current = {
                "commit": header.group(1),
                "original_line": int(header.group(2)),
                "line": int(header.group(3)),
            }
            continue
        if current is None:
            continue
        if raw.startswith("\t"):
            lines.append(BlameLine(
                int(current["line"]), int(current["original_line"]),
                str(current["commit"]), str(current.get("author", "")),
                str(current.get("author-mail", "")).strip("<>"),
                int(current.get("author-time", 0)),
                str(current.get("summary", "")), raw[1:],
            ))
            current = None
            continue
        key, separator, value = raw.partition(" ")
        if separator and key in {"author", "author-mail", "author-time", "summary"}:
            current[key] = value
    return FileBlame(
        repository, revision, normalized, line_start, line_end, tuple(lines)
    )
