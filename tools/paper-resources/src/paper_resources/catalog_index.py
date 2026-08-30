"""Structured extraction and SQLite full-text indexing for external documents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
import hashlib
from importlib.metadata import version as package_version
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Callable, Iterable, Iterator

from pypdf import PdfReader


SCHEMA_VERSION = 1
EXTRACTION_PIPELINE_VERSION = 1
EXTRACTORS = (
    "pypdf",
    "pypdf-layout",
    "pdftotext",
    "pdftotext-layout",
)


class CatalogIndexError(RuntimeError):
    pass


@dataclass(frozen=True, eq=False, slots=True)
class Page:
    """Opaque handle declaring a page in an extractor event stream."""

    label: str | None = None


@dataclass(frozen=True, eq=False, slots=True)
class Section:
    """Opaque handle declaring a section in an extractor event stream."""

    name: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """A text chunk associated with previously yielded page/section handles."""

    content: str
    pages: tuple[Page, ...] = ()
    sections: tuple[Section, ...] = ()


ExtractionEvent = Page | Section | Chunk


@dataclass(frozen=True, slots=True)
class Extractor:
    name: str
    version: str
    producer: Callable[[Path], Iterator[ExtractionEvent]]

    def extract(self, path: Path) -> Iterator[ExtractionEvent]:
        return self.producer(path)


@dataclass(frozen=True, slots=True)
class ExtractionCounts:
    chunks: int
    pages: int
    sections: int


@dataclass(frozen=True)
class IndexOutcome:
    action: str
    document_id: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class IndexReport:
    outcomes: list[IndexOutcome]
    unchanged: int

    @property
    def failed(self) -> bool:
        return any(outcome.action == "failed" for outcome in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "unchanged": self.unchanged,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class IndexStatus:
    document_id: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    chunk_id: int
    resource_id: str
    description: str
    path: str
    pages: list[int]
    sections: list[str]
    score: float
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PageResult:
    resource_id: str
    description: str
    path: str
    page_number: int
    sections: list[str]
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SectionResult:
    resource_id: str
    description: str
    path: str
    section_index: int
    name: str
    pages: list[int]
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SCHEMA = """
PRAGMA user_version = 1;

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE document_tags (
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (document_id, tag)
) WITHOUT ROWID;

CREATE INDEX document_tags_by_tag
    ON document_tags(tag, document_id);

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    UNIQUE (document_id, chunk_index),
    UNIQUE (document_id, id)
);

CREATE TABLE document_pages (
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    page_label TEXT,
    PRIMARY KEY (document_id, page_number)
) WITHOUT ROWID;

CREATE TABLE chunk_pages (
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    PRIMARY KEY (document_id, page_number, chunk_id),
    FOREIGN KEY (document_id, page_number)
        REFERENCES document_pages(document_id, page_number)
        ON DELETE CASCADE,
    FOREIGN KEY (document_id, chunk_id)
        REFERENCES chunks(document_id, id)
        ON DELETE CASCADE
);

CREATE INDEX chunk_pages_by_chunk
    ON chunk_pages(document_id, chunk_id);

CREATE TABLE document_sections (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    section_index INTEGER NOT NULL CHECK(section_index >= 0),
    name TEXT NOT NULL,
    UNIQUE (document_id, section_index),
    UNIQUE (document_id, id)
);

