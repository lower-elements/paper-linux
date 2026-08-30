"""Agent-neutral MCP adapter for the Paper Resources catalog."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
from typing import Annotated, Iterator, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import catalog_index
from .config import ResourceError
from .manager import CatalogInfo, ResourceInfo, ResourceKind, ResourceManager


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
    async def get_catalog_info() -> CatalogInfo:
        """Return resolved catalog paths, extractor defaults, and resource counts."""
        return manager.catalog_info()

    @server.tool(title="List resources", annotations=READ_ONLY)
    async def list_resources(
        kind: ResourceKind | None = None,
        tag: str | None = None,
    ) -> list[ResourceInfo]:
        """List cataloged documents, repositories, and worktrees with availability."""
        return manager.list_resources(kind, tag)

    @server.tool(title="Get resource", annotations=READ_ONLY)
    async def get_resource(resource_id: str) -> ResourceInfo:
        """Return manifest metadata and local availability for one resource ID."""
        with domain_errors():
            return manager.get_resource(resource_id)

    @server.tool(title="Search indexed documents", annotations=READ_ONLY)
    async def search_documents(
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
    async def get_document_page(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
    ) -> catalog_index.PageResult:
        """Return indexed text for one physical PDF page of a document."""
        with domain_errors():
            return manager.get_document_page(document_id, page_number)

    @server.tool(title="Get document index status", annotations=READ_ONLY)
    async def get_index_status(
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
    async def index_documents(
        resource_ids: list[str] | None = None,
        extractor: Literal[
            "pypdf", "pypdf-layout", "pdftotext", "pdftotext-layout"
        ]
        | None = None,
    ) -> catalog_index.IndexReport:
        """Index selected documents, or update every stale document when IDs are omitted."""
        with domain_errors():
            return manager.index_documents(resource_ids, extractor)

    @server.resource("paper-resource://catalog")
    async def catalog_resource() -> str:
        """The resource catalog and local availability as JSON."""
        return json.dumps(
            [asdict(resource) for resource in manager.list_resources()],
            indent=2,
            ensure_ascii=False,
        )

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
    create_server(ResourceManager.load(args.manifest, args.root)).run()


if __name__ == "__main__":
    main()
