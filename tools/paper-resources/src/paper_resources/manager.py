"""Reusable application service for CLI and MCP resource operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import catalog_index
from .config import ResourceError, ResourceSettings
from .manifest import load_manifest


ResourceKind = Literal["document", "repository", "worktree"]


@dataclass(frozen=True)
class RemoteInfo:
    name: str
    url: str


@dataclass(frozen=True)
class WorktreeInfo:
    id: str
    path: str
    resolved_path: str
    ref: str
    available: bool


@dataclass(frozen=True)
class ResourceInfo:
    id: str
    kind: ResourceKind
    description: str
    tags: list[str]
    path: str
    resolved_path: str
    available: bool
    sha256: str | None = None
    extractor: str | None = None
    clone_url: str | None = None
    repository_id: str | None = None
    ref: str | None = None
    remotes: list[RemoteInfo] | None = None
    worktrees: list[WorktreeInfo] | None = None


@dataclass(frozen=True)
class CatalogInfo:
    manifest_path: str
    resource_root: str
    database_path: str
    default_extractor: str
    documents: int
    repositories: int
    worktrees: int


class ResourceManager:
    """Manifest-backed resource and document-index operations."""

    def __init__(self, settings: ResourceSettings, manifest: dict[str, Any]):
        self.settings = settings
        self.manifest = manifest
        self.documents = manifest.get("documents", [])
        self.repositories = manifest.get("repositories", [])
        self.documents_by_id = {
            document["id"]: document for document in self.documents
        }

    @classmethod
    def load(cls, settings: ResourceSettings) -> "ResourceManager":
        return cls(settings, load_manifest(settings.manifest_path))

    def catalog_info(self) -> CatalogInfo:
        return CatalogInfo(
            manifest_path=str(self.settings.manifest_path),
            resource_root=str(self.settings.root),
            database_path=str(self.settings.database),
            default_extractor=self.settings.default_extractor,
            documents=len(self.documents),
            repositories=len(self.repositories),
            worktrees=sum(
                len(repository.get("worktrees", []))
                for repository in self.repositories
            ),
        )

    def _document_info(self, document: dict[str, Any]) -> ResourceInfo:
        path = self.settings.root / document["path"]
        return ResourceInfo(
            id=document["id"],
            kind="document",
            description=document.get("description", ""),
            tags=list(document.get("tags", [])),
            path=document["path"],
            resolved_path=str(path.resolve()),
            available=path.is_file(),
            sha256=document["sha256"],
            extractor=document.get("extractor") or self.settings.default_extractor,
        )

    def _repository_info(self, repository: dict[str, Any]) -> ResourceInfo:
        path = self.settings.root / repository["path"]
        worktrees = [
            WorktreeInfo(
                id=worktree["id"],
                path=worktree["path"],
                resolved_path=str((self.settings.root / worktree["path"]).resolve()),
                ref=worktree["ref"],
                available=(self.settings.root / worktree["path"]).is_dir(),
            )
            for worktree in repository.get("worktrees", [])
        ]
        remotes = [
            RemoteInfo("origin", repository["clone_url"]),
            *[
                RemoteInfo(remote["name"], remote["url"])
                for remote in repository.get("remotes", [])
            ],
        ]
        return ResourceInfo(
            id=repository["id"],
            kind="repository",
            description=repository.get("description", ""),
            tags=list(repository.get("tags", [])),
            path=repository["path"],
            resolved_path=str(path.resolve()),
            available=(path / "HEAD").is_file(),
            clone_url=repository["clone_url"],
            remotes=remotes,
            worktrees=worktrees,
        )

    def _worktree_info(
        self, repository: dict[str, Any], worktree: dict[str, Any]
    ) -> ResourceInfo:
        path = self.settings.root / worktree["path"]
        return ResourceInfo(
            id=worktree["id"],
            kind="worktree",
            description=repository.get("description", ""),
            tags=list(repository.get("tags", [])),
            path=worktree["path"],
            resolved_path=str(path.resolve()),
            available=path.is_dir(),
            repository_id=repository["id"],
            ref=worktree["ref"],
        )

    def list_resources(
        self, kind: ResourceKind | None = None, tag: str | None = None
    ) -> list[ResourceInfo]:
        resources = [self._document_info(document) for document in self.documents]
        for repository in self.repositories:
            resources.append(self._repository_info(repository))
            resources.extend(
                self._worktree_info(repository, worktree)
                for worktree in repository.get("worktrees", [])
            )
        return [
            resource
            for resource in resources
            if (kind is None or resource.kind == kind)
            and (tag is None or tag in resource.tags)
        ]

    def get_resource(self, resource_id: str) -> ResourceInfo:
        for resource in self.list_resources():
            if resource.id == resource_id:
                return resource
        raise ResourceError(f"unknown resource ID: {resource_id}")

    def index_documents(
        self, resource_ids: list[str] | None = None, extractor: str | None = None
    ) -> catalog_index.IndexReport:
        return catalog_index.index_documents(
            self.documents,
            self.settings.root,
            self.settings.database,
            self.settings.default_extractor,
            extractor,
            set(resource_ids or []),
        )

    def index_status(
        self, resource_ids: list[str] | None = None, extractor: str | None = None
    ) -> list[catalog_index.IndexStatus]:
        return catalog_index.index_status(
            self.documents,
            self.settings.root,
            self.settings.database,
            self.settings.default_extractor,
            extractor,
            set(resource_ids or []),
        )

    def search_documents(
        self,
        query: str,
        *,
        document_id: str | None = None,
        tag: str | None = None,
        limit: int = 10,
        raw_fts: bool = False,
    ) -> list[catalog_index.SearchResult]:
        if document_id is not None and document_id not in self.documents_by_id:
            raise ResourceError(f"unknown document ID: {document_id}")
        if not 1 <= limit <= 50:
            raise ResourceError("search limit must be between 1 and 50")
        return catalog_index.search_database(
            self.settings.database,
            self.settings.root,
            query,
            raw_fts=raw_fts,
            document_id=document_id,
            tag=tag,
            limit=limit,
        )

    def get_document_page(
        self, document_id: str, page_number: int
    ) -> catalog_index.PageResult:
        if document_id not in self.documents_by_id:
            raise ResourceError(f"unknown document ID: {document_id}")
        if page_number < 1:
            raise ResourceError("page number must be at least 1")
        return catalog_index.read_page(
            self.settings.database, self.settings.root, document_id, page_number
        )

    def extract_document(
        self,
        document_id: str,
        extractor: str | None = None,
        page_number: int | None = None,
    ) -> dict[str, Any]:
        document = self.documents_by_id.get(document_id)
        if document is None:
            raise ResourceError(f"unknown document ID: {document_id}")
        return catalog_index.extract_document(
            document,
            self.settings.root,
            self.settings.default_extractor,
            extractor,
            page_number,
        )
