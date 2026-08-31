"""Reusable application service for CLI and MCP resource operations."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from threading import RLock
from typing import Any, Literal

from . import artifacts, catalog_index, database, git_resources
from .config import ResourceError, ResourceSettings
from .manifest import load_manifest


ResourceKind = Literal["document", "repository"]


@dataclass(frozen=True)
class RemoteInfo:
    name: str
    url: str


@dataclass(frozen=True)
class WorktreeInfo:
    id: str
    repository_id: str
    revision_id: str
    path: str
    resolved_path: str
    available: bool
    status: str
    head: str | None
    dirty: bool


@dataclass(frozen=True)
class ReferenceBaseInfo:
    revision: str
    reason: str


@dataclass(frozen=True)
class RevisionSourceInfo:
    remote: str
    ref: str


@dataclass(frozen=True)
class RevisionInfo:
    repository_id: str
    id: str
    description: str
    author: str
    index: bool
    tags: list[str]
    commit: str
    tree: str
    available: bool
    derived_from: str | None
    reference_base: ReferenceBaseInfo | None
    source: RevisionSourceInfo | None
    patches: list[str]
    worktrees: list[WorktreeInfo]


@dataclass(frozen=True)
class RevisionPathChange:
    status: str
    path: str


@dataclass(frozen=True)
class RevisionComparison:
    repository_id: str
    from_revision_id: str
    to_revision_id: str
    path: str | None
    total: int
    offset: int
    limit: int
    status_counts: dict[str, int]
    changes: list[RevisionPathChange]


@dataclass(frozen=True)
class RevisionFileDiff:
    repository_id: str
    from_revision_id: str
    to_revision_id: str
    path: str
    diff: str


@dataclass(frozen=True)
class PatchInfo:
    id: str
    description: str
    author: str
    tags: list[str]
    path: str
    resolved_path: str
    sha256: str
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
    remotes: list[RemoteInfo] | None = None


@dataclass(frozen=True)
class CatalogInfo:
    manifest_path: str
    resource_root: str
    database_path: str
    default_extractor: str
    documents: int
    patches: int
    repositories: int
    revisions: int
    worktrees: int


class ResourceManager:
    """Manifest-backed resource and index operations."""

    def __init__(self, settings: ResourceSettings, manifest: dict[str, Any]):
        self.settings = settings
        self.manifest = manifest
        self.documents = manifest.get("documents", [])
        self.patches = manifest.get("patches", [])
        self.repositories = manifest.get("repositories", [])
        self.documents_by_id = {
            document["id"]: document for document in self.documents
        }
        self.patches_by_id = {patch["id"]: patch for patch in self.patches}
        self.repositories_by_id = {
            repository["id"]: repository for repository in self.repositories
        }
        self._connection: sqlite3.Connection | None = None
        self._database_lock = RLock()

    @classmethod
    def load(cls, settings: ResourceSettings) -> "ResourceManager":
        return cls(settings, load_manifest(settings.manifest_path))

    def _database(self, *, create: bool) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = database.open_database(
                self.settings.database, create=create
            )
        return self._connection

    def close(self) -> None:
        """Close the lazily opened resource-index connection, if any."""
        with self._database_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def catalog_info(self) -> CatalogInfo:
        return CatalogInfo(
            manifest_path=str(self.settings.manifest_path),
            resource_root=str(self.settings.root),
            database_path=str(self.settings.database),
            default_extractor=self.settings.default_extractor,
            documents=len(self.documents),
            patches=len(self.patches),
            repositories=len(self.repositories),
            revisions=sum(len(repository.get("revisions", [])) for repository in self.repositories),
            worktrees=sum(
                sum(len(revision.get("worktrees", [])) for revision in repository.get("revisions", []))
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
        )

    def list_repositories(self, tag: str | None = None) -> list[ResourceInfo]:
        return [
            self._repository_info(repository)
            for repository in self.repositories
            if tag is None or tag in repository.get("tags", [])
        ]

    def get_repository(self, repository_id: str) -> ResourceInfo:
        repository = self.repositories_by_id.get(repository_id)
        if repository is None:
            raise ResourceError(f"unknown repository ID: {repository_id}")
        return self._repository_info(repository)

    def _patch_info(self, patch: dict[str, Any]) -> PatchInfo:
        path = self.settings.root / patch["path"]
        return PatchInfo(
            id=patch["id"],
            description=patch["description"],
            author=patch["author"],
            tags=list(patch.get("tags", [])),
            path=patch["path"],
            resolved_path=str(path.resolve()),
            sha256=patch["sha256"],
            available=path.is_file() and artifacts.sha256(path) == patch["sha256"],
        )

    def _revision_manifest(self, repository_id: str, revision_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        repository = self.repositories_by_id.get(repository_id)
        if repository is None:
            raise ResourceError(f"unknown repository ID: {repository_id}")
        for revision in repository.get("revisions", []):
            if revision["id"] == revision_id:
                return repository, revision
        raise ResourceError(f"unknown revision: {repository_id}:{revision_id}")

    def _worktree_info_v2(
        self, repository: dict[str, Any], revision: dict[str, Any], worktree: dict[str, Any]
    ) -> WorktreeInfo:
        repository_path = self.settings.root / repository["path"]
        available, status, head, dirty = git_resources.worktree_status(
            repository_path, revision, worktree, self.settings.root
        )
        return WorktreeInfo(
            id=worktree["id"],
            repository_id=repository["id"],
            revision_id=revision["id"],
            path=worktree["path"],
            resolved_path=str((self.settings.root / worktree["path"]).resolve()),
            available=available,
            status=status,
            head=head,
            dirty=dirty,
        )

    def _revision_info(self, repository: dict[str, Any], revision: dict[str, Any]) -> RevisionInfo:
        repository_path = self.settings.root / repository["path"]
        commit = git_resources.git_object(
            repository_path, f"{git_resources.revision_ref(revision['id'])}^{{commit}}"
        ) if repository_path.exists() else None
        reference = revision.get("reference_base")
        source = revision.get("source")
        return RevisionInfo(
            repository_id=repository["id"],
            id=revision["id"],
            description=revision["description"],
            author=revision["author"],
            index=revision["index"],
            tags=list(revision.get("tags", [])),
            commit=revision["commit"],
            tree=revision["tree"],
            available=commit == revision["commit"],
            derived_from=revision.get("derived_from"),
            reference_base=ReferenceBaseInfo(**reference) if reference else None,
            source=RevisionSourceInfo(**source) if source else None,
            patches=[git_resources.patch_application(item)[0] for item in revision.get("patches", [])],
            worktrees=[
                self._worktree_info_v2(repository, revision, worktree)
                for worktree in revision.get("worktrees", [])
            ],
        )

    def list_patches(self, tag: str | None = None) -> list[PatchInfo]:
        return [
            self._patch_info(patch) for patch in self.patches
            if tag is None or tag in patch.get("tags", [])
        ]

    def get_patch(self, patch_id: str) -> PatchInfo:
        patch = self.patches_by_id.get(patch_id)
        if patch is None:
            raise ResourceError(f"unknown patch ID: {patch_id}")
        return self._patch_info(patch)

    def list_revisions(
        self, repository_id: str | None = None, author: str | None = None,
        tag: str | None = None
    ) -> list[RevisionInfo]:
        repositories = self.repositories if repository_id is None else [
            self.repositories_by_id.get(repository_id)
        ]
        if repositories == [None]:
            raise ResourceError(f"unknown repository ID: {repository_id}")
        return [
            self._revision_info(repository, revision)
            for repository in repositories if repository is not None
            for revision in repository.get("revisions", [])
            if (author is None or revision["author"] == author)
            and (tag is None or tag in revision.get("tags", []))
        ]

    def get_revision(self, repository_id: str, revision_id: str) -> RevisionInfo:
        repository, revision = self._revision_manifest(repository_id, revision_id)
        return self._revision_info(repository, revision)

    def _revision_pair(
        self, repository_id: str, from_revision_id: str, to_revision_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
        repository, from_revision = self._revision_manifest(
            repository_id, from_revision_id
        )
        _repository, to_revision = self._revision_manifest(
            repository_id, to_revision_id
        )
        repository_path = self.settings.root / repository["path"]
        if not (repository_path / "HEAD").is_file():
            raise ResourceError(f"repository is not populated: {repository_id}")
        for revision in (from_revision, to_revision):
            commit = git_resources.git_object(
                repository_path, f"{revision['commit']}^{{commit}}"
            )
            if commit is None:
                raise ResourceError(
                    f"revision is not populated: {repository_id}:{revision['id']}"
                )
            git_resources.verify_revision_objects(
                repository_path, repository_id, revision, commit
            )
        return repository, from_revision, to_revision, repository_path

    def compare_revisions(
        self,
        repository_id: str,
        from_revision_id: str,
        to_revision_id: str,
        *,
        path: str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> RevisionComparison:
        if offset < 0:
            raise ResourceError("comparison offset must be at least 0")
        if limit < 1:
            raise ResourceError("comparison limit must be at least 1")
        _repository, from_revision, to_revision, repository_path = (
            self._revision_pair(repository_id, from_revision_id, to_revision_id)
        )
        normalized_path = (
            git_resources.validate_repository_path(path) if path is not None else None
        )
        raw_changes = git_resources.compare_revision_paths(
            repository_path,
            from_revision["commit"],
            to_revision["commit"],
            normalized_path,
        )
        status_counts: dict[str, int] = {}
        for status, _changed_path in raw_changes:
            status_counts[status] = status_counts.get(status, 0) + 1
        return RevisionComparison(
            repository_id=repository_id,
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
            path=normalized_path,
            total=len(raw_changes),
            offset=offset,
            limit=limit,
            status_counts=status_counts,
            changes=[
                RevisionPathChange(status=status, path=changed_path)
                for status, changed_path in raw_changes[offset:offset + limit]
            ],
        )

    def diff_revision_file(
        self,
        repository_id: str,
        from_revision_id: str,
        to_revision_id: str,
        path: str,
    ) -> RevisionFileDiff:
        _repository, from_revision, to_revision, repository_path = (
            self._revision_pair(repository_id, from_revision_id, to_revision_id)
        )
        normalized_path = git_resources.validate_repository_path(path)
        return RevisionFileDiff(
            repository_id=repository_id,
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
            path=normalized_path,
            diff=git_resources.revision_file_diff(
                repository_path,
                from_revision["commit"],
                to_revision["commit"],
                normalized_path,
            ),
        )

    def list_worktrees(
        self, repository_id: str | None = None, revision_id: str | None = None
    ) -> list[WorktreeInfo]:
        revisions = self.list_revisions(repository_id)
        if revision_id is not None:
            revisions = [revision for revision in revisions if revision.id == revision_id]
            if not revisions:
                raise ResourceError(f"unknown revision: {repository_id}:{revision_id}")
        return [worktree for revision in revisions for worktree in revision.worktrees]

    def get_worktree(
        self, repository_id: str, revision_id: str, worktree_id: str
    ) -> WorktreeInfo:
        revision = self.get_revision(repository_id, revision_id)
        for worktree in revision.worktrees:
            if worktree.id == worktree_id:
                return worktree
        raise ResourceError(
            f"unknown worktree: {repository_id}:{revision_id}:{worktree_id}"
        )

    def list_resources(
        self, kind: ResourceKind | None = None, tag: str | None = None
    ) -> list[ResourceInfo]:
        resources = [self._document_info(document) for document in self.documents]
        for repository in self.repositories:
            resources.append(self._repository_info(repository))
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

    def populate(
        self,
        resource_ids: list[str] | None = None,
        *,
        repository_id: str | None = None,
        revision_key: tuple[str, str] | None = None,
        patch_id: str | None = None,
        worktree_key: tuple[str, str, str] | None = None,
    ) -> list[str]:
        if self.manifest.get("version") == 2:
            selectors = sum(value is not None for value in (
                repository_id, revision_key, patch_id, worktree_key
            ))
            if selectors + bool(resource_ids) > 1:
                raise ResourceError("select only one repository, revision, patch, or worktree")
            requested = set(resource_ids or [])
            globally_known = {
                *self.documents_by_id, *self.patches_by_id, *self.repositories_by_id
            }
            unknown = requested - globally_known
            if unknown:
                raise ResourceError(f"unknown resource ID(s): {', '.join(sorted(unknown))}")
            output: list[str] = []
            if selectors == 0 and not requested:
                for document in self.documents:
                    output.append(artifacts.populate_file(document, self.settings.root))
            elif requested:
                for document in self.documents:
                    if document["id"] in requested:
                        output.append(artifacts.populate_file(document, self.settings.root))
            selected_repository = repository_id
            selected_revision: tuple[str, str] | None = revision_key
            selected_worktree: tuple[str, str, str] | None = worktree_key
            if selected_revision is not None:
                selected_repository = selected_revision[0]
            if selected_worktree is not None:
                selected_repository = selected_worktree[0]
                selected_revision = selected_worktree[:2]
            requested_repositories = requested & self.repositories_by_id.keys()
            if requested_repositories:
                repositories = [
                    self.repositories_by_id[item] for item in sorted(requested_repositories)
                ]
            else:
                repositories = self.repositories if selected_repository is None and not requested else [
                    self.repositories_by_id.get(selected_repository)
                ] if selected_repository is not None else []
            if repositories == [None]:
                raise ResourceError(f"unknown repository ID: {selected_repository}")
            needed_patches: set[str] = set()
            if patch_id is not None:
                if patch_id not in self.patches_by_id:
                    raise ResourceError(f"unknown patch ID: {patch_id}")
                needed_patches.add(patch_id)
                repositories = []
            else:
                for repository in repositories:
                    if repository is None:
                        continue
                    revision_ids = (
                        git_resources.revision_dependencies(
                            repository, {selected_revision[1]}
                        )
                        if selected_revision is not None else None
                    )
                    for revision in repository["revisions"]:
                        if revision_ids is not None and revision["id"] not in revision_ids:
                            continue
                        needed_patches.update(
                            git_resources.patch_application(item)[0]
                            for item in revision.get("patches", [])
                        )
            needed_patches.update(requested & self.patches_by_id.keys())
            if selectors == 0:
                needed_patches.update(self.patches_by_id)
            for needed_patch in self.patches:
                if needed_patch["id"] in needed_patches:
                    output.append(artifacts.populate_file(needed_patch, self.settings.root))
            for repository in repositories:
                if repository is None:
                    continue
                revision_ids = None if selected_revision is None else {selected_revision[1]}
                selected_worktree_key = None
                if selected_worktree is not None:
                    selected_worktree_key = (selected_worktree[1], selected_worktree[2])
                output.extend(
                    git_resources.populate_repository(
                        repository, self.patches_by_id, self.settings.root,
                        revision_ids=revision_ids,
                        worktree_key=selected_worktree_key,
                    )
                )
            return output
        raise AssertionError("manifest validation accepted an unsupported version")

    def check(
        self,
        resource_ids: list[str] | None = None,
        *,
        repository_id: str | None = None,
        revision_key: tuple[str, str] | None = None,
        patch_id: str | None = None,
        worktree_key: tuple[str, str, str] | None = None,
    ) -> tuple[bool, list[str]]:
        if self.manifest.get("version") == 2:
            selectors = sum(value is not None for value in (
                repository_id, revision_key, patch_id, worktree_key
            ))
            if selectors + bool(resource_ids) > 1:
                raise ResourceError("select only one repository, revision, patch, or worktree")
            requested = set(resource_ids or [])
            globally_known = {
                *self.documents_by_id, *self.patches_by_id, *self.repositories_by_id
            }
            unknown = requested - globally_known
            if unknown:
                raise ResourceError(f"unknown resource ID(s): {', '.join(sorted(unknown))}")
            success = True
            output: list[str] = []
            files: list[dict[str, Any]] = []
            if selectors == 0 and not requested:
                files.extend(self.documents)
                files.extend(self.patches)
            elif requested:
                files.extend(
                    resource for resource in [*self.documents, *self.patches]
                    if resource["id"] in requested
                )
            elif patch_id is not None:
                patch = self.patches_by_id.get(patch_id)
                if patch is None:
                    raise ResourceError(f"unknown patch ID: {patch_id}")
                files.append(patch)
            for resource in files:
                ok, message = artifacts.check_file(resource, self.settings.root)
                success &= ok
                output.append(message)
            selected_repository = repository_id
            selected_revision = revision_key
            selected_worktree = worktree_key
            if selected_revision:
                selected_repository = selected_revision[0]
            if selected_worktree:
                selected_repository = selected_worktree[0]
                selected_revision = selected_worktree[:2]
            requested_repositories = requested & self.repositories_by_id.keys()
            repositories = [] if patch_id is not None else (
                [self.repositories_by_id[item] for item in sorted(requested_repositories)]
                if requested_repositories else self.repositories if selected_repository is None and not requested
                else [self.repositories_by_id.get(selected_repository)]
                if selected_repository is not None else []
            )
            if repositories == [None]:
                raise ResourceError(f"unknown repository ID: {selected_repository}")
            for repository in repositories:
                if repository is None:
                    continue
                revision_ids = None if selected_revision is None else {selected_revision[1]}
                selected_worktree_key = None if selected_worktree is None else (
                    selected_worktree[1], selected_worktree[2]
                )
                for ok, message in git_resources.check_repository(
                    repository, self.settings.root,
                    revision_ids=revision_ids,
                    worktree_key=selected_worktree_key,
                ):
                    success &= ok
                    output.append(message)
            return success, output
        raise AssertionError("manifest validation accepted an unsupported version")

    def index_documents(
        self, resource_ids: list[str] | None = None, extractor: str | None = None
    ) -> catalog_index.IndexReport:
        with self._database_lock:
            return catalog_index.index_documents(
                self.documents,
                self.settings.root,
                self._database(create=True),
                self.settings.default_extractor,
                extractor,
                set(resource_ids or []),
            )

    def index_status(
        self, resource_ids: list[str] | None = None, extractor: str | None = None
    ) -> list[catalog_index.IndexStatus]:
        with self._database_lock:
            connection = (
                self._database(create=False)
                if self._connection is not None or self.settings.database.is_file()
                else None
            )
            return catalog_index.index_status(
                self.documents,
                self.settings.root,
                connection,
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
        with self._database_lock:
            return catalog_index.search_database(
                self._database(create=False),
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
        with self._database_lock:
            return catalog_index.read_page(
                self._database(create=False),
                self.settings.root,
                document_id,
                page_number,
            )

    def get_document_section(
        self, document_id: str, section_index: int
    ) -> catalog_index.SectionResult:
        if document_id not in self.documents_by_id:
            raise ResourceError(f"unknown document ID: {document_id}")
        if section_index < 0:
            raise ResourceError("section index must be at least 0")
        with self._database_lock:
            return catalog_index.read_section(
                self._database(create=False),
                self.settings.root,
                document_id,
                section_index,
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
