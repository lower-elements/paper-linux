"""Pure destination planning for workspace commit ranges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Iterable, Mapping

from .config import ResourceError


_NUMBERED = re.compile(r"^(\d+)-(.+)\.patch$")


@dataclass(frozen=True)
class ExportCommit:
    oid: str
    subject: str
    annotations: tuple[str, ...]


@dataclass(frozen=True)
class PlannedPatch:
    commit: str
    subject: str
    path: str
    synthesized: bool


@dataclass(frozen=True)
class ExportPlan:
    patches: tuple[PlannedPatch, ...]
    obsolete_paths: tuple[str, ...]
    other_selected_paths: tuple[str, ...]
    warnings: tuple[str, ...]


def _directory(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent


def _number(path: str) -> int | None:
    match = _NUMBERED.fullmatch(PurePosixPath(path).name)
    return int(match.group(1)) if match else None


def _slug(subject: str) -> str:
    value = subject
    value = re.sub(r"^\[PATCH(?:\s+[^]]*)?]\s*", "", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (value or "change")[:72].rstrip("-")


def _allocate_run(
    commits: list[ExportCommit],
    previous_path: str | None,
    following_path: str | None,
    directories: tuple[str, ...],
    occupied_numbers: Mapping[str, set[int]],
) -> list[str]:
    previous_directory = _directory(previous_path) if previous_path else None
    following_directory = _directory(following_path) if following_path else None
    if previous_directory is not None and following_directory is not None:
        if previous_directory != following_directory:
            affected = ", ".join(item.oid[:12] for item in commits)
            raise ResourceError(
                f"cannot infer patch layer for commits {affected}: anchors are in "
                f"{previous_directory} and {following_directory}; annotate the commits"
            )
        directory = previous_directory
    else:
        directory = previous_directory or following_directory
    if directory is None:
        raise ResourceError(
            "an entirely unannotated export range requires at least one explicit annotation"
        )
    if directory not in directories:
        raise ResourceError(f"annotation selects an unconfigured patch directory: {directory}")
    previous_number = _number(previous_path) if previous_path else None
    following_number = _number(following_path) if following_path else None
    if previous_path and previous_number is None:
        raise ResourceError(
            f"cannot allocate after non-numbered anchor {previous_path}; annotate the affected commits"
        )
    if following_path and following_number is None:
        raise ResourceError(
            f"cannot allocate before non-numbered anchor {following_path}; annotate the affected commits"
        )
    occupied = set(occupied_numbers.get(directory, set()))
    if previous_number is not None:
        occupied.discard(previous_number)
    if following_number is not None:
        occupied.discard(following_number)
    if previous_number is not None and following_number is not None:
        available = [
            number for number in range(previous_number + 1, following_number)
            if number not in occupied
        ]
        if len(available) < len(commits):
            raise ResourceError(
                f"no ordered filename slots remain between {previous_path} and "
                f"{following_path} for {len(commits)} commit(s)"
            )
        numbers = available[:len(commits)]
    elif following_number is not None:
        available = [
            number for number in range(1, following_number)
            if number not in occupied
        ]
        if len(available) < len(commits):
            raise ResourceError(
                f"no positive filename slots remain before {following_path} "
                f"for {len(commits)} commit(s)"
            )
        numbers = available[-len(commits):]
    else:
        assert previous_number is not None
        numbers = []
        candidate = previous_number + 1
        while len(numbers) < len(commits):
            if candidate not in occupied:
                numbers.append(candidate)
            candidate += 1
    width = max(4, *(len(str(number)) for number in numbers))
    return [
        f"{directory}/{number:0{width}d}-{_slug(commit.subject)}.patch"
        for number, commit in zip(numbers, commits)
    ]


def plan_export(
    commits: Iterable[ExportCommit],
    *,
    directories: Iterable[str],
    existing_paths: Iterable[str],
    managed_paths: Iterable[str],
) -> ExportPlan:
    """Allocate every commit without touching notes or destination files."""
    commits = list(commits)
    directories = tuple(dict.fromkeys(directories))
    if not commits:
        raise ResourceError("workspace export range has no commits")
    directory_order = {directory: index for index, directory in enumerate(directories)}
    explicit: list[str | None] = []
    used_paths: set[str] = set()
    duplicate_anchor: dict[int, str] = {}
    for index, commit in enumerate(commits):
        annotations = tuple(dict.fromkeys(commit.annotations))
        if annotations == ("<malformed>",):
            raise ResourceError(
                f"commit {commit.oid[:12]} has malformed workspace note data"
            )
        if len(annotations) > 1:
            raise ResourceError(
                f"commit {commit.oid[:12]} has conflicting patch annotations: "
                + ", ".join(annotations)
            )
        path = annotations[0] if annotations else None
        if path is not None:
            pure = PurePosixPath(path)
            if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".patch":
                raise ResourceError(
                    f"commit {commit.oid[:12]} has unsafe patch destination {path!r}"
                )
            if _directory(path) not in directory_order:
                raise ResourceError(
                    f"commit {commit.oid[:12]} selects an unconfigured patch directory: "
                    f"{_directory(path)}"
                )
            if path in used_paths:
                duplicate_anchor[index] = path
                path = None
            else:
                used_paths.add(path)
        explicit.append(path)
    if not used_paths:
        raise ResourceError(
            "an entirely unannotated export range requires at least one explicit annotation"
        )

    existing_paths = set(existing_paths)
    occupied_numbers: dict[str, set[int]] = {directory: set() for directory in directories}
    for path in existing_paths | used_paths:
        if _directory(path) in occupied_numbers and (number := _number(path)) is not None:
            occupied_numbers[_directory(path)].add(number)

    assigned = list(explicit)
    index = 0
    while index < len(commits):
        if assigned[index] is not None:
            index += 1
            continue
        start = index
        while index < len(commits) and assigned[index] is None:
            index += 1
        end = index
        previous = assigned[start - 1] if start else None
        if previous is None and start in duplicate_anchor:
            previous = duplicate_anchor[start]
        following = assigned[end] if end < len(commits) else None
        paths = _allocate_run(
            commits[start:end], previous, following, directories, occupied_numbers
        )
        for offset, path in enumerate(paths, start=start):
            assigned[offset] = path
            number = _number(path)
            if number is not None:
                occupied_numbers[_directory(path)].add(number)

    assert all(path is not None for path in assigned)
    resolved = [path for path in assigned if path is not None]
    if len(set(resolved)) != len(resolved):
        raise ResourceError("export plan contains duplicate patch destinations")
    numbered: dict[tuple[str, int], str] = {}
    for path in resolved:
        number = _number(path)
        if number is None:
            continue
        key = (_directory(path), number)
        previous = numbered.get(key)
        if previous is not None and previous != path:
            raise ResourceError(
                f"numeric patch prefix collision: {previous} and {path}"
            )
        numbered[key] = path
    for existing in existing_paths:
        number = _number(existing)
        key = (_directory(existing), number) if number is not None else None
        if key is not None and key in numbered and numbered[key] != existing:
            raise ResourceError(
                f"numeric patch prefix collision: {existing} and {numbered[key]}"
            )
    order = [
        (directory_order[_directory(path)], PurePosixPath(path).name)
        for path in resolved
    ]
    if order != sorted(order):
        raise ResourceError(
            "planned destinations do not preserve Buildroot directory/lexical order; "
            "annotate commits or rename paths explicitly"
        )
    managed_paths = set(managed_paths)
    planned_set = set(resolved)
    obsolete = tuple(sorted(managed_paths - planned_set))
    other = tuple(sorted(existing_paths - planned_set))
    warnings: list[str] = []
    if obsolete:
        warnings.append(
            "obsolete managed patch files were not removed: " + ", ".join(obsolete)
        )
    if other:
        warnings.append(
            "other selected patch files are outside the export plan and replay check: "
            + ", ".join(other)
        )
    return ExportPlan(
        tuple(
            PlannedPatch(
                commit.oid, commit.subject, path,
                explicit[index] is None,
            )
            for index, (commit, path) in enumerate(zip(commits, resolved))
        ),
        obsolete, other, tuple(warnings),
    )
