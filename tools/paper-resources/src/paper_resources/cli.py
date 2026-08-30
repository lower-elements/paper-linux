"""Populate Paper Linux external resources from a JSON manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from . import catalog_index


class ResourceError(RuntimeError):
    pass


RESOURCE_ROOT_ENV = "PAPER_RESOURCES_DIR"
RESOURCE_DATABASE_ENV = "PAPER_RESOURCES_DB"
PDF_BACKEND_ENV = "PAPER_RESOURCES_PDF_BACKEND"


def load_environment(manifest_path: Path) -> None:
    """Load the repository-local .env without overriding the shell."""
    load_dotenv(
        dotenv_path=manifest_path.expanduser().resolve().parent / ".env",
        override=False,
    )


def resolve_root(manifest_path: Path, explicit_root: Path | None = None) -> Path:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    configured = os.environ.get(RESOURCE_ROOT_ENV)
    if not configured:
        raise ResourceError(
            f"{RESOURCE_ROOT_ENV} is not configured; set it in .env or pass --root PATH"
        )
    root = Path(configured).expanduser()
    if not root.is_absolute():
        root = manifest_path.expanduser().resolve().parent / root
    return root.resolve()


def resolve_database(root: Path) -> Path:
    configured = os.environ.get(RESOURCE_DATABASE_ENV)
    if not configured:
        return root / "resources.db"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def resolve_pdf_backend(explicit_backend: str | None = None) -> str:
    backend = explicit_backend or os.environ.get(PDF_BACKEND_ENV, "pypdf")
    if backend not in catalog_index.PDF_BACKENDS:
        raise ResourceError(
            f"invalid {PDF_BACKEND_ENV}: {backend!r}; "
            f"choose from {', '.join(catalog_index.PDF_BACKENDS)}"
        )
    return backend


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResourceError(f"cannot read manifest {path}: {error}") from error
    if manifest.get("version") != 1:
        raise ResourceError("manifest version must be 1")
    for key in ("documents", "repositories"):
        if not isinstance(manifest.get(key, []), list):
            raise ResourceError(f"manifest field {key!r} must be a list")
    validate_manifest(manifest)
    return manifest


def validate_relative_path(value: str, context: str) -> None:
    path = Path(value)
    if path.is_absolute() or not value or ".." in path.parts:
        raise ResourceError(f"{context} must be a non-empty relative path: {value!r}")


def validate_manifest(manifest: dict[str, Any]) -> None:
    ids: set[str] = set()
    paths: set[str] = set()
    for kind in ("documents", "repositories"):
        for resource in manifest.get(kind, []):
            resource_id = resource.get("id")
            if not isinstance(resource_id, str) or not resource_id:
                raise ResourceError(f"every {kind[:-1]} needs a non-empty id")
            if resource_id in ids:
                raise ResourceError(f"duplicate resource id: {resource_id}")
            ids.add(resource_id)
            path = resource.get("path")
            if not isinstance(path, str):
                raise ResourceError(f"{resource_id}: path must be a string")
            validate_relative_path(path, f"{resource_id}: path")
            if path in paths:
                raise ResourceError(f"duplicate resource path: {path}")
            paths.add(path)
            if kind == "documents":
                checksum = resource.get("sha256")
                if not isinstance(checksum, str) or len(checksum) != 64:
                    raise ResourceError(f"{resource_id}: sha256 must contain 64 hex digits")
                try:
                    int(checksum, 16)
                except ValueError as error:
                    raise ResourceError(f"{resource_id}: invalid sha256") from error
            else:
                if not resource.get("clone_url"):
                    raise ResourceError(f"{resource_id}: clone_url is required")
                remote_names: set[str] = set()
                for remote in resource.get("remotes", []):
                    name = remote.get("name")
                    if not name or name in remote_names:
                        raise ResourceError(f"{resource_id}: invalid or duplicate remote name")
                    remote_names.add(name)
                    fetch = remote.get("fetch")
                    if fetch is not None and (
                        not isinstance(fetch, list)
                        or not all(isinstance(refspec, str) and refspec for refspec in fetch)
                    ):
                        raise ResourceError(f"{resource_id}: remote fetch must be a list of refspecs")
                worktree_paths: set[str] = set()
                for worktree in resource.get("worktrees", []):
                    worktree_id = worktree.get("id")
                    if not worktree_id or not worktree.get("ref"):
                        raise ResourceError(f"{resource_id}: every worktree needs id and ref")
                    if worktree_id in ids:
                        raise ResourceError(f"duplicate resource id: {worktree_id}")
                    ids.add(worktree_id)
                    worktree_path = worktree.get("path")
                    if not isinstance(worktree_path, str):
                        raise ResourceError(f"{resource_id}: worktree path must be a string")
                    validate_relative_path(worktree_path, f"{resource_id}: worktree path")
                    if worktree_path in paths or worktree_path in worktree_paths:
                        raise ResourceError(f"duplicate resource path: {worktree_path}")
                    worktree_paths.add(worktree_path)
                paths.update(worktree_paths)


def run_git(arguments: Iterable[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as error:
        raise ResourceError("git is required to populate repository resources") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else f"exit status {error.returncode}"
        raise ResourceError(f"{' '.join(command)}: {detail}") from error
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def selected(resource: dict[str, Any], requested: set[str]) -> bool:
    return not requested or resource["id"] in requested


def selected_repository(resource: dict[str, Any], requested: set[str]) -> bool:
    return selected(resource, requested) or any(
        worktree["id"] in requested for worktree in resource.get("worktrees", [])
    )


def populate_document(resource: dict[str, Any], root: Path) -> str:
    destination = root / resource["path"]
    expected = resource["sha256"].lower()
    if destination.is_file() and sha256(destination) == expected:
        return f"ok       {resource['id']}"
    url = resource.get("url")
    if not url:
        hint = resource.get("source_page", "the source described in the manifest")
        return f"manual   {resource['id']} (place a matching file at {destination}; source: {hint})"
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Paper-Linux-resource-tool/1"})
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urlopen(request) as response:
                while block := response.read(1024 * 1024):
                    temporary.write(block)
        actual = sha256(temporary_path)
        if actual != expected:
            raise ResourceError(
                f"{resource['id']}: checksum mismatch (expected {expected}, got {actual})"
            )
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return f"fetched  {resource['id']}"


def remote_url(repository_path: Path, name: str) -> str | None:
    try:
        return run_git(
            ["--git-dir", str(repository_path), "remote", "get-url", name], capture=True
        )
    except ResourceError:
        return None


def populate_repository(resource: dict[str, Any], root: Path, requested: set[str]) -> list[str]:
    repository_path = root / resource["path"]
    output: list[str] = []
    if not repository_path.exists():
        repository_path.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--bare"]
        if resource.get("filter"):
            clone_args.append(f"--filter={resource['filter']}")
        clone_args.extend([resource["clone_url"], str(repository_path)])
        run_git(clone_args)
        output.append(f"cloned   {resource['id']}")
    elif not (repository_path / "HEAD").is_file():
        raise ResourceError(f"{repository_path} exists but is not a bare Git repository")

    declared_remotes = [{"name": "origin", "url": resource["clone_url"]}, *resource.get("remotes", [])]
    for remote in declared_remotes:
        current = remote_url(repository_path, remote["name"])
        if current is None:
            run_git(["--git-dir", str(repository_path), "remote", "add", remote["name"], remote["url"]])
        elif current != remote["url"]:
            run_git(["--git-dir", str(repository_path), "remote", "set-url", remote["name"], remote["url"]])
        fetch = remote.get("fetch")
        if fetch == []:
            continue
        fetch_arguments = ["--git-dir", str(repository_path), "fetch", "--prune"]
        if resource.get("filter"):
            fetch_arguments.append(f"--filter={resource['filter']}")
            run_git(
                [
                    "--git-dir", str(repository_path), "config",
                    f"remote.{remote['name']}.promisor", "true",
                ]
            )
            run_git(
                [
                    "--git-dir", str(repository_path), "config",
                    f"remote.{remote['name']}.partialclonefilter", resource["filter"],
                ]
            )
        fetch_arguments.append(remote["name"])
        if fetch is not None:
            fetch_arguments.extend(fetch)
        run_git(fetch_arguments)

    for worktree in resource.get("worktrees", []):
        if requested and resource["id"] not in requested and worktree["id"] not in requested:
            continue
        destination = root / worktree["path"]
        if destination.exists():
            try:
                common = run_git(["-C", str(destination), "rev-parse", "--git-common-dir"], capture=True)
            except ResourceError as error:
                raise ResourceError(f"{destination} exists but is not a Git worktree") from error
            if Path(common).resolve() != repository_path.resolve():
                raise ResourceError(f"{destination} belongs to a different Git repository")
            output.append(f"ok       {worktree['id']}")
            continue
        if worktree.get("manual"):
            try:
                run_git(
                    [
                        "--git-dir", str(repository_path), "rev-parse", "--verify",
                        f"{worktree['ref']}^{{commit}}",
                    ],
                    capture=True,
                )
            except ResourceError:
                hint = worktree.get("source_page", "the source described in the manifest")
                output.append(
                    f"manual   {worktree['id']} (create ref {worktree['ref']}; source: {hint})"
                )
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        arguments = ["--git-dir", str(repository_path), "worktree", "add"]
        branch = worktree.get("branch")
        if branch:
            arguments.extend(["-b", branch])
        else:
            arguments.append("--detach")
        arguments.extend([str(destination), worktree["ref"]])
        run_git(arguments)
        output.append(f"created  {worktree['id']}")
    if not output:
        output.append(f"ok       {resource['id']}")
    return output


def check_document(resource: dict[str, Any], root: Path) -> tuple[bool, str]:
    path = root / resource["path"]
    if not path.is_file():
        return False, f"missing  {resource['id']}"
    if sha256(path) != resource["sha256"].lower():
        return False, f"changed  {resource['id']}"
    return True, f"ok       {resource['id']}"


def check_repository(
    resource: dict[str, Any], root: Path, requested: set[str]
) -> list[tuple[bool, str]]:
    repository_path = root / resource["path"]
    if not (repository_path / "HEAD").is_file():
        return [(False, f"missing  {resource['id']}")]
    results: list[tuple[bool, str]] = [(True, f"ok       {resource['id']}")]
    for remote in [{"name": "origin", "url": resource["clone_url"]}, *resource.get("remotes", [])]:
        matches = remote_url(repository_path, remote["name"]) == remote["url"]
        results.append((matches, f"{'ok' if matches else 'changed':8} {resource['id']} remote {remote['name']}"))
    for worktree in resource.get("worktrees", []):
        if requested and resource["id"] not in requested and worktree["id"] not in requested:
            continue
        path = root / worktree["path"]
        try:
            common = run_git(["-C", str(path), "rev-parse", "--git-common-dir"], capture=True)
            matches = Path(common).resolve() == repository_path.resolve()
        except ResourceError:
            matches = False
        results.append((matches, f"{'ok' if matches else 'missing':8} {worktree['id']}"))
    return results


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


def print_extraction(result: dict[str, Any]) -> None:
    for index, page in enumerate(result["pages"]):
        if index:
            print("\n\f")
        print(f"{result['resource_id']}, PDF page {page['page_number']}")
        print(result["description"])
        print(f"Extractor: {result['extractor']} {result['extractor_version']}")
        print(result["path"])
        print()
        print(page["content"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=Path("external-resources.json"))
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list catalogued resources")
    subparsers.add_parser("env", help="print the effective resource environment")
    subparsers.add_parser("path", help="print the configured resource directory")

    index_parser = subparsers.add_parser("index", help="index document text for search")
    index_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    index_parser.add_argument("--pdf-backend", choices=catalog_index.PDF_BACKENDS)
    index_parser.add_argument("--json", action="store_true", help="print JSON output")
    index_parser.add_argument("resources", nargs="*", metavar="ID", help="document IDs (default: all)")

    status_parser = subparsers.add_parser(
        "index-status", help="show whether document indexes are current"
    )
    status_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    status_parser.add_argument("--pdf-backend", choices=catalog_index.PDF_BACKENDS)
    status_parser.add_argument("--json", action="store_true", help="print JSON output")
    status_parser.add_argument("resources", nargs="*", metavar="ID", help="document IDs (default: all)")

    extract_parser = subparsers.add_parser(
        "extract", help="extract PDF text without modifying the index"
    )
    extract_parser.add_argument("--root", type=Path, help="override the configured resource directory")
    extract_parser.add_argument("--pdf-backend", choices=catalog_index.PDF_BACKENDS)
    extract_parser.add_argument("--page", type=positive_integer, help="extract one physical PDF page")
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

    for command, help_text in (("populate", "fetch and prepare resources"), ("check", "check an existing resource directory")):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--root", type=Path, help="override the configured resource directory")
        subparser.add_argument("resources", nargs="*", metavar="ID", help="resource IDs (default: all)")
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        manifest = load_manifest(args.manifest)
        load_environment(args.manifest)
        if args.command == "list":
            print_catalog(manifest)
            return 0
        if args.command in ("env", "path"):
            root = resolve_root(args.manifest)
            if args.command == "env":
                print(f"{RESOURCE_ROOT_ENV}={root}")
                print(f"{RESOURCE_DATABASE_ENV}={resolve_database(root)}")
                print(f"{PDF_BACKEND_ENV}={resolve_pdf_backend()}")
            else:
                print(root)
            return 0

        documents = manifest.get("documents", [])
        documents_by_id = {document["id"]: document for document in documents}
        root = resolve_root(args.manifest, args.root)
        database = resolve_database(root)

        if args.command in ("index", "index-status"):
            requested = set(args.resources)
            backend = resolve_pdf_backend(args.pdf_backend)
            if args.command == "index":
                report = catalog_index.index_documents(
                    documents, root, database, backend, requested
                )
                if args.json:
                    print(catalog_index.json_output(report.to_dict()))
                else:
                    print_index_report(report)
                return 1 if report.failed else 0
            statuses = catalog_index.index_status(
                documents, root, database, backend, requested
            )
            if args.json:
                print(catalog_index.json_output([status.to_dict() for status in statuses]))
            else:
                for status in statuses:
                    print(f"{status.status:10} {status.document_id} ({status.detail})")
            return 0 if all(status.status == "ok" for status in statuses) else 1

        if args.command == "extract":
            document = documents_by_id.get(args.resource)
            if document is None:
                raise ResourceError(f"unknown document ID: {args.resource}")
            extraction = catalog_index.extract_document(
                document,
                root,
                resolve_pdf_backend(args.pdf_backend),
                args.page,
            )
            if args.json:
                print(catalog_index.json_output(extraction))
            else:
                print_extraction(extraction)
            return 0

        if args.command == "search":
            if args.document is not None and args.document not in documents_by_id:
                raise ResourceError(f"unknown document ID: {args.document}")
            results = catalog_index.search_database(
                database,
                root,
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
            if args.resource not in documents_by_id:
                raise ResourceError(f"unknown document ID: {args.resource}")
            page = catalog_index.read_page(database, root, args.resource, args.page_number)
            if args.json:
                print(catalog_index.json_output(page.to_dict()))
            else:
                print_page_result(page)
            return 0

        requested = set(args.resources)
        known = {item["id"] for key in ("documents", "repositories") for item in manifest.get(key, [])}
        known.update(
            worktree["id"]
            for repository in manifest.get("repositories", [])
            for worktree in repository.get("worktrees", [])
        )
        unknown = requested - known
        if unknown:
            raise ResourceError(f"unknown resource ID(s): {', '.join(sorted(unknown))}")
        if args.command == "populate":
            root.mkdir(parents=True, exist_ok=True)
            for document in manifest.get("documents", []):
                if selected(document, requested):
                    print(populate_document(document, root))
            for repository in manifest.get("repositories", []):
                if selected_repository(repository, requested):
                    print("\n".join(populate_repository(repository, root, requested)))
            return 0
        success = True
        for document in manifest.get("documents", []):
            if selected(document, requested):
                ok, message = check_document(document, root)
                success &= ok
                print(message)
        for repository in manifest.get("repositories", []):
            if selected_repository(repository, requested):
                for ok, message in check_repository(repository, root, requested):
                    success &= ok
                    print(message)
        return 0 if success else 1
    except (ResourceError, catalog_index.CatalogIndexError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
