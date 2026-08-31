"""SQLite connection and schema lifecycle for the resource index."""

from __future__ import annotations

from pathlib import Path
import sqlite3


SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA user_version = 3;

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
