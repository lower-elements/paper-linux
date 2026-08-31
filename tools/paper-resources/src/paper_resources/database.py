"""SQLite connection and schema lifecycle for the resource index."""

from __future__ import annotations

from pathlib import Path
import sqlite3


MINIMUM_SQLITE_VERSION = (3, 45, 0)
SCHEMA_VERSION = 7

SCHEMA = """
PRAGMA user_version = 7;

CREATE TABLE repositories (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
) STRICT, WITHOUT ROWID;

CREATE TABLE repository_revisions (
    repository_id TEXT NOT NULL
        REFERENCES repositories(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    commit_oid BLOB NOT NULL CHECK(length(commit_oid) IN (20, 32)),
    tree_oid BLOB NOT NULL CHECK(length(tree_oid) IN (20, 32)),
    author TEXT,
    description TEXT,
    index_enabled INTEGER NOT NULL CHECK(index_enabled IN (0, 1)),
    PRIMARY KEY (repository_id, id)
) STRICT, WITHOUT ROWID;

CREATE TABLE repository_blobs (
    repository_id TEXT NOT NULL
        REFERENCES repositories(id) ON DELETE CASCADE,
    oid BLOB NOT NULL CHECK(length(oid) IN (20, 32)),
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY (repository_id, oid)
) STRICT, WITHOUT ROWID;

-- A profile describes every Ctags input and output setting which can affect
-- the interpretation of a blob analysis.
CREATE TABLE ctags_profiles (
    id INTEGER PRIMARY KEY,
    program_name TEXT NOT NULL,
    program_version TEXT NOT NULL,
    output_version TEXT NOT NULL,
    json_output_version TEXT NOT NULL,
    configuration_sha256 BLOB NOT NULL
        CHECK(length(configuration_sha256) = 32),
    UNIQUE (
        program_name,
        program_version,
        output_version,
        json_output_version,
        configuration_sha256
    )
) STRICT;

-- A profile may contain several parsers because guest parsers and subparsers
-- can emit tags in languages other than the input language.
CREATE TABLE ctags_parsers (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL
        REFERENCES ctags_profiles(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language <> ''),
    -- Subparsers can emit tags without first emitting catalog pseudo-tags.
    parser_version TEXT,
    UNIQUE (profile_id, language),
    UNIQUE (id, profile_id)
) STRICT;

-- JSON output uses the long kind name. The optional letter is retained as
-- Ctags profile metadata, but does not identify kinds in public interfaces.
CREATE TABLE ctags_kinds (
    id INTEGER PRIMARY KEY,
    parser_id INTEGER NOT NULL
        REFERENCES ctags_parsers(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(name <> ''),
    letter TEXT CHECK(letter IS NULL OR length(letter) = 1),
    description TEXT,
    UNIQUE (parser_id, name),
    UNIQUE (id, parser_id)
) STRICT;

CREATE TABLE ctags_roles (
    id INTEGER PRIMARY KEY,
    kind_id INTEGER NOT NULL
        REFERENCES ctags_kinds(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(name <> ''),
    description TEXT,
    UNIQUE (kind_id, name),
    UNIQUE (id, kind_id)
) STRICT;

-- There is exactly one current analysis of a blob in a repository. Reanalysis
-- atomically replaces its tags. A row with no tags is a successful result.
CREATE TABLE ctags_analyses (
    id INTEGER PRIMARY KEY,
    repository_id TEXT NOT NULL,
    blob_oid BLOB NOT NULL CHECK(length(blob_oid) IN (20, 32)),
    profile_id INTEGER NOT NULL
        REFERENCES ctags_profiles(id) ON DELETE RESTRICT,
    input_name TEXT NOT NULL CHECK(input_name <> ''),
    input_parser_id INTEGER,
    UNIQUE (repository_id, blob_oid),
    UNIQUE (id, profile_id),
    FOREIGN KEY (repository_id, blob_oid)
        REFERENCES repository_blobs(repository_id, oid) ON DELETE CASCADE,
    FOREIGN KEY (input_parser_id, profile_id)
        REFERENCES ctags_parsers(id, profile_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE ctags_tags (
    id INTEGER PRIMARY KEY,
    analysis_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    parser_id INTEGER NOT NULL,
    kind_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    name TEXT NOT NULL,
    -- Populated by a later pass which pairs Ctags qualified extra-tags.
    qualified_name TEXT,
    line_start INTEGER NOT NULL CHECK(line_start > 0),
    line_end INTEGER CHECK(line_end IS NULL OR line_end >= line_start),
    signature TEXT,
    typeref TEXT,
    access TEXT,
    scope TEXT,
    scope_kind TEXT,
    nth INTEGER CHECK(nth IS NULL OR nth >= 0),
    is_file_restricted INTEGER NOT NULL
        CHECK(is_file_restricted IN (0, 1)),
    is_reference INTEGER NOT NULL CHECK(is_reference IN (0, 1)),
    -- Populated later by best-effort resolution of scope and scope_kind.
    enclosing_tag_id INTEGER
        REFERENCES ctags_tags(id) ON DELETE SET NULL,
    metadata BLOB CHECK(metadata IS NULL OR json_valid(metadata, 8)),
    UNIQUE (analysis_id, ordinal),
    UNIQUE (id, kind_id),
    FOREIGN KEY (analysis_id, profile_id)
        REFERENCES ctags_analyses(id, profile_id) ON DELETE CASCADE,
    FOREIGN KEY (parser_id, profile_id)
        REFERENCES ctags_parsers(id, profile_id) ON DELETE RESTRICT,
    FOREIGN KEY (kind_id, parser_id)
        REFERENCES ctags_kinds(id, parser_id) ON DELETE RESTRICT
) STRICT;

-- Exact and prefix symbol lookup before restricting results to revisions.
CREATE INDEX ctags_tags_by_name
    ON ctags_tags(name, analysis_id, is_reference, kind_id);

-- Precise language-native qualified lookup once names have been paired.
CREATE INDEX ctags_tags_by_qualified_name
    ON ctags_tags(qualified_name, analysis_id, is_reference, kind_id)
    WHERE qualified_name IS NOT NULL;

-- Source-order listing and lookup around a particular line.
CREATE INDEX ctags_tags_by_analysis_line
    ON ctags_tags(analysis_id, line_start, ordinal);

-- Members of a class, structure, namespace, or other reported scope.
CREATE INDEX ctags_tags_by_scope
    ON ctags_tags(scope, scope_kind, access, analysis_id, kind_id)
    WHERE scope IS NOT NULL;

-- Best-effort scope hierarchy traversal once enclosing IDs are resolved.
CREATE INDEX ctags_tags_by_enclosing
    ON ctags_tags(enclosing_tag_id, ordinal)
    WHERE enclosing_tag_id IS NOT NULL;

-- Definitions use is_reference = 0 rather than a redundant "def" role row.
-- References may have multiple parser-defined roles constrained to their kind.
CREATE TABLE ctags_tag_roles (
    tag_id INTEGER NOT NULL,
    kind_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (tag_id, role_id),
    FOREIGN KEY (tag_id, kind_id)
        REFERENCES ctags_tags(id, kind_id) ON DELETE CASCADE,
    FOREIGN KEY (role_id, kind_id)
        REFERENCES ctags_roles(id, kind_id) ON DELETE RESTRICT
) STRICT, WITHOUT ROWID;

-- The primary key lists roles for a tag; this finds tags with a given role.
CREATE INDEX ctags_tag_roles_by_role
    ON ctags_tag_roles(role_id, tag_id);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    sha256 BLOB NOT NULL CHECK(length(sha256) = 32),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE document_tags (
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (document_id, tag)
) STRICT, WITHOUT ROWID;

CREATE INDEX document_tags_by_tag
    ON document_tags(tag, document_id);

CREATE TABLE document_chunks (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    content TEXT NOT NULL,
    content_sha256 BLOB NOT NULL CHECK(length(content_sha256) = 32),
    UNIQUE (document_id, chunk_index),
    UNIQUE (document_id, id)
) STRICT;

CREATE TABLE document_pages (
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    page_label TEXT,
    PRIMARY KEY (document_id, page_number)
) STRICT, WITHOUT ROWID;

CREATE TABLE document_page_chunks (
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    PRIMARY KEY (document_id, page_number, chunk_id),
    FOREIGN KEY (document_id, page_number)
        REFERENCES document_pages(document_id, page_number)
        ON DELETE CASCADE,
    FOREIGN KEY (document_id, chunk_id)
        REFERENCES document_chunks(document_id, id)
        ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE INDEX document_page_chunks_by_chunk
    ON document_page_chunks(document_id, chunk_id);

CREATE TABLE document_sections (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    section_index INTEGER NOT NULL CHECK(section_index >= 0),
    name TEXT NOT NULL,
    level INTEGER CHECK(level IS NULL OR level >= 0),
    parent_section_id INTEGER,
    UNIQUE (document_id, section_index),
    UNIQUE (document_id, id),
    FOREIGN KEY (document_id, parent_section_id)
        REFERENCES document_sections(document_id, id)
        ON DELETE CASCADE
) STRICT;

CREATE TABLE document_section_chunks (
    document_id TEXT NOT NULL,
    section_id INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    PRIMARY KEY (document_id, section_id, chunk_id),
    FOREIGN KEY (document_id, section_id)
        REFERENCES document_sections(document_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (document_id, chunk_id)
        REFERENCES document_chunks(document_id, id)
        ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE INDEX document_section_chunks_by_chunk
    ON document_section_chunks(document_id, chunk_id);

CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
    content,
    content = 'document_chunks',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER document_chunks_fts_insert AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER document_chunks_fts_delete AFTER DELETE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER document_chunks_fts_update AFTER UPDATE OF content ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO document_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class DatabaseError(RuntimeError):
    """The resource index could not be opened or initialized."""


def open_database(path: Path, *, create: bool) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
        required = ".".join(str(item) for item in MINIMUM_SQLITE_VERSION)
        raise DatabaseError(
            f"Paper Resources requires SQLite {required} or newer for JSONB; "
            f"Python is using {sqlite3.sqlite_version}"
        )
    if not create and not path.is_file():
        raise DatabaseError(
            f"resource index does not exist: {path}; run: just resource index"
        )
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
                raise DatabaseError(f"{path} is not a Paper Resources index")
            connection.executescript(SCHEMA)
        elif version != SCHEMA_VERSION:
            raise DatabaseError(
                f"unsupported resource index schema {version}; "
                f"expected {SCHEMA_VERSION}"
            )
        return connection
    except (sqlite3.Error, DatabaseError) as error:
        if connection is not None:
            connection.close()
        if isinstance(error, DatabaseError):
            raise
        raise DatabaseError(f"cannot open resource index {path}: {error}") from error
