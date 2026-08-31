"""Populate Paper Linux external resources from a JSON manifest."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Any

from . import catalog_index, ctags_index, database
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

    index_parser = subparsers.add_parser(
        "index", help="index documents and selected source revisions"
    )
    index_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    index_parser.add_argument("--extractor", choices=catalog_index.EXTRACTORS)
    index_parser.add_argument("--json", action="store_true", help="print JSON output")
    index_parser.add_argument("resources", nargs="*", metavar="ID", help="document IDs (default: all)")

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
