"""Populate Paper Linux external resources from a JSON manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from . import catalog_index
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
    for heading, resources in (("Documents", manifest.get("documents", [])), ("Repositories", manifest.get("repositories", []))):
        print(f"{heading}:")
        for resource in resources:
            tags = ", ".join(resource.get("tags", []))
            suffix = f" [{tags}]" if tags else ""
            print(f"  {resource['id']}: {resource.get('description', '')}{suffix}")
            for worktree in resource.get("worktrees", []):
                print(f"    {worktree['id']}: {worktree['ref']}")


def print_index_report(report: catalog_index.IndexReport) -> None:
    for outcome in report.outcomes:
        print(f"{outcome.action:10} {outcome.document_id} ({outcome.detail})")
    updated = sum(outcome.action != "failed" for outcome in report.outcomes)
    failed = sum(outcome.action == "failed" for outcome in report.outcomes)
    summary = f"{updated} updated, {report.unchanged} already current"
    if failed:
        summary += f", {failed} failed"
    print(summary)


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

    index_parser = subparsers.add_parser("index", help="index document text for search")
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
        if args.command in ("index", "index-status"):
            if args.command == "index":
                report = manager.index_documents(args.resources, args.extractor)
                if args.json:
                    print(catalog_index.json_output(report.to_dict()))
                else:
                    print_index_report(report)
                return 1 if report.failed else 0
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
            print("\n".join(manager.populate(args.resources)))
            return 0
        success, messages = manager.check(args.resources)
        print("\n".join(messages))
        return 0 if success else 1
    except (ResourceError, catalog_index.CatalogIndexError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
