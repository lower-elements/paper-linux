"""Populate Paper Linux external resources from a JSON manifest."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Any

from . import catalog_index, code_navigation, ctags_index, database
from .config import (
    DEFAULT_EXTRACTOR_ENV,
    RESOURCE_DATABASE_ENV,
    RESOURCE_ROOT_ENV,
    ResourceError,
    ResourceSettings,
)
from .manager import ResourceManager


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def nonnegative_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return result


def line_range(value: str) -> tuple[int, int | None]:
    try:
        start_text, separator, end_text = value.partition(":")
        start = positive_integer(start_text)
        end = positive_integer(end_text) if separator and end_text else None
    except argparse.ArgumentTypeError as error:
        raise argparse.ArgumentTypeError("must be START or START:END") from error
    if end is not None and end < start:
        raise argparse.ArgumentTypeError("end must not precede start")
    return start, end


def print_catalog(manifest: dict[str, Any]) -> None:
    for heading, resources in (
        ("Documents", manifest.get("documents", [])),
        ("Patches", manifest.get("patches", [])),
    ):
        print(f"{heading}:")
        for resource in resources:
            tags = ", ".join(resource.get("tags", []))
            suffix = f" [{tags}]" if tags else ""
            print(f"  {resource['id']}: {resource.get('description', '')}{suffix}")
    print("Repositories:")
    for repository in manifest.get("repositories", []):
        tags = ", ".join(repository.get("tags", []))
        suffix = f" [{tags}]" if tags else ""
        print(f"  {repository['id']}: {repository.get('description', '')}{suffix}")
        for revision in repository.get("revisions", []):
            print(f"    {revision['id']}: {revision.get('description', '')}")
            for worktree in revision.get("worktrees", []):
                print(f"      worktree {worktree['id']}: {worktree['path']}")


def print_index_report(report: catalog_index.IndexReport) -> None:
    for outcome in report.outcomes:
        print(f"{outcome.action:10} {outcome.document_id} ({outcome.detail})")
    updated = sum(outcome.action != "failed" for outcome in report.outcomes)
    failed = sum(outcome.action == "failed" for outcome in report.outcomes)
    summary = f"{updated} updated, {report.unchanged} already current"
    if failed:
        summary += f", {failed} failed"
    print(summary)


def print_code_index_report(report: ctags_index.CodeIndexReport) -> None:
    for revision in report.revisions:
        identity = f"{revision.repository_id}:{revision.revision_id}"
        detail = (
            f"{revision.paths} paths, {revision.indexed} indexed, "
            f"{revision.reused} reused, {revision.unrecognized} unrecognized"
        )
        if revision.failed:
            detail += f", {revision.failed} failed"
        print(f"{revision.status:10} {identity} ({detail})")
        for warning in revision.warnings:
            print(f"  warning: {warning}")


def print_search_results(results: list[catalog_index.SearchResult]) -> None:
    if not results:
        print("no matches")
        return
    for index, result in enumerate(results):
        if index:
            print()
        pages = ", ".join(str(page) for page in result.pages) or "unknown"
        print(f"{result.resource_id}, PDF page {pages}")
        print(result.description)
        if result.sections:
            print(f"Sections: {'; '.join(result.sections)}")
        print(result.snippet)
        print(result.path)


def print_page_result(result: catalog_index.PageResult) -> None:
    print(f"{result.resource_id}, PDF page {result.page_number}")
    print(result.description)
    if result.sections:
        print(f"Sections: {'; '.join(result.sections)}")
    print(result.path)
    print()
    print(result.content)


def print_section_result(result: catalog_index.SectionResult) -> None:
    print(f"{result.resource_id}, section {result.section_index}: {result.name}")
    print(result.description)
    if result.pages:
        print(f"Pages: {', '.join(str(page) for page in result.pages)}")
    print(result.path)
    print()
    print(result.content)


def print_metadata(items: list[Any], *, json_output: bool) -> None:
    values = [asdict(item) for item in items]
    if json_output:
        print(catalog_index.json_output(values))
        return
    for index, value in enumerate(values):
        if index:
            print()
        identity = value["id"]
        if value.get("repository_id"):
            identity = f"{value['repository_id']}:{identity}"
        if value.get("revision_id"):
            identity = f"{value['repository_id']}:{value['revision_id']}:{value['id']}"
        print(identity)
        if value.get("description"):
            print(value["description"])
        for key in (
            "author", "index", "commit", "tree", "status", "path", "resolved_path"
        ):
            if value.get(key) is not None:
                print(f"{key.replace('_', ' ').title()}: {value[key]}")
        if value.get("derived_from"):
            print(f"Derived from: {value['derived_from']}")
        if value.get("reference_base"):
            reference = value["reference_base"]
            print(f"Reference base: {reference['revision']}")
            print(f"Reason: {reference['reason']}")


def print_revision_comparison(result: Any) -> None:
    print(
        f"{result.repository_id}:{result.from_revision_id}..{result.to_revision_id}"
    )
    if result.path is not None:
        print(f"Path: {result.path}")
    counts = ", ".join(
        f"{status} {count}" for status, count in sorted(result.status_counts.items())
    )
    print(f"Changed paths: {result.total}" + (f" ({counts})" if counts else ""))
    if not result.changes:
        if result.total:
            print("No paths in this page")
        return
    first = result.offset + 1
    last = result.offset + len(result.changes)
    print(f"Showing: {first}-{last} of {result.total}")
    for change in result.changes:
        print(f"{change.status}\t{change.path}")


def print_revision_file_diff(result: Any) -> None:
    if not result.diff:
        print(
            f"no changes: {result.repository_id}:{result.from_revision_id}.."
            f"{result.to_revision_id} {result.path}"
        )
        return
    print(result.diff, end="" if result.diff.endswith("\n") else "\n")


def print_revision_files(result: Any) -> None:
    for match in result.results:
        print(f"{match.blob_oid}\t{match.size} bytes")
        for occurrence in match.occurrences:
            print(f"  {occurrence.revision}:{occurrence.path}")
    if result.truncated:
        print(
            f"Showing {result.offset + 1}-"
            f"{result.offset + len(result.results)} of {result.total}"
        )


def print_source_text_search(result: Any) -> None:
    if not result.results:
        print("no matches")
        return
    for index, match in enumerate(result.results):
        if index:
            print()
        locations = ", ".join(
            f"{item.revision}:{item.path}" for item in match.occurrences
        )
        print(f"{locations}:{match.line} [{match.blob_oid}]")
        if match.containing:
            tag = match.containing[0]
            print(
                f"Containing: [{tag.tag_id}] {tag.kind} "
                f"{tag.qualified_name or tag.name}"
            )
        print(match.source)
    if result.truncated:
        print(
            f"Showing {result.offset + 1}-"
            f"{result.offset + len(result.results)} of {result.total}"
        )


def print_commits(commits: Any, *, truncated: bool) -> None:
    for commit in commits:
        print(f"{commit.commit}\t{commit.authored_at}\t{commit.subject}")
        print(f"  {commit.author} <{commit.author_email}>")
    if truncated:
        print("[history truncated]")


def print_extraction(result: dict[str, Any]) -> None:
    pages_by_chunk: dict[int, list[int]] = {}
    for relation in result["chunk_pages"]:
        pages_by_chunk.setdefault(relation["chunk_index"], []).append(
            relation["page_number"]
        )
    section_names = {
        section["section_index"]: section["name"] for section in result["sections"]
    }
    sections_by_chunk: dict[int, list[str]] = {}
    for relation in result["section_chunks"]:
        sections_by_chunk.setdefault(relation["chunk_index"], []).append(
            section_names[relation["section_index"]]
        )
    for index, chunk in enumerate(result["chunks"]):
        if index:
            print("\n\f")
        print(f"{result['resource_id']}, chunk {chunk['chunk_index']}")
        print(result["description"])
        print(f"Extractor: {result['extractor']} {result['extractor_version']}")
        pages = pages_by_chunk.get(chunk["chunk_index"], [])
        if pages:
            print(f"Pages: {', '.join(str(page) for page in pages)}")
        sections = sections_by_chunk.get(chunk["chunk_index"], [])
        if sections:
            print(f"Sections: {'; '.join(sections)}")
        print(result["path"])
        print()
        print(chunk["content"])


def print_code_tag_search(result: code_navigation.CodeTagSearch) -> None:
    if not result.results:
        print("no matching code tags")
        return
    for tag in result.results:
        identity = tag.qualified_name or tag.name
        end = f"-{tag.line_end}" if tag.line_end and tag.line_end != tag.line_start else ""
        category = "reference" if tag.is_reference else "definition"
        print(
            f"[{tag.tag_id}] {identity} ({tag.language} {tag.kind}, {category}, "
            f"line {tag.line_start}{end})"
        )
        for occurrence in tag.occurrences:
            print(f"  {occurrence.repository}@{occurrence.revision}:{occurrence.path}")
    if result.truncated:
        print(f"More results: --cursor {result.next_cursor}")


def print_code_outline(result: code_navigation.CodeFileOutline) -> None:
    print(f"{result.file.repository}@{result.file.revision}:{result.file.path}")
    if result.file.warning:
        print(f"Warning: {result.file.warning}")
    for item in result.items:
        end = f"-{item.line_end}" if item.line_end and item.line_end != item.line_start else ""
        name = item.qualified_name or item.name
        signature = item.signature or ""
        print(f"{item.line_start}{end}\t[{item.tag_id}]\t{item.kind}\t{name}{signature}")
    if result.truncated:
        print(f"Showing {len(result.items)} of {result.total} tags")


def print_code_source(result: code_navigation.CodeSourceBatch) -> None:
    for index, region in enumerate(result.regions):
        if index:
            print()
        tag_ids = ", ".join(str(tag.tag_id) for tag in region.tags)
        print(
            f"{region.file.repository}@{region.file.revision}:{region.file.path} "
            f"lines {region.line_start}-{region.line_end} [tags {tag_ids}]"
        )
        print(region.source)
        if region.truncated:
            print("[region truncated]")
    if result.truncated:
        print("[output truncated]")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=Path("external-resources.json"))
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list catalogued resources")
    subparsers.add_parser("env", help="print the effective resource environment")
    subparsers.add_parser("path", help="print the configured resource directory")

    for command, help_text in (
        ("repositories", "list Git repositories"),
        ("patches", "list patch artifacts"),
        ("revisions", "list source revisions"),
        ("worktrees", "list revision worktrees"),
    ):
        metadata_parser = subparsers.add_parser(command, help=help_text)
        metadata_parser.add_argument("--json", action="store_true")
        if command in ("repositories", "patches", "revisions"):
            metadata_parser.add_argument("--tag")
        if command == "revisions":
            metadata_parser.add_argument("--author")
            metadata_parser.add_argument("repository", nargs="?")
        if command == "worktrees":
            metadata_parser.add_argument("repository", nargs="?")
            metadata_parser.add_argument("revision", nargs="?")

    repository_parser = subparsers.add_parser("repository", help="show one Git repository")
    repository_parser.add_argument("repository")
    repository_parser.add_argument("--json", action="store_true")
    patch_parser = subparsers.add_parser("patch", help="show one patch artifact")
    patch_parser.add_argument("patch")
    patch_parser.add_argument("--json", action="store_true")
    revision_parser = subparsers.add_parser("revision", help="show one source revision")
    revision_parser.add_argument("repository")
    revision_parser.add_argument("revision")
    revision_parser.add_argument("--json", action="store_true")
    worktree_parser = subparsers.add_parser("worktree", help="show one revision worktree")
    worktree_parser.add_argument("repository")
    worktree_parser.add_argument("revision")
    worktree_parser.add_argument("worktree")
    worktree_parser.add_argument("--json", action="store_true")

    compare_parser = subparsers.add_parser(
        "compare", help="list changed paths between two source revisions"
    )
    compare_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    compare_parser.add_argument("--path", help="restrict changes to one repository path")
    compare_parser.add_argument("--offset", type=nonnegative_integer, default=0)
    compare_parser.add_argument("--limit", type=positive_integer, default=200)
    compare_parser.add_argument("--json", action="store_true", help="print JSON output")
    compare_parser.add_argument("repository")
    compare_parser.add_argument("from_revision", metavar="FROM")
    compare_parser.add_argument("to_revision", metavar="TO")

    diff_parser = subparsers.add_parser(
        "diff", help="show a unified diff for one file between two source revisions"
    )
    diff_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    diff_parser.add_argument("--json", action="store_true", help="print JSON output")
    diff_parser.add_argument("repository")
    diff_parser.add_argument("from_revision", metavar="FROM")
    diff_parser.add_argument("to_revision", metavar="TO")
    diff_parser.add_argument("file", metavar="FILE")

    files_parser = subparsers.add_parser(
        "files", help="find files across pinned source revisions"
    )
    files_parser.add_argument("--root", type=Path)
    files_parser.add_argument("--path-prefix")
    files_parser.add_argument("--glob", action="append", default=[])
    files_parser.add_argument("--offset", type=nonnegative_integer, default=0)
    files_parser.add_argument("--limit", type=positive_integer, default=200)
    files_parser.add_argument("--json", action="store_true")
    files_parser.add_argument("repository")
    files_parser.add_argument("revisions", nargs="+", metavar="REVISION")

    grep_parser = subparsers.add_parser(
        "grep", help="search text across pinned source revisions"
    )
    grep_parser.add_argument("--root", type=Path)
    grep_parser.add_argument("--regex", action="store_true")
    grep_parser.add_argument("--ignore-case", action="store_true")
    grep_parser.add_argument("--path-prefix")
    grep_parser.add_argument("--glob", action="append", default=[])
    grep_parser.add_argument("--context", type=nonnegative_integer, default=0)
    grep_parser.add_argument("--no-scope", action="store_true")
    grep_parser.add_argument("--offset", type=nonnegative_integer, default=0)
    grep_parser.add_argument("--limit", type=positive_integer, default=50)
    grep_parser.add_argument("--json", action="store_true")
    grep_parser.add_argument("repository")
    grep_parser.add_argument("query")
    grep_parser.add_argument("revisions", nargs="+", metavar="REVISION")

    file_index_parser = subparsers.add_parser(
        "file-index", help="explain Ctags index coverage for one revision file"
    )
    file_index_parser.add_argument("--root", type=Path)
    file_index_parser.add_argument("--json", action="store_true")
    file_index_parser.add_argument("repository")
    file_index_parser.add_argument("revision")
    file_index_parser.add_argument("file", metavar="FILE")

    file_history_parser = subparsers.add_parser(
        "file-history", help="show bounded Git history for one revision file"
    )
    file_history_parser.add_argument("--root", type=Path)
    file_history_parser.add_argument("--no-follow", action="store_true")
    file_history_parser.add_argument("--limit", type=positive_integer, default=50)
    file_history_parser.add_argument("--json", action="store_true")
    file_history_parser.add_argument("repository")
    file_history_parser.add_argument("revision")
    file_history_parser.add_argument("file", metavar="FILE")

    history_search_parser = subparsers.add_parser(
        "history-search", help="search changes reachable from a pinned revision"
    )
    history_search_parser.add_argument("--root", type=Path)
    history_search_parser.add_argument("--regex", action="store_true")
    history_search_parser.add_argument("--path")
    history_search_parser.add_argument("--limit", type=positive_integer, default=50)
    history_search_parser.add_argument("--json", action="store_true")
    history_search_parser.add_argument("repository")
    history_search_parser.add_argument("revision")
    history_search_parser.add_argument("query")

    blame_parser = subparsers.add_parser(
        "blame", help="attribute a bounded revision file line range"
    )
    blame_parser.add_argument("--root", type=Path)
    blame_parser.add_argument("--json", action="store_true")
    blame_parser.add_argument("repository")
    blame_parser.add_argument("revision")
    blame_parser.add_argument("file", metavar="FILE")
    blame_parser.add_argument("lines", type=line_range, metavar="START:END")

    blob_parser = subparsers.add_parser(
        "blob-occurrences", help="find revision paths containing an identical blob"
    )
    blob_parser.add_argument("--root", type=Path)
    blob_parser.add_argument("--revision", action="append", dest="revisions")
    blob_parser.add_argument("--json", action="store_true")
    blob_parser.add_argument("repository")
    blob_parser.add_argument("source_revision")
    blob_parser.add_argument("source_path")

    index_parser = subparsers.add_parser(
        "index", help="index documents and selected source revisions"
    )
    index_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    index_parser.add_argument("--extractor", choices=catalog_index.EXTRACTORS)
    index_parser.add_argument("--json", action="store_true", help="print JSON output")
    index_parser.add_argument("resources", nargs="*", metavar="ID", help="document IDs (default: all)")

    index_code_parser = subparsers.add_parser(
        "index-code", help="index selected source revisions with Universal Ctags"
    )
    index_code_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    selectors = index_code_parser.add_mutually_exclusive_group()
    selectors.add_argument(
        "--repository", help="index all index-enabled revisions in one repository"
    )
    selectors.add_argument(
        "--revision", nargs=2, metavar=("REPOSITORY", "REVISION"),
        help="index one source revision",
    )
    index_code_parser.add_argument("--json", action="store_true", help="print JSON output")

    status_parser = subparsers.add_parser(
        "index-status", help="show whether document indexes are current"
    )
    status_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    status_parser.add_argument("--extractor", choices=catalog_index.EXTRACTORS)
    status_parser.add_argument("--json", action="store_true", help="print JSON output")
    status_parser.add_argument("resources", nargs="*", metavar="ID", help="document IDs (default: all)")

    extract_parser = subparsers.add_parser(
        "extract", help="extract document text without modifying the index"
    )
    extract_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    extract_parser.add_argument("--extractor", choices=catalog_index.EXTRACTORS)
    extract_parser.add_argument(
        "--page", type=positive_integer, help="show chunks associated with one page"
    )
    extract_parser.add_argument("--json", action="store_true", help="print JSON output")
    extract_parser.add_argument("resource", metavar="ID", help="document ID")

    search_parser = subparsers.add_parser("search", help="search indexed document text")
    search_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    search_parser.add_argument("--document", metavar="ID", help="restrict matches to one document")
    search_parser.add_argument("--tag", help="restrict matches to documents with this tag")
    search_parser.add_argument("--limit", type=positive_integer, default=10)
    search_parser.add_argument("--fts", action="store_true", help="interpret the query as raw FTS5 syntax")
    search_parser.add_argument("--json", action="store_true", help="print JSON output")
    search_parser.add_argument("query", help="search query")

    hardware_parser = subparsers.add_parser(
        "hardware-search", help="search document and source references together"
    )
    hardware_parser.add_argument("--root", type=Path)
    hardware_parser.add_argument("--repository")
    hardware_parser.add_argument("--revision", action="append", dest="revisions")
    hardware_parser.add_argument("--document-tag")
    hardware_parser.add_argument("--document-limit", type=positive_integer, default=5)
    hardware_parser.add_argument("--tag-limit", type=positive_integer, default=20)
    hardware_parser.add_argument("--source-limit", type=positive_integer, default=20)
    hardware_parser.add_argument("--context", type=nonnegative_integer, default=1)
    hardware_parser.add_argument("--json", action="store_true")
    hardware_parser.add_argument("query")

    page_parser = subparsers.add_parser("page", help="print indexed text for one PDF page")
    page_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    page_parser.add_argument("--json", action="store_true", help="print JSON output")
    page_parser.add_argument("resource", metavar="ID", help="document ID")
    page_parser.add_argument("page_number", type=positive_integer, metavar="PAGE")

    section_parser = subparsers.add_parser(
        "section", help="print indexed text for one extracted document section"
    )
    section_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    section_parser.add_argument("--json", action="store_true", help="print JSON output")
    section_parser.add_argument("resource", metavar="ID", help="document ID")
    section_parser.add_argument("section_index", type=nonnegative_integer, metavar="SECTION")

    tags_parser = subparsers.add_parser("tags", help="search indexed code tags")
    tags_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    for option in (
        "repository", "revision", "path", "name", "qualified-name",
        "language", "kind", "role", "access", "scope", "scope-kind",
    ):
        tags_parser.add_argument(f"--{option}", action="append")
    tags_parser.add_argument("--path-prefix")
    tags_parser.add_argument("--name-prefix")
    tags_parser.add_argument("--qualified-name-prefix")
    tags_parser.add_argument("--tag-id", action="append", type=positive_integer)
    reference = tags_parser.add_mutually_exclusive_group()
    reference.add_argument("--reference", dest="is_reference", action="store_true")
    reference.add_argument("--definition", dest="is_reference", action="store_false")
    tags_parser.set_defaults(is_reference=None)
    tags_parser.add_argument("--cursor")
    tags_parser.add_argument("--limit", type=positive_integer, default=50)
    tags_parser.add_argument("--json", action="store_true")

    outline_parser = subparsers.add_parser("outline", help="outline one indexed source file")
    outline_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    outline_parser.add_argument("--worktree", type=Path, metavar="PATH")
    outline_parser.add_argument("--references", action="store_true")
    outline_parser.add_argument("--limit", type=positive_integer, default=1000)
    outline_parser.add_argument("--json", action="store_true")
    outline_parser.add_argument("repository", nargs="?")
    outline_parser.add_argument("revision", nargs="?")
    outline_parser.add_argument("file", nargs="?")

    outline_scope_parser = subparsers.add_parser(
        "outline-scope", help="outline direct children of one code tag"
    )
    outline_scope_parser.add_argument("--root", type=Path)
    outline_scope_parser.add_argument("--references", action="store_true")
    outline_scope_parser.add_argument("--limit", type=positive_integer, default=1000)
    outline_scope_parser.add_argument("--json", action="store_true")
    outline_scope_parser.add_argument("tag_id", type=positive_integer, metavar="TAG_ID")

    tag_parser = subparsers.add_parser("tag", help="inspect indexed code tags")
    tag_parser.add_argument("--root", type=Path)
    tag_parser.add_argument("--json", action="store_true")
    tag_parser.add_argument("tag_ids", nargs="+", type=positive_integer, metavar="TAG_ID")

    tag_source_parser = subparsers.add_parser(
        "tag-source", help="read and coalesce source for indexed code tags"
    )
    tag_source_parser.add_argument("--root", type=Path)
    tag_source_parser.add_argument("--repository")
    tag_source_parser.add_argument("--revision")
    tag_source_parser.add_argument("--path")
    tag_source_parser.add_argument("--context", type=nonnegative_integer, default=0)
    tag_source_parser.add_argument("--no-line-numbers", action="store_true")
    tag_source_parser.add_argument("--max-lines", type=positive_integer, default=5000)
    tag_source_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    tag_source_parser.add_argument("--json", action="store_true")
    tag_source_parser.add_argument("tag_ids", nargs="+", type=positive_integer, metavar="TAG_ID")

    tag_scope_parser = subparsers.add_parser(
        "tag-scope", help="read enclosing source scopes for indexed tags"
    )
    tag_scope_parser.add_argument("--root", type=Path)
    tag_scope_parser.add_argument("--levels", type=positive_integer, default=1)
    tag_scope_parser.add_argument("--context", type=nonnegative_integer, default=0)
    tag_scope_parser.add_argument("--no-line-numbers", action="store_true")
    tag_scope_parser.add_argument("--max-lines", type=positive_integer, default=5000)
    tag_scope_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    tag_scope_parser.add_argument("--json", action="store_true")
    tag_scope_parser.add_argument("tag_ids", nargs="+", type=positive_integer, metavar="TAG_ID")

    file_source_parser = subparsers.add_parser(
        "file-source", help="read a bounded indexed source file region"
    )
    file_source_parser.add_argument("--root", type=Path)
    file_source_parser.add_argument("--worktree", type=Path, metavar="PATH")
    file_source_parser.add_argument("--lines", type=line_range, default=(1, None))
    file_source_parser.add_argument("--no-line-numbers", action="store_true")
    file_source_parser.add_argument("--max-lines", type=positive_integer, default=5000)
    file_source_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    file_source_parser.add_argument("--json", action="store_true")
    file_source_parser.add_argument("repository", nargs="?")
    file_source_parser.add_argument("revision", nargs="?")
    file_source_parser.add_argument("file", nargs="?")

    tag_diff_parser = subparsers.add_parser(
        "tag-diff", help="diff exactly two indexed code tag regions"
    )
    tag_diff_parser.add_argument("--root", type=Path)
    tag_diff_parser.add_argument("--context", type=nonnegative_integer, default=3)
    tag_diff_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    tag_diff_parser.add_argument("--json", action="store_true")
    tag_diff_parser.add_argument("from_tag", type=positive_integer, metavar="FROM_TAG")
    tag_diff_parser.add_argument("to_tag", type=positive_integer, metavar="TO_TAG")

    facets_parser = subparsers.add_parser(
        "tag-facets", help="describe indexed code coverage and facets"
    )
    facets_parser.add_argument("--root", type=Path)
    facets_parser.add_argument("--json", action="store_true")

    references_parser = subparsers.add_parser(
        "references", help="find best-effort parser-reported symbol references"
    )
    references_parser.add_argument("--root", type=Path)
    references_parser.add_argument("--repository")
    references_parser.add_argument("--revision")
    references_parser.add_argument("--path")
    references_parser.add_argument("--role", action="append")
    references_parser.add_argument("--cursor")
    references_parser.add_argument("--limit", type=positive_integer, default=50)
    references_parser.add_argument("--json", action="store_true")
    references_parser.add_argument("symbol")

    locate_parser = subparsers.add_parser(
        "locate", help="locate indexed tags at one source line"
    )
    locate_parser.add_argument("--root", type=Path)
    locate_parser.add_argument("--worktree", type=Path, metavar="PATH")
    locate_parser.add_argument("--nearby", type=positive_integer, default=5)
    locate_parser.add_argument("--json", action="store_true")
    locate_parser.add_argument(
        "coordinates", nargs="+",
        help="REPOSITORY REVISION FILE LINE, or LINE with --worktree",
    )

    read_line_parser = subparsers.add_parser(
        "read-line", help="read the enclosing tag or context at one source line"
    )
    read_line_parser.add_argument("--root", type=Path)
    read_line_parser.add_argument("--worktree", type=Path, metavar="PATH")
    read_line_parser.add_argument("--fallback-context", type=nonnegative_integer, default=10)
    read_line_parser.add_argument("--context", type=nonnegative_integer, default=0)
    read_line_parser.add_argument("--no-line-numbers", action="store_true")
    read_line_parser.add_argument("--max-lines", type=positive_integer, default=5000)
    read_line_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    read_line_parser.add_argument("--json", action="store_true")
    read_line_parser.add_argument(
        "coordinates", nargs="+",
        help="REPOSITORY REVISION FILE LINE, or LINE with --worktree",
    )

    outline_diff_parser = subparsers.add_parser(
        "outline-diff", help="compare definitions in two revision files"
    )
    outline_diff_parser.add_argument("--root", type=Path)
    outline_diff_parser.add_argument("--to-path")
    outline_diff_parser.add_argument("--json", action="store_true")
    outline_diff_parser.add_argument("repository")
    outline_diff_parser.add_argument("from_revision", metavar="FROM")
    outline_diff_parser.add_argument("to_revision", metavar="TO")
    outline_diff_parser.add_argument("file", metavar="FILE")

    symbol_diff_parser = subparsers.add_parser(
        "symbol-diff", help="compare one definition across indexed revisions"
    )
    symbol_diff_parser.add_argument("--root", type=Path)
    symbol_diff_parser.add_argument("--from-path")
    symbol_diff_parser.add_argument("--to-path")
    symbol_diff_parser.add_argument("--kind")
    symbol_diff_parser.add_argument("--qualified", action="store_true")
    symbol_diff_parser.add_argument("--context", type=nonnegative_integer, default=3)
    symbol_diff_parser.add_argument("--max-chars", type=positive_integer, default=200_000)
    symbol_diff_parser.add_argument("--json", action="store_true")
    symbol_diff_parser.add_argument("repository")
    symbol_diff_parser.add_argument("from_revision", metavar="FROM")
    symbol_diff_parser.add_argument("to_revision", metavar="TO")
    symbol_diff_parser.add_argument("symbol")

    history_parser = subparsers.add_parser(
        "history", help="trace a code symbol through indexed revisions"
    )
    history_parser.add_argument("--root", type=Path)
    history_parser.add_argument("--path")
    history_parser.add_argument("--qualified", action="store_true")
    history_parser.add_argument("--json", action="store_true")
    history_parser.add_argument("repository")
    history_parser.add_argument("symbol")

    for command, help_text in (("populate", "fetch and prepare resources"), ("check", "check an existing resource directory")):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--root", type=Path, help="override the configured resource directory")
        selectors = subparser.add_mutually_exclusive_group()
        selectors.add_argument("--repository")
        selectors.add_argument("--revision", nargs=2, metavar=("REPOSITORY", "REVISION"))
        selectors.add_argument("--patch")
        selectors.add_argument(
            "--worktree", nargs=3,
            metavar=("REPOSITORY", "REVISION", "WORKTREE"),
        )
        subparser.add_argument("resources", nargs="*", metavar="ID", help="resource IDs (default: all)")
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    manager: ResourceManager | None = None
    try:
        settings = ResourceSettings.load(
            args.manifest, getattr(args, "root", None)
        )
        if args.command == "list":
            manager = ResourceManager.load(settings)
            print_catalog(manager.manifest)
            return 0
        if args.command in ("env", "path"):
            if args.command == "env":
                print(f"{RESOURCE_ROOT_ENV}={settings.root}")
                print(f"{RESOURCE_DATABASE_ENV}={settings.database}")
                print(f"{DEFAULT_EXTRACTOR_ENV}={settings.default_extractor}")
            else:
                print(settings.root)
            return 0

        manager = ResourceManager.load(settings)

        if args.command in ("repositories", "repository", "patches", "patch", "revisions", "revision", "worktrees", "worktree"):
            if args.command == "repositories":
                items = manager.list_repositories(args.tag)
            elif args.command == "repository":
                items = [manager.get_repository(args.repository)]
            elif args.command == "patches":
                items = manager.list_patches(args.tag)
            elif args.command == "patch":
                items = [manager.get_patch(args.patch)]
            elif args.command == "revisions":
                items = manager.list_revisions(args.repository, args.author, args.tag)
            elif args.command == "revision":
                items = [manager.get_revision(args.repository, args.revision)]
            elif args.command == "worktrees":
                items = manager.list_worktrees(args.repository, args.revision)
            else:
                items = [manager.get_worktree(args.repository, args.revision, args.worktree)]
            print_metadata(items, json_output=args.json)
            return 0
        if args.command == "compare":
            comparison = manager.compare_revisions(
                args.repository,
                args.from_revision,
                args.to_revision,
                path=args.path,
                offset=args.offset,
                limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(comparison)))
            else:
                print_revision_comparison(comparison)
            return 0
        if args.command == "diff":
            file_diff = manager.diff_revision_file(
                args.repository, args.from_revision, args.to_revision, args.file
            )
            if args.json:
                print(catalog_index.json_output(asdict(file_diff)))
            else:
                print_revision_file_diff(file_diff)
            return 0
        if args.command == "files":
            result = manager.find_revision_files(
                args.repository, args.revisions, path_prefix=args.path_prefix,
                globs=args.glob, offset=args.offset, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_revision_files(result)
            return 0
        if args.command == "grep":
            result = manager.search_source_text(
                args.repository, args.revisions, args.query, regex=args.regex,
                case_sensitive=not args.ignore_case,
                path_prefix=args.path_prefix, globs=args.glob,
                context_lines=args.context, resolve_scope=not args.no_scope,
                offset=args.offset, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_source_text_search(result)
            return 0
        if args.command == "file-index":
            result = manager.describe_file_index(
                args.repository, args.revision, args.file
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.repository}@{result.revision}:{result.path}")
                print(f"Present: {result.present}")
                print(f"Revision indexing enabled: {result.revision_index_enabled}")
                print(f"Selected by patterns: {result.selected_by_patterns}")
                print(f"Indexed: {result.indexed}")
                if result.blob_oid:
                    print(f"Blob: {result.blob_oid} ({result.size} bytes)")
                if result.analysis_input_name:
                    print(f"Analysis input: {result.analysis_input_name}")
                if result.language:
                    print(f"Language: {result.language}")
                print(f"Tags: {result.tag_count}")
            return 0
        if args.command == "file-history":
            result = manager.show_file_history(
                args.repository, args.revision, args.file,
                follow=not args.no_follow, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_commits(result.commits, truncated=result.truncated)
            return 0
        if args.command == "history-search":
            result = manager.search_revision_history(
                args.repository, args.revision, args.query,
                regex=args.regex, path=args.path, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_commits(result.commits, truncated=result.truncated)
            return 0
        if args.command == "blame":
            start, end = args.lines
            if end is None:
                end = start
            result = manager.blame_file_lines(
                args.repository, args.revision, args.file,
                line_start=start, line_end=end,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                for line in result.lines:
                    print(
                        f"{line.line}\t{line.commit}\t{line.author}\t{line.text}"
                    )
            return 0
        if args.command == "blob-occurrences":
            result = manager.find_blob_occurrences(
                args.repository, args.source_revision, args.source_path,
                revision_ids=args.revisions,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.blob_oid}\t{result.size} bytes")
                for occurrence in result.occurrences:
                    print(f"{occurrence.revision}:{occurrence.path}")
            return 0
        if args.command in ("index", "index-status"):
            if args.command == "index":
                report = manager.index_documents(args.resources, args.extractor)
                code_report = (
                    manager.index_code()
                    if not args.resources
                    else ctags_index.CodeIndexReport(())
                )
                if args.json:
                    output = report.to_dict()
                    output["code"] = code_report.to_dict()
                    print(catalog_index.json_output(output))
                else:
                    print_index_report(report)
                    print_code_index_report(code_report)
                return 1 if report.failed or code_report.failed else 0
            statuses = manager.index_status(args.resources, args.extractor)
            if args.json:
                print(catalog_index.json_output([status.to_dict() for status in statuses]))
            else:
                for status in statuses:
                    print(f"{status.status:10} {status.document_id} ({status.detail})")
            return 0 if all(status.status == "ok" for status in statuses) else 1

        if args.command == "index-code":
            repository_id = args.repository
            revision_id = None
            if args.revision is not None:
                selected_repository, revision_id = args.revision
                repository_id = selected_repository
            code_report = manager.index_code(repository_id, revision_id)
            if args.json:
                print(catalog_index.json_output(code_report.to_dict()))
            else:
                print_code_index_report(code_report)
            return 1 if code_report.failed else 0

        if args.command == "extract":
            extraction = manager.extract_document(
                args.resource, args.extractor, args.page
            )
            if args.json:
                print(catalog_index.json_output(extraction))
            else:
                print_extraction(extraction)
            return 0

        if args.command == "search":
            results = manager.search_documents(
                args.query,
                raw_fts=args.fts,
                document_id=args.document,
                tag=args.tag,
                limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output([result.to_dict() for result in results]))
            else:
                print_search_results(results)
            return 0

        if args.command == "hardware-search":
            result = manager.search_hardware_references(
                args.query, repository=args.repository,
                revisions=args.revisions, document_tag=args.document_tag,
                document_limit=args.document_limit, tag_limit=args.tag_limit,
                source_limit=args.source_limit, context_lines=args.context,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print("Documents:")
                print_search_results(list(result.documents))
                print("\nCode tags:")
                print_code_tag_search(code_navigation.CodeTagSearch(
                    result.tags, None, False
                ))
                print("\nSource text:")
                for search in result.source_searches:
                    print_source_text_search(search)
                for warning in result.warnings:
                    print(f"Warning: {warning}")
            return 0

        if args.command == "page":
            page = manager.get_document_page(args.resource, args.page_number)
            if args.json:
                print(catalog_index.json_output(page.to_dict()))
            else:
                print_page_result(page)
            return 0

        if args.command == "section":
            section = manager.get_document_section(args.resource, args.section_index)
            if args.json:
                print(catalog_index.json_output(section.to_dict()))
            else:
                print_section_result(section)
            return 0

        if args.command == "tags":
            result = manager.search_code_tags(
                repository=args.repository, revision=args.revision, path=args.path,
                path_prefix=args.path_prefix, tag_id=args.tag_id, name=args.name,
                name_prefix=args.name_prefix, qualified_name=args.qualified_name,
                qualified_name_prefix=args.qualified_name_prefix,
                language=args.language, kind=args.kind, role=args.role,
                access=args.access, scope=args.scope, scope_kind=args.scope_kind,
                is_reference=args.is_reference, cursor=args.cursor, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_code_tag_search(result)
            return 0

        if args.command == "outline":
            result = manager.outline_code_file(
                args.repository, args.revision, args.file,
                worktree_path=args.worktree,
                include_references=args.references, limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_code_outline(result)
            return 0

        if args.command == "outline-scope":
            result = manager.outline_code_scope(
                args.tag_id, include_references=args.references, limit=args.limit
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"[{result.scope.tag_id}] {result.scope.qualified_name or result.scope.name}")
                for item in result.children:
                    print(f"{item.line_start}\t[{item.tag_id}]\t{item.kind}\t{item.qualified_name or item.name}")
            return 0

        if args.command == "tag":
            result = manager.inspect_code_tags(args.tag_ids)
            if args.json:
                print(catalog_index.json_output([asdict(item) for item in result]))
            else:
                for index, item in enumerate(result):
                    if index:
                        print()
                    print(catalog_index.json_output(asdict(item)))
            return 0

        if args.command == "tag-source":
            result = manager.read_tagged_code(
                args.tag_ids, repository=args.repository, revision=args.revision,
                path=args.path, context_lines=args.context,
                numbered=not args.no_line_numbers, max_lines=args.max_lines,
                max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_code_source(result)
            return 0

        if args.command == "tag-scope":
            result = manager.read_enclosing_code_scope(
                args.tag_ids, levels=args.levels, context_lines=args.context,
                numbered=not args.no_line_numbers, max_lines=args.max_lines,
                max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                for scope in result.scopes:
                    target = scope.scope_tag_id if scope.scope_tag_id is not None else "none"
                    print(f"tag {scope.requested_tag_id} -> scope {target}" + (f" ({scope.message})" if scope.message else ""))
                print_code_source(result.source)
            return 0

        if args.command == "file-source":
            start, end = args.lines
            result = manager.read_code_file(
                repository=args.repository, revision=args.revision, path=args.file,
                worktree_path=args.worktree, line_start=start, line_end=end,
                numbered=not args.no_line_numbers, max_lines=args.max_lines,
                max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.file.repository}@{result.file.revision}:{result.file.path} lines {result.line_start}-{result.line_end}")
                if result.file.warning:
                    print(f"Warning: {result.file.warning}")
                print(result.source)
                if result.truncated:
                    print("[output truncated]")
            return 0

        if args.command == "tag-diff":
            result = manager.diff_tagged_code(
                args.from_tag, args.to_tag, context_lines=args.context,
                max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            elif result.diff:
                print(result.diff, end="" if result.diff.endswith("\n") else "\n")
            else:
                print("no changes")
            return 0

        if args.command == "tag-facets":
            result = manager.describe_code_index()
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.repositories} repositories, {result.revisions} revisions, {result.paths} paths")
                print(f"{result.blobs} blobs, {result.analyses} analyses, {result.tags} tags")
                print("Languages: " + ", ".join(f"{name} ({total})" for name, total in result.languages))
                print("Kinds: " + ", ".join(f"{name} ({total})" for name, total in result.kinds))
            return 0

        if args.command == "references":
            result = manager.find_code_references(
                args.symbol, repository=args.repository, revision=args.revision,
                path=args.path, role=args.role, cursor=args.cursor,
                limit=args.limit,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print_code_tag_search(result)
            return 0

        if args.command == "locate":
            if args.worktree is not None and len(args.coordinates) == 1:
                repository = revision = file = None
                line = positive_integer(args.coordinates[0])
            elif args.worktree is None and len(args.coordinates) == 4:
                repository, revision, file, line_text = args.coordinates
                line = positive_integer(line_text)
            else:
                raise ResourceError(
                    "locate expects REPOSITORY REVISION FILE LINE, or --worktree PATH LINE"
                )
            result = manager.locate_code_at_line(
                repository=repository, revision=revision,
                path=file, worktree_path=args.worktree, line=line,
                nearby_limit=args.nearby,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.file.repository}@{result.file.revision}:{result.file.path}:{result.line}")
                items = result.containing or result.nearby
                heading = "Containing" if result.containing else "Nearby"
                print(f"{heading} tags:")
                for item in items:
                    print(f"  [{item.tag_id}] {item.kind} {item.qualified_name or item.name} at line {item.line_start}")
                if result.enclosing_chain:
                    print("Enclosing chain: " + " -> ".join(
                        item.qualified_name or item.name
                        for item in result.enclosing_chain
                    ))
            return 0

        if args.command == "read-line":
            if args.worktree is not None and len(args.coordinates) == 1:
                repository = revision = file = None
                line = positive_integer(args.coordinates[0])
            elif args.worktree is None and len(args.coordinates) == 4:
                repository, revision, file, line_text = args.coordinates
                line = positive_integer(line_text)
            else:
                raise ResourceError(
                    "read-line expects REPOSITORY REVISION FILE LINE, or "
                    "--worktree PATH LINE"
                )
            result = manager.read_code_at_line(
                repository=repository, revision=revision, path=file,
                worktree_path=args.worktree, line=line,
                fallback_context_lines=args.fallback_context,
                context_lines=args.context, numbered=not args.no_line_numbers,
                max_lines=args.max_lines, max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(
                    f"{result.source.file.repository}@{result.source.file.revision}:"
                    f"{result.source.file.path}:{line} ({result.mode})"
                )
                if result.selected_tag is not None:
                    tag = result.selected_tag
                    print(f"[{tag.tag_id}] {tag.kind} {tag.qualified_name or tag.name}")
                print(result.source.source)
                if result.source.truncated:
                    print("[output truncated]")
            return 0

        if args.command == "outline-diff":
            result = manager.compare_code_file_outlines(
                args.repository, args.from_revision, args.to_revision,
                args.file, to_path=args.to_path,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                counts = ", ".join(
                    f"{status} {total}"
                    for status, total in sorted(result.status_counts.items())
                )
                print(counts)
                for change in result.changes:
                    print(f"{change.status}\t{change.kind}\t{change.symbol}")
            return 0

        if args.command == "symbol-diff":
            result = manager.compare_symbol_definitions(
                args.repository, args.from_revision, args.to_revision,
                args.symbol, from_path=args.from_path, to_path=args.to_path,
                kind=args.kind, qualified=args.qualified,
                context_lines=args.context, max_chars=args.max_chars,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                print(f"{result.status}: {result.symbol}")
                if result.diff is not None and result.diff.diff:
                    print(
                        result.diff.diff,
                        end="" if result.diff.diff.endswith("\n") else "\n",
                    )
                else:
                    for side in (result.from_side, result.to_side):
                        tags = ", ".join(
                            f"[{tag.tag_id}] {tag.qualified_name or tag.name}"
                            for tag in side.matches
                        ) or "no match"
                        print(f"{side.revision}: {tags}")
            return 0

        if args.command == "history":
            result = manager.trace_code_symbol_history(
                args.repository, args.symbol, path=args.path,
                qualified=args.qualified,
            )
            if args.json:
                print(catalog_index.json_output(asdict(result)))
            else:
                for step in result.steps:
                    revisions = ", ".join(step.revisions)
                    if not step.matches:
                        print(f"{revisions}: no indexed match")
                    else:
                        tags = ", ".join(
                            f"[{tag.tag_id}] {tag.qualified_name or tag.name}"
                            for tag in step.matches
                        )
                        print(f"{revisions}: {tags}" + (" (ambiguous)" if step.ambiguous else ""))
            return 0

        if args.command == "populate":
            manager.settings.root.mkdir(parents=True, exist_ok=True)
            print("\n".join(manager.populate(
                args.resources,
                repository_id=args.repository,
                revision_key=tuple(args.revision) if args.revision else None,
                patch_id=args.patch,
                worktree_key=tuple(args.worktree) if args.worktree else None,
            )))
            return 0
        success, messages = manager.check(
            args.resources,
            repository_id=args.repository,
            revision_key=tuple(args.revision) if args.revision else None,
            patch_id=args.patch,
            worktree_key=tuple(args.worktree) if args.worktree else None,
        )
        print("\n".join(messages))
        return 0 if success else 1
    except (
        ResourceError,
        catalog_index.CatalogIndexError,
        database.DatabaseError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
