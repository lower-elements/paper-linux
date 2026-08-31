"""Agent-neutral MCP adapter for the Paper Resources catalog."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import catalog_index
from .config import ResourceError, ResourceSettings
from .manager import (
    CatalogInfo, PatchInfo, ResourceInfo, ResourceKind, ResourceManager,
    RevisionInfo, WorktreeInfo,
)


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
INDEX_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


@contextmanager
def domain_errors() -> Iterator[None]:
    """Turn application errors into concise, recoverable MCP tool errors."""
    try:
        yield
    except (ResourceError, catalog_index.CatalogIndexError) as error:
        raise ToolError(str(error)) from error


@contextmanager
def resource_errors() -> Iterator[None]:
    """Turn missing catalog/index entries into MCP resource lookup errors."""
    try:
        yield
    except (ResourceError, catalog_index.CatalogIndexError) as error:
        raise ResourceNotFoundError(str(error)) from error


def json_resource(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def resource_of_kind(
    manager: ResourceManager, resource_id: str, kind: ResourceKind
) -> ResourceInfo:
    resource = manager.get_resource(resource_id)
    if resource.kind != kind:
        raise ResourceError(f"{resource_id} is not a {kind}")
    return resource


def create_server(manager: ResourceManager) -> MCPServer:
    """Create an MCP server backed by an already configured resource manager."""
    server = MCPServer(
        "Paper Resources",
        instructions=(
            "Search and inspect the local external-resource catalog. Document search "
            "results identify manifest resource IDs and physical PDF pages; cite both "
            "when using indexed reference material."
        ),
    )

    @server.tool(title="Get catalog information", annotations=READ_ONLY)
    def get_catalog_info() -> CatalogInfo:
        """Return resolved catalog paths, extractor defaults, and resource counts."""
        return manager.catalog_info()

    @server.tool(title="List resources", annotations=READ_ONLY)
    def list_resources(
        kind: ResourceKind | None = None,
        tag: str | None = None,
    ) -> list[ResourceInfo]:
        """List cataloged documents and repositories with availability."""
        return manager.list_resources(kind, tag)

    @server.tool(title="Get resource", annotations=READ_ONLY)
    def get_resource(resource_id: str) -> ResourceInfo:
        """Return manifest metadata and local availability for one resource ID."""
        with domain_errors():
            return manager.get_resource(resource_id)

    @server.tool(title="List Git repositories", annotations=READ_ONLY)
    def list_repositories(tag: str | None = None) -> list[ResourceInfo]:
        """List cataloged Git object stores."""
        return manager.list_repositories(tag)

    @server.tool(title="Get Git repository", annotations=READ_ONLY)
    def get_repository(repository_id: str) -> ResourceInfo:
        """Return metadata and local availability for one Git repository."""
        with domain_errors():
            return manager.get_repository(repository_id)

    @server.tool(title="List source revisions", annotations=READ_ONLY)
    def list_revisions(
        repository_id: str | None = None,
        author: str | None = None,
        tag: str | None = None,
    ) -> list[RevisionInfo]:
        """List immutable source revisions, optionally filtered by repository."""
        with domain_errors():
            return manager.list_revisions(repository_id, author, tag)

    @server.tool(title="Get source revision", annotations=READ_ONLY)
    def get_revision(repository_id: str, revision_id: str) -> RevisionInfo:
        """Return pinned identity, provenance, and worktrees for one revision."""
        with domain_errors():
            return manager.get_revision(repository_id, revision_id)

    @server.tool(title="List patch artifacts", annotations=READ_ONLY)
    def list_patches(tag: str | None = None) -> list[PatchInfo]:
        """List fetched patches used to construct source revisions."""
        return manager.list_patches(tag)

    @server.tool(title="Get patch artifact", annotations=READ_ONLY)
    def get_patch(patch_id: str) -> PatchInfo:
        """Return metadata and local availability for one patch."""
        with domain_errors():
            return manager.get_patch(patch_id)

    @server.tool(title="List revision worktrees", annotations=READ_ONLY)
    def list_worktrees(
        repository_id: str | None = None,
        revision_id: str | None = None,
    ) -> list[WorktreeInfo]:
        """List worktrees scoped by repository and revision."""
        with domain_errors():
            return manager.list_worktrees(repository_id, revision_id)

    @server.tool(title="Get revision worktree", annotations=READ_ONLY)
    def get_worktree(
        repository_id: str, revision_id: str, worktree_id: str
    ) -> WorktreeInfo:
        """Return status for one revision-scoped worktree."""
        with domain_errors():
            return manager.get_worktree(repository_id, revision_id, worktree_id)

    @server.tool(title="Search indexed documents", annotations=READ_ONLY)
    def search_documents(
        query: Annotated[str, Field(min_length=1)],
        document_id: str | None = None,
        tag: str | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        raw_fts: bool = False,
    ) -> list[catalog_index.SearchResult]:
        """Search FTS5 text and return bounded excerpts with physical PDF pages."""
        with domain_errors():
            return manager.search_documents(
                query,
                document_id=document_id,
                tag=tag,
                limit=limit,
                raw_fts=raw_fts,
            )

    @server.tool(title="Get indexed document page", annotations=READ_ONLY)
    def get_document_page(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
    ) -> catalog_index.PageResult:
        """Return indexed text for one physical PDF page of a document."""
        with domain_errors():
            return manager.get_document_page(document_id, page_number)

    @server.tool(title="Get document index status", annotations=READ_ONLY)
    def get_index_status(
        resource_ids: list[str] | None = None,
        extractor: Literal[
            "pypdf", "pypdf-layout", "pdftotext", "pdftotext-layout"
        ]
        | None = None,
    ) -> list[catalog_index.IndexStatus]:
        """Report whether selected document indexes are current, stale, or missing."""
        with domain_errors():
            return manager.index_status(resource_ids, extractor)

    @server.tool(title="Index documents", annotations=INDEX_WRITE)
    def index_documents(
        resource_ids: list[str] | None = None,
        extractor: Literal[
            "pypdf", "pypdf-layout", "pdftotext", "pdftotext-layout"
        ]
        | None = None,
    ) -> catalog_index.IndexReport:
        """Index selected documents, or update every stale document when IDs are omitted."""
        with domain_errors():
            return manager.index_documents(resource_ids, extractor)

    @server.resource("paper-resource://catalog", mime_type="application/json")
    def catalog_resource() -> str:
        """The resource catalog and local availability as JSON."""
        return json_resource(
            [asdict(resource) for resource in manager.list_resources()]
        )

    @server.resource("paper-resource://documents", mime_type="application/json")
    def documents_resource() -> str:
        """All cataloged reference documents and their local availability."""
        return json_resource(
            [
                asdict(resource)
                for resource in manager.list_resources(kind="document")
            ]
        )

    @server.resource("paper-resource://repositories", mime_type="application/json")
    def repositories_resource() -> str:
        """All cataloged Git repositories and remotes."""
        return json_resource(
            [
                asdict(resource)
                for resource in manager.list_resources(kind="repository")
            ]
        )

    @server.resource("paper-resource://worktrees", mime_type="application/json")
    def worktrees_resource() -> str:
        """All cataloged Git worktrees and their local availability."""
        return json_resource([asdict(resource) for resource in manager.list_worktrees()])

    @server.resource("paper-resource://patches", mime_type="application/json")
    def patches_resource() -> str:
        """All cataloged patch artifacts and their local availability."""
        return json_resource([asdict(patch) for patch in manager.list_patches()])

    @server.resource("paper-resource://revisions", mime_type="application/json")
    def revisions_resource() -> str:
        """All immutable source revisions and provenance relationships."""
        return json_resource([asdict(revision) for revision in manager.list_revisions()])

    @server.resource(
        "paper-resource://documents/{document_id}", mime_type="application/json"
    )
    def document_resource(document_id: str) -> str:
        """Manifest metadata and local availability for one document."""
        with resource_errors():
            return json_resource(
                asdict(resource_of_kind(manager, document_id, "document"))
            )

    @server.resource(
        "paper-resource://documents/{document_id}/pages/{page_number}",
        mime_type="application/json",
    )
    def document_page_resource(document_id: str, page_number: int) -> str:
        """Indexed text and context for one physical PDF page."""
        with resource_errors():
            return json_resource(
                manager.get_document_page(document_id, page_number).to_dict()
            )

    @server.resource(
        "paper-resource://documents/{document_id}/sections/{section_index}",
        mime_type="application/json",
    )
    def document_section_resource(document_id: str, section_index: int) -> str:
        """Indexed text and page context for one extracted document section."""
        with resource_errors():
            return json_resource(
                manager.get_document_section(document_id, section_index).to_dict()
            )

    @server.resource(
        "paper-resource://repositories/{repository_id}",
        mime_type="application/json",
    )
    def repository_resource(repository_id: str) -> str:
        """Manifest metadata and remotes for one Git repository."""
        with resource_errors():
            return json_resource(
                asdict(resource_of_kind(manager, repository_id, "repository"))
            )

    @server.resource(
        "paper-resource://patches/{patch_id}", mime_type="application/json"
    )
    def patch_resource(patch_id: str) -> str:
        """Manifest metadata and local availability for one patch artifact."""
        with resource_errors():
            return json_resource(asdict(manager.get_patch(patch_id)))

    @server.resource(
        "paper-resource://revisions/{repository_id}", mime_type="application/json"
    )
    def repository_revisions_resource(repository_id: str) -> str:
        """All revisions in one repository."""
        with resource_errors():
            return json_resource([
                asdict(revision) for revision in manager.list_revisions(repository_id)
            ])

    @server.resource(
        "paper-resource://revisions/{repository_id}/{revision_id}",
        mime_type="application/json",
    )
    def revision_resource(repository_id: str, revision_id: str) -> str:
        """Pinned identity and provenance for one source revision."""
        with resource_errors():
            return json_resource(asdict(manager.get_revision(repository_id, revision_id)))

    @server.resource(
        "paper-resource://worktrees/{repository_id}/{revision_id}",
        mime_type="application/json",
    )
    def revision_worktrees_resource(repository_id: str, revision_id: str) -> str:
        """All declared worktrees for one source revision."""
        with resource_errors():
            return json_resource([
                asdict(worktree)
                for worktree in manager.list_worktrees(repository_id, revision_id)
            ])

    @server.resource(
        "paper-resource://worktrees/{repository_id}/{revision_id}/{worktree_id}",
        mime_type="application/json",
    )
    def revision_worktree_resource(
        repository_id: str, revision_id: str, worktree_id: str
    ) -> str:
        """Status for one revision-scoped worktree."""
        with resource_errors():
            return json_resource(asdict(
                manager.get_worktree(repository_id, revision_id, worktree_id)
            ))

    @server.resource(
        "paper-resource://worktrees/{repository_id}", mime_type="application/json"
    )
    def repository_worktrees_resource(repository_id: str) -> str:
        """All declared worktrees in one repository."""
        with resource_errors():
            return json_resource([
                asdict(worktree)
                for worktree in manager.list_worktrees(repository_id)
            ])

    return server


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--manifest", type=Path, default=Path("external-resources.json")
    )
    result.add_argument("--root", type=Path, help="override the resource directory")
    return result


def main(arguments: list[str] | None = None) -> None:
    args = parser().parse_args(arguments)
    settings = ResourceSettings.load(args.manifest, args.root)
    manager = ResourceManager.load(settings)
    try:
        create_server(manager).run()
    finally:
        manager.close()


if __name__ == "__main__":
    main()