CREATE TABLE section_chunks (
    document_id TEXT NOT NULL,
    section_id INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    PRIMARY KEY (document_id, section_id, chunk_id),
    FOREIGN KEY (document_id, section_id)
        REFERENCES document_sections(document_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (document_id, chunk_id)
        REFERENCES chunks(document_id, id)
        ON DELETE CASCADE
);

CREATE INDEX section_chunks_by_chunk
    ON section_chunks(document_id, chunk_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content,
    content = 'chunks',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER chunks_fts_update AFTER UPDATE OF content ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def pdftotext_version() -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-v"],
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise CatalogIndexError(
            "pdftotext is required for the selected extractor (install poppler-utils)"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout).strip()
        raise CatalogIndexError(f"cannot determine pdftotext version: {detail}") from error
    output = (result.stderr or result.stdout).strip()
    match = re.search(r"pdftotext version\s+([^\s]+)", output)
    if not match:
        raise CatalogIndexError(f"cannot parse pdftotext version: {output}")
    return match.group(1)


@cache
def make_extractor(name: str) -> Extractor:
    if name not in EXTRACTORS:
        raise CatalogIndexError(
            f"unknown extractor {name!r}; choose from {', '.join(EXTRACTORS)}"
        )
    if name.startswith("pypdf"):
        library_version = package_version("pypdf")
        producer = lambda path: extract_with_pypdf(
            path, layout=name.endswith("-layout")
        )
    else:
        library_version = pdftotext_version()
        producer = lambda path: extract_with_pdftotext(
            path, layout=name.endswith("-layout")
        )
    return Extractor(
        name=name,
        version=f"{library_version};pipeline={EXTRACTION_PIPELINE_VERSION}",
        producer=producer,
    )


def extract_with_pypdf(path: Path, *, layout: bool) -> Iterator[ExtractionEvent]:
    try:
        reader = PdfReader(path)
        for pdf_page in reader.pages:
            page = Page()
            yield page
            yield Chunk(
                (
                    pdf_page.extract_text(extraction_mode="layout")
                    if layout
                    else pdf_page.extract_text()
                )
                or "",
                pages=(page,),
            )
    except Exception as error:
        raise CatalogIndexError(f"cannot extract {path} with pypdf: {error}") from error


def extract_with_pdftotext(path: Path, *, layout: bool) -> Iterator[ExtractionEvent]:
    command = ["pdftotext"]
    if layout:
        command.append("-layout")
    command.extend(["-enc", "UTF-8", "-eol", "unix", "-q", str(path), "-"])
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise CatalogIndexError(
            "pdftotext is required for the selected extractor (install poppler-utils)"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"exit status {error.returncode}"
        raise CatalogIndexError(f"cannot extract {path} with pdftotext: {detail}") from error
    pages = result.stdout.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    for content in pages:
        page = Page()
        yield page
        yield Chunk(content, pages=(page,))


def open_database(path: Path, *, create: bool) -> sqlite3.Connection:
    if not create and not path.is_file():
        raise CatalogIndexError(f"resource index does not exist: {path}; run: just resource index")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if not create:
                raise CatalogIndexError(f"{path} is not a Paper Resources index")
            connection.executescript(SCHEMA)
        elif version != SCHEMA_VERSION:
            raise CatalogIndexError(
                f"unsupported resource index schema {version}; expected {SCHEMA_VERSION}"
            )
        return connection
    except (sqlite3.Error, CatalogIndexError) as error:
        if connection is not None:
            connection.close()
        if isinstance(error, CatalogIndexError):
            raise
        raise CatalogIndexError(f"cannot open resource index {path}: {error}") from error


def document_map(documents: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {document["id"]: document for document in documents}


def extractor_for_document(
    document: dict[str, Any], default_extractor: str, override_extractor: str | None
) -> Extractor:
    return make_extractor(
        override_extractor or document.get("extractor") or default_extractor
    )


def validate_document_file(document: dict[str, Any], root: Path) -> Path:
    path = root / document["path"]
    if not path.is_file():
        raise CatalogIndexError(f"{document['id']}: document is missing: {path}")
    actual = file_sha256(path)
    expected = document["sha256"].lower()
    if actual != expected:
        raise CatalogIndexError(
            f"{document['id']}: checksum mismatch (expected {expected}, got {actual})"
        )
    return path


def tags_for_document(connection: sqlite3.Connection, document_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT tag FROM document_tags WHERE document_id = ? ORDER BY tag",
            (document_id,),
        )
    ]


class ExtractionState:
    """Assign ordered identities and validate references in an event stream."""

    def __init__(self) -> None:
        self.page_numbers: dict[Page, int] = {}
        self.section_indexes: dict[Section, int] = {}
        self.chunk_count = 0

    def add_page(self, page: Page) -> int:
        if page in self.page_numbers:
            raise CatalogIndexError("extractor yielded the same page handle twice")
        if page.label is not None and not isinstance(page.label, str):
            raise CatalogIndexError("extractor yielded a page with a non-text label")
        page_number = len(self.page_numbers) + 1
        self.page_numbers[page] = page_number
        return page_number

    def add_section(self, section: Section) -> int:
        if section in self.section_indexes:
            raise CatalogIndexError("extractor yielded the same section handle twice")
        if not isinstance(section.name, str):
            raise CatalogIndexError("extractor yielded a section with a non-text name")
        section_index = len(self.section_indexes)
        self.section_indexes[section] = section_index
        return section_index

    def add_chunk(
        self, chunk: Chunk
    ) -> tuple[int, str, tuple[int, ...], tuple[int, ...]]:
        if not isinstance(chunk.content, str):
            raise CatalogIndexError("extractor yielded a chunk with non-text content")
        if not all(isinstance(page, Page) for page in chunk.pages):
            raise CatalogIndexError("extractor chunk contains an invalid page handle")
        if not all(isinstance(section, Section) for section in chunk.sections):
            raise CatalogIndexError("extractor chunk contains an invalid section handle")
        try:
            page_numbers = tuple(self.page_numbers[page] for page in chunk.pages)
        except KeyError as error:
            raise CatalogIndexError(
                "extractor chunk references a page before yielding it"
            ) from error
        try:
            section_indexes = tuple(
                self.section_indexes[section] for section in chunk.sections
            )
        except KeyError as error:
            raise CatalogIndexError(
                "extractor chunk references a section before yielding it"
            ) from error
        if len(set(page_numbers)) != len(page_numbers):
            raise CatalogIndexError("extractor chunk references the same page twice")
        if len(set(section_indexes)) != len(section_indexes):
            raise CatalogIndexError("extractor chunk references the same section twice")
        chunk_index = self.chunk_count
        self.chunk_count += 1
        return (
            chunk_index,
            normalize_text(chunk.content),
            page_numbers,
            section_indexes,
        )

    def counts(self) -> ExtractionCounts:
        if self.chunk_count == 0:
            raise CatalogIndexError("extractor yielded no chunks")
        return ExtractionCounts(
            chunks=self.chunk_count,
            pages=len(self.page_numbers),
            sections=len(self.section_indexes),
        )


def insert_document_metadata(
    connection: sqlite3.Connection,
    document: dict[str, Any],
    extractor: Extractor,
) -> None:
    document_id = document["id"]
    connection.execute(
        """
        INSERT INTO documents(id, description, path, sha256, extractor, extractor_version)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            document.get("description", ""),
            document["path"],
            document["sha256"].lower(),
            extractor.name,
            extractor.version,
        ),
    )
    connection.executemany(
        "INSERT INTO document_tags(document_id, tag) VALUES (?, ?)",
        [(document_id, tag) for tag in sorted(set(document.get("tags", [])))],
    )


def consume_extraction(
    connection: sqlite3.Connection,
    document_id: str,
    events: Iterable[ExtractionEvent],
) -> ExtractionCounts:
    state = ExtractionState()
    section_ids: dict[int, int] = {}
    for event in events:
        match event:
            case Page(label=label):
                page_number = state.add_page(event)
                connection.execute(
                    """
                    INSERT INTO document_pages(document_id, page_number, page_label)
                    VALUES (?, ?, ?)
                    """,
                    (document_id, page_number, label),
                )
            case Section(name=name):
                section_index = state.add_section(event)
                cursor = connection.execute(
                    """
                    INSERT INTO document_sections(document_id, section_index, name)
                    VALUES (?, ?, ?)
                    """,
                    (document_id, section_index, name),
                )
                if cursor.lastrowid is None:
                    raise CatalogIndexError("SQLite did not assign a section row ID")
                section_ids[section_index] = cursor.lastrowid
            case Chunk():
                chunk_index, content, page_numbers, section_indexes = state.add_chunk(
                    event
                )
                cursor = connection.execute(
                    """
                    INSERT INTO chunks(document_id, chunk_index, content, content_sha256)
                    VALUES (?, ?, ?, ?)
                    """,
                    (document_id, chunk_index, content, text_sha256(content)),
                )
                if cursor.lastrowid is None:
                    raise CatalogIndexError("SQLite did not assign a chunk row ID")
                chunk_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO chunk_pages(document_id, page_number, chunk_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (document_id, page_number, chunk_id)
                        for page_number in page_numbers
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO section_chunks(document_id, section_id, chunk_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (document_id, section_ids[section_index], chunk_id)
                        for section_index in section_indexes
                    ],
                )
            case _:
                raise CatalogIndexError(
                    f"extractor yielded unsupported event {type(event).__name__}"
                )
    return state.counts()


def index_documents(
    documents: list[dict[str, Any]],
    root: Path,
    connection: sqlite3.Connection,
    default_extractor: str,
    override_extractor: str | None,
    requested: set[str],
) -> IndexReport:
    available = document_map(documents)
    unknown = requested - available.keys()
    if unknown:
        raise CatalogIndexError(f"unknown document ID(s): {', '.join(sorted(unknown))}")
    outcomes: list[IndexOutcome] = []
    unchanged = 0
    selected = [
        document for document in documents if not requested or document["id"] in requested
    ]
    for document in selected:
        document_id = document["id"]
        try:
            path = validate_document_file(document, root)
            extractor = extractor_for_document(
                document, default_extractor, override_extractor
            )
            existing = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            fingerprint = (
                document["sha256"].lower(),
                extractor.name,
                extractor.version,
            )
            if existing is not None and fingerprint == (
                existing["sha256"],
                existing["extractor"],
                existing["extractor_version"],
            ):
                expected_tags = sorted(set(document.get("tags", [])))
                metadata_changed = (
                    existing["description"] != document.get("description", "")
                    or existing["path"] != document["path"]
                    or tags_for_document(connection, document_id) != expected_tags
                )
                if metadata_changed:
                    with connection:
                        connection.execute(
                            "UPDATE documents SET description = ?, path = ? WHERE id = ?",
                            (document.get("description", ""), document["path"], document_id),
                        )
                        connection.execute(
                            "DELETE FROM document_tags WHERE document_id = ?", (document_id,)
                        )
                        connection.executemany(
                            "INSERT INTO document_tags(document_id, tag) VALUES (?, ?)",
                            [(document_id, tag) for tag in expected_tags],
                        )
                    outcomes.append(IndexOutcome("updated", document_id, "metadata"))
                else:
                    unchanged += 1
                continue

            reasons: list[str] = []
            action = "indexed" if existing is None else "reindexed"
            if existing is None:
                reasons.append("new")
            else:
                if existing["sha256"] != fingerprint[0]:
                    reasons.append("sha256 changed")
                if existing["extractor"] != fingerprint[1]:
                    reasons.append(
                        f"extractor {existing['extractor']} -> {fingerprint[1]}"
                    )
                if existing["extractor_version"] != fingerprint[2]:
                    reasons.append(
                        "extractor version "
                        f"{existing['extractor_version']} -> {fingerprint[2]}"
                    )
            counts: ExtractionCounts
            with connection:
                connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
                insert_document_metadata(connection, document, extractor)
                counts = consume_extraction(
                    connection, document_id, extractor.extract(path)
                )
            detail = "; ".join(
                [
                    *reasons,
                    f"{counts.chunks} chunks",
                    f"{counts.pages} pages",
                    f"{counts.sections} sections",
                ]
            )
            outcomes.append(IndexOutcome(action, document_id, detail))
        except (CatalogIndexError, OSError, sqlite3.Error) as error:
            outcomes.append(IndexOutcome("failed", document_id, str(error)))

    if not requested:
        manifest_ids = set(available)
        indexed_ids = {
            row[0] for row in connection.execute("SELECT id FROM documents")
        }
        for document_id in sorted(indexed_ids - manifest_ids):
            with connection:
                connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            outcomes.append(IndexOutcome("removed", document_id, "not in manifest"))
    return IndexReport(outcomes, unchanged)


def index_status(
    documents: list[dict[str, Any]],
    root: Path,
    connection: sqlite3.Connection | None,
    default_extractor: str,
    override_extractor: str | None,
    requested: set[str],
) -> list[IndexStatus]:
    available = document_map(documents)
    unknown = requested - available.keys()
    if unknown:
        raise CatalogIndexError(f"unknown document ID(s): {', '.join(sorted(unknown))}")
    selected = [
        document for document in documents if not requested or document["id"] in requested
    ]
    if connection is None:
        return [IndexStatus(document["id"], "missing", "not indexed") for document in selected]
    statuses: list[IndexStatus] = []
    for document in selected:
        document_id = document["id"]
        extractor = extractor_for_document(
            document, default_extractor, override_extractor
        )
        path = root / document["path"]
        if not path.is_file():
            statuses.append(IndexStatus(document_id, "missing", "document file is missing"))
            continue
        if file_sha256(path) != document["sha256"].lower():
            statuses.append(IndexStatus(document_id, "changed", "document checksum differs"))
            continue
        existing = connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if existing is None:
            statuses.append(IndexStatus(document_id, "missing", "not indexed"))
            continue
        reasons: list[str] = []
        if existing["sha256"] != document["sha256"].lower():
            reasons.append("sha256")
        if existing["extractor"] != extractor.name:
            reasons.append("extractor")
        if existing["extractor_version"] != extractor.version:
            reasons.append("extractor version")
        expected_tags = sorted(set(document.get("tags", [])))
        if (
            existing["description"] != document.get("description", "")
            or existing["path"] != document["path"]
            or tags_for_document(connection, document_id) != expected_tags
        ):
            reasons.append("metadata")
        if reasons:
            statuses.append(IndexStatus(document_id, "stale", ", ".join(reasons)))
        else:
            statuses.append(IndexStatus(document_id, "ok", "current"))
    if not requested:
        indexed_ids = {
            row[0] for row in connection.execute("SELECT id FROM documents")
        }
        for document_id in sorted(indexed_ids - set(available)):
            statuses.append(IndexStatus(document_id, "stale", "not in manifest"))
    return statuses


def plain_fts_query(value: str) -> str:
    terms = re.findall(r"\w+", value, flags=re.UNICODE)
    if not terms:
        raise CatalogIndexError("search query contains no searchable terms")
    return " AND ".join(f'"{term}"' for term in terms)


def search_database(
    connection: sqlite3.Connection,
    root: Path,
    query: str,
    *,
    raw_fts: bool,
    document_id: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    expression = query if raw_fts else plain_fts_query(query)
    conditions = ["chunks_fts MATCH ?"]
    parameters: list[Any] = [expression]
    if document_id:
        conditions.append("documents.id = ?")
        parameters.append(document_id)
    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM document_tags "
            "WHERE document_tags.document_id = documents.id AND document_tags.tag = ?)"
        )
        parameters.append(tag)
    parameters.append(limit)
    sql = f"""
        SELECT chunks.id AS chunk_id,
               documents.id AS resource_id,
               documents.description,
               documents.path,
               chunks.chunk_index,
               bm25(chunks_fts) AS score,
               snippet(chunks_fts, 0, '[[', ']]', ' … ', 32) AS snippet
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.rowid
        JOIN documents ON documents.id = chunks.document_id
        WHERE {' AND '.join(conditions)}
        ORDER BY score, documents.id, chunks.chunk_index
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise CatalogIndexError(f"invalid FTS5 query: {error}") from error
    results: list[SearchResult] = []
    for row in rows:
        pages = [
            item[0]
            for item in connection.execute(
                """
                SELECT page_number FROM chunk_pages
                WHERE document_id = ? AND chunk_id = ?
                ORDER BY page_number
                """,
                (row["resource_id"], row["chunk_id"]),
            )
        ]
        sections = [
            item[0]
            for item in connection.execute(
                """
                SELECT document_sections.name
                FROM section_chunks
                JOIN document_sections
                  ON document_sections.document_id = section_chunks.document_id
                 AND document_sections.id = section_chunks.section_id
                WHERE section_chunks.document_id = ?
                  AND section_chunks.chunk_id = ?
                ORDER BY document_sections.section_index
                """,
                (row["resource_id"], row["chunk_id"]),
            )
        ]
        results.append(
            SearchResult(
                chunk_id=row["chunk_id"],
                resource_id=row["resource_id"],
                description=row["description"],
                path=str((root / row["path"]).resolve()),
                pages=pages,
                sections=sections,
                score=row["score"],
                snippet=row["snippet"],
            )
        )
    return results


def read_page(
    connection: sqlite3.Connection,
    root: Path,
    document_id: str,
    page_number: int,
) -> PageResult:
    document = connection.execute(
        "SELECT id, description, path FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if document is None:
        raise CatalogIndexError(f"document is not indexed: {document_id}")
    page = connection.execute(
        """
        SELECT 1 FROM document_pages
        WHERE document_id = ? AND page_number = ?
        """,
        (document_id, page_number),
    ).fetchone()
    if page is None:
        raise CatalogIndexError(f"{document_id}: no PDF page {page_number}")
    chunks = connection.execute(
        """
        SELECT chunks.content
        FROM chunk_pages
        JOIN chunks
          ON chunks.document_id = chunk_pages.document_id
         AND chunks.id = chunk_pages.chunk_id
        WHERE chunk_pages.document_id = ? AND chunk_pages.page_number = ?
        ORDER BY chunks.chunk_index
        """,
        (document_id, page_number),
    ).fetchall()
    sections = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT document_sections.name, document_sections.section_index
            FROM chunk_pages
            JOIN section_chunks
              ON section_chunks.document_id = chunk_pages.document_id
             AND section_chunks.chunk_id = chunk_pages.chunk_id
            JOIN document_sections
              ON document_sections.document_id = section_chunks.document_id
             AND document_sections.id = section_chunks.section_id
            WHERE chunk_pages.document_id = ? AND chunk_pages.page_number = ?
            ORDER BY document_sections.section_index
            """,
            (document_id, page_number),
        )
    ]
    return PageResult(
        resource_id=document["id"],
        description=document["description"],
        path=str((root / document["path"]).resolve()),
        page_number=page_number,
        sections=sections,
        content="\n\n".join(row["content"] for row in chunks),
    )


def read_section(
    connection: sqlite3.Connection,
    root: Path,
    document_id: str,
    section_index: int,
) -> SectionResult:
    document = connection.execute(
        "SELECT id, description, path FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if document is None:
        raise CatalogIndexError(f"document is not indexed: {document_id}")
    section = connection.execute(
        """
        SELECT id, name FROM document_sections
        WHERE document_id = ? AND section_index = ?
        """,
        (document_id, section_index),
    ).fetchone()
    if section is None:
        raise CatalogIndexError(
            f"{document_id}: no indexed section {section_index}"
        )
    chunks = connection.execute(
        """
        SELECT chunks.content
        FROM section_chunks
        JOIN chunks
          ON chunks.document_id = section_chunks.document_id
         AND chunks.id = section_chunks.chunk_id
        WHERE section_chunks.document_id = ?
          AND section_chunks.section_id = ?
        ORDER BY chunks.chunk_index
        """,
        (document_id, section["id"]),
    ).fetchall()
    pages = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT chunk_pages.page_number
            FROM section_chunks
            JOIN chunk_pages
              ON chunk_pages.document_id = section_chunks.document_id
             AND chunk_pages.chunk_id = section_chunks.chunk_id
            WHERE section_chunks.document_id = ?
              AND section_chunks.section_id = ?
            ORDER BY chunk_pages.page_number
            """,
            (document_id, section["id"]),
        )
    ]
    return SectionResult(
        resource_id=document["id"],
        description=document["description"],
        path=str((root / document["path"]).resolve()),
        section_index=section_index,
        name=section["name"],
        pages=pages,
        content="\n\n".join(row["content"] for row in chunks),
    )


def extract_document(
    document: dict[str, Any],
    root: Path,
    default_extractor: str,
    override_extractor: str | None,
    page_number: int | None,
) -> dict[str, Any]:
    path = validate_document_file(document, root)
    extractor = extractor_for_document(
        document, default_extractor, override_extractor
    )
    state = ExtractionState()
    pages: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    chunk_pages: list[dict[str, int]] = []
    section_chunks: list[dict[str, int]] = []
    for event in extractor.extract(path):
        match event:
            case Page(label=label):
                number = state.add_page(event)
                pages.append({"page_number": number, "page_label": label})
            case Section(name=name):
                index = state.add_section(event)
                sections.append({"section_index": index, "name": name})
            case Chunk():
                index, content, page_numbers, section_indexes = state.add_chunk(event)
                chunks.append({"chunk_index": index, "content": content})
                chunk_pages.extend(
                    {"chunk_index": index, "page_number": number}
                    for number in page_numbers
                )
                section_chunks.extend(
                    {"section_index": section_index, "chunk_index": index}
                    for section_index in section_indexes
                )
            case _:
                raise CatalogIndexError(
                    f"extractor yielded unsupported event {type(event).__name__}"
                )
    state.counts()
    if page_number is not None:
        if not any(page["page_number"] == page_number for page in pages):
            raise CatalogIndexError(f"{document['id']}: no page {page_number}")
        selected_chunks = {
            relation["chunk_index"]
            for relation in chunk_pages
            if relation["page_number"] == page_number
        }
        selected_sections = {
            relation["section_index"]
            for relation in section_chunks
            if relation["chunk_index"] in selected_chunks
        }
        pages = [page for page in pages if page["page_number"] == page_number]
        chunks = [chunk for chunk in chunks if chunk["chunk_index"] in selected_chunks]
        chunk_pages = [
            relation
            for relation in chunk_pages
            if relation["chunk_index"] in selected_chunks
            and relation["page_number"] == page_number
        ]
        sections = [
            section
            for section in sections
            if section["section_index"] in selected_sections
        ]
        section_chunks = [
            relation
            for relation in section_chunks
            if relation["chunk_index"] in selected_chunks
        ]
    return {
        "resource_id": document["id"],
        "description": document.get("description", ""),
        "path": str(path.resolve()),
        "extractor": extractor.name,
        "extractor_version": extractor.version,
        "chunks": chunks,
        "pages": pages,
        "sections": sections,
        "chunk_pages": chunk_pages,
        "section_chunks": section_chunks,
    }


def json_output(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)
