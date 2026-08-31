import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import anyio
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

from paper_resources import (
    catalog_index,
    cli,
    ctags,
    ctags_index,
    database,
    git_resources,
    manager as manager_module,
    repository_index,
)
from paper_resources.config import ResourceError, ResourceSettings
from paper_resources.manager import ResourceManager
from paper_resources.mcp_server import create_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write_test_pdf(path: Path, pages: list[str]) -> None:
    """Write a small Type 1-font PDF whose text is extractable by all extractors."""
    objects: list[bytes] = []
    page_ids = [3 + index * 2 for index in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    )
    font_id = 3 + len(pages) * 2
    for index, text in enumerate(pages):
        page_id = page_ids[index]
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(output)


def git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True
    )
    return result.stdout.strip()


class PaperResourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        source = self.base / "source"
        self.source = source
        source.mkdir()
        git("init", "-b", "main", cwd=source)
        (source / "README").write_text("first\n", encoding="utf-8")
        (source / "alias.c").write_text("first\n", encoding="utf-8")
        (source / "driver.c").write_text(
            "struct device { int state; };\n"
            "static int driver_start(void) { return 0; }\n",
            encoding="utf-8",
        )
        (source / "obsolete.txt").write_text("move me\n", encoding="utf-8")
        (source / "duplicate.txt").write_text("move me\n", encoding="utf-8")
        (source / "empty.txt").touch()
        git(
            "add", "README", "alias.c", "driver.c", "obsolete.txt",
            "duplicate.txt", "empty.txt",
            cwd=source,
        )
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "first",
            cwd=source,
        )
        git("tag", "v1", cwd=source)
        git("branch", "alternate", cwd=source)
        commit = git("rev-parse", "v1^{commit}", cwd=source)
        tree = git("rev-parse", "v1^{tree}", cwd=source)
        git("switch", "alternate", cwd=source)
        (source / "README").write_text("second\n", encoding="utf-8")
        git("mv", "obsolete.txt", "renamed.txt", cwd=source)
        (source / "drivers").mkdir()
        (source / "drivers/new.c").write_text("int new_driver;\n", encoding="utf-8")
        git("add", "README", "drivers/new.c", cwd=source)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "alternate",
            cwd=source,
        )
        alternate_commit = git("rev-parse", "HEAD^{commit}", cwd=source)
        alternate_tree = git("rev-parse", "HEAD^{tree}", cwd=source)
        git("switch", "main", cwd=source)

        document = self.base / "source-document.pdf"
        write_test_pdf(
            document,
            ["Reference material overview", "Display power sequence details"],
        )
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
        manifest = {
            "version": 2,
            "documents": [
                {
                    "id": "test-document",
                    "description": "test document",
                    "path": "documents/reference.pdf",
                    "url": document.as_uri(),
                    "sha256": digest,
                    "tags": ["display", "test"],
                }
            ],
            "patches": [],
            "repositories": [
                {
                    "id": "test-repository",
                    "description": "test repository",
                    "path": "git/test.git",
                    "clone_url": str(source),
                    "remotes": [],
                    "revisions": [
                        {
                            "id": "v1",
                            "description": "test release",
                            "author": "test",
                            "index": True,
                            "tags": ["test"],
                            "commit": commit,
                            "tree": tree,
                            "source": {"remote": "origin", "ref": "refs/tags/v1"},
                            "worktrees": [
                                {"id": "default", "path": "worktrees/test-v1"}
                            ],
                        },
                        {
                            "id": "alternate",
                            "description": "test alternate release",
                            "author": "test",
                            "index": False,
                            "tags": ["test"],
                            "commit": alternate_commit,
                            "tree": alternate_tree,
                            "source": {"remote": "origin", "ref": "refs/heads/alternate"},
                            "worktrees": [
                                {"id": "default", "path": "worktrees/test-alternate"}
                            ],
                        },
                    ],
                }
            ],
        }
        self.manifest = self.base / "manifest.json"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.resources = self.base / "resources"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def tool(
        self,
        *arguments: str,
        expected: int = 0,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for variable in (
            "PAPER_RESOURCES_DIR",
            "PAPER_RESOURCES_DB",
            "PAPER_RESOURCES_DEFAULT_EXTRACTOR",
        ):
            env.pop(variable, None)
        if environment is not None:
            env.update(environment)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "paper_resources",
                "--manifest",
                str(self.manifest),
                *arguments,
            ],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_populate_and_check_all_resources(self) -> None:
        (self.base / ".env").write_text(
            f"PAPER_RESOURCES_DIR={self.resources}\n", encoding="utf-8"
        )
        result = self.tool("populate")
        self.assertIn("fetched  test-document", result.stdout)
        self.assertTrue((self.resources / "worktrees/test-v1/README").is_file())
        self.assertTrue((self.resources / "worktrees/test-alternate/README").is_file())
        self.tool("check")

        second = self.tool("populate")
        self.assertIn("ok       test-document", second.stdout)
        self.assertIn("ok       test-repository:v1:default", second.stdout)

    def test_repository_catalog_synchronization(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        repository = manifest["repositories"][0]
        connection = database.open_database(self.base / "catalog.db", create=True)
        self.addCleanup(connection.close)

        summary = repository_index.synchronize_catalog(
            connection, manifest["repositories"]
        )
        self.assertEqual(
            summary, repository_index.RepositoryCatalogSummary(1, 2)
        )
        revision = connection.execute(
            """
            SELECT commit_oid, tree_oid, author, description, index_enabled
            FROM repository_revisions
            WHERE repository_id = ? AND id = ?
            """,
            (repository["id"], "v1"),
        ).fetchone()
        self.assertEqual(
            revision["commit_oid"],
            bytes.fromhex(repository["revisions"][0]["commit"]),
        )
        self.assertEqual(
            revision["tree_oid"],
            bytes.fromhex(repository["revisions"][0]["tree"]),
        )
        self.assertEqual(
            (revision["author"], revision["description"]),
            ("test", "test release"),
        )
        self.assertEqual(revision["index_enabled"], 1)

        repository["revisions"][1]["description"] = "updated alternate"
        repository["revisions"] = repository["revisions"][1:]
        repository_index.synchronize_catalog(connection, manifest["repositories"])
        rows = connection.execute(
            """
            SELECT id, description FROM repository_revisions
            WHERE repository_id = ? ORDER BY id
            """,
            (repository["id"],),
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["description"]) for row in rows],
            [("alternate", "updated alternate")],
        )

    def test_revision_blob_iteration_reading_and_lazy_registration(self) -> None:
        self.tool(
            "populate", "--root", str(self.resources),
            "--repository", "test-repository",
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        repository = manifest["repositories"][0]
        v1, alternate = repository["revisions"]

        v1_blobs = list(
            repository_index.iter_revision_blobs(repository, v1, self.resources)
        )
        alternate_blobs = list(
            repository_index.iter_revision_blobs(
                repository, alternate, self.resources
            )
        )
        self.assertEqual(
            [blob.path for blob in v1_blobs],
            sorted(blob.path for blob in v1_blobs),
        )
        by_v1_path = {blob.path: blob for blob in v1_blobs}
        by_alternate_path = {blob.path: blob for blob in alternate_blobs}
        self.assertEqual(by_v1_path["empty.txt"].size, 0)
        self.assertEqual(
            by_v1_path["obsolete.txt"].oid,
            by_v1_path["duplicate.txt"].oid,
        )
        self.assertEqual(
            by_v1_path["obsolete.txt"].oid,
            by_alternate_path["renamed.txt"].oid,
        )
        self.assertEqual(
            repository_index.read_blob(
                repository, self.resources, by_v1_path["empty.txt"].oid
            ),
            b"",
        )
        self.assertEqual(
            repository_index.read_blob(
                repository, self.resources, by_v1_path["obsolete.txt"].oid
            ),
            b"move me\n",
        )

        connection = database.open_database(self.base / "blobs.db", create=True)
        self.addCleanup(connection.close)
        repository_index.synchronize_catalog(connection, manifest["repositories"])
        shared = by_v1_path["obsolete.txt"]
        with connection:
            self.assertTrue(
                repository_index.register_blob(
                    connection, repository["id"], shared.oid, shared.size
                )
            )
            self.assertFalse(
                repository_index.register_blob(
                    connection,
                    repository["id"],
                    by_alternate_path["renamed.txt"].oid,
                    by_alternate_path["renamed.txt"].size,
                )
            )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM repository_blobs").fetchone()[0],
            1,
        )
        with self.assertRaisesRegex(ResourceError, "blob size mismatch"):
            repository_index.register_blob(
                connection, repository["id"], shared.oid, shared.size + 1
            )

        bad_revision = dict(v1, tree="0" * 40)
        with self.assertRaisesRegex(ResourceError, "tree mismatch"):
            list(
                repository_index.iter_revision_blobs(
                    repository, bad_revision, self.resources
                )
            )

        self.assertEqual(
            git_resources.oid_to_hex(git_resources.oid_from_hex("ab" * 32)),
            "ab" * 32,
        )

    def test_ctags_session_lazily_catalogs_blob_languages(self) -> None:
        if shutil.which("ctags") is None:
            self.skipTest("Universal Ctags is not installed")
        connection = database.open_database(self.base / "ctags.db", create=True)
        self.addCleanup(connection.close)

        sources = [
            (
                "driver.c",
                b"#include <linux/types.h>\n"
                b"struct device { int state; };\n"
                b"static int driver_start(void) { return 0; }\n",
            ),
            ("device.h", b"struct header_type { int field; };\n"),
            ("start.S", b".globl _start\n_start:\n\tnop\n"),
            ("layout.lds", b"SECTIONS { .text : { *(.text) } }\n"),
            ("Makefile", b"all:\n\t@echo ready\n"),
            ("unrecognized.paper-resource", b"\x00\xff\x00"),
        ]

        with ctags.CtagsSession(connection) as session:
            analyses = [
                list(session.analyze(name, content)) for name, content in sources
            ]
            c_events = analyses[0]
            profile = next(
                event for event in c_events if isinstance(event, ctags.CtagsProfile)
            )
            c_parser = next(
                event
                for event in c_events
                if isinstance(event, ctags.CtagsParser) and event.language == "C"
            )
            c_tags = [
                event for event in c_events if isinstance(event, ctags.CtagsTag)
            ]
            self.assertTrue(any(tag.name == "driver_start" for tag in c_tags))
            self.assertTrue(
                any(
                    tag.name.endswith("::state") and "qualified" in tag.extras
                    for tag in c_tags
                )
            )
            self.assertTrue(
                any(
                    tag.name == "driver_start" and tag.fields.get("file") is True
                    for tag in c_tags
                )
            )
            header_reference = next(
                tag for tag in c_tags if tag.name == "linux/types.h"
            )
            self.assertIn("reference", header_reference.extras)
            self.assertEqual(header_reference.roles, ("system",))
            self.assertEqual(len(header_reference.role_ids), 1)
            self.assertFalse(
                any("inputFile" in tag.extras for tag in c_tags)
            )
            self.assertTrue(
                all(
                    not {
                        "_type", "name", "path", "language", "kind", "roles", "extras"
                    }
                    & tag.fields.keys()
                    for tag in c_tags
                )
            )

            assembly_events = analyses[2]
            provisional_parser = next(
                event
                for event in assembly_events
                if isinstance(event, ctags.CtagsParser)
                and event.language == "LdScript"
            )
            self.assertIsNone(provisional_parser.version)
            provisional_kind = next(
                event
                for event in assembly_events
                if isinstance(event, ctags.CtagsKind)
                and event.language == "LdScript"
                and event.name == "symbol"
            )
            self.assertIsNone(provisional_kind.letter)

            linker_events = analyses[3]
            enriched_parser = next(
                event
                for event in linker_events
                if isinstance(event, ctags.CtagsParser)
                and event.language == "LdScript"
            )
            self.assertEqual(enriched_parser.id, provisional_parser.id)
            self.assertIsNotNone(enriched_parser.version)
            enriched_kind = next(
                event
                for event in linker_events
                if isinstance(event, ctags.CtagsKind)
                and event.language == "LdScript"
                and event.name == "symbol"
            )
            self.assertEqual(enriched_kind.id, provisional_kind.id)
            self.assertIsNotNone(enriched_kind.letter)

            completed = [
                next(
                    event
                    for event in reversed(events)
                    if isinstance(event, ctags.CtagsCompleted)
                )
                for events in analyses
            ]
            self.assertTrue(all(item.profile_id == profile.id for item in completed))
            self.assertEqual(completed[0].input_parser_id, c_parser.id)
            self.assertEqual(completed[-1].input_parser_id, None)
            self.assertEqual(completed[-1].tags, 0)

            counts = tuple(
                connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM ctags_profiles),
                        (SELECT count(*) FROM ctags_parsers),
                        (SELECT count(*) FROM ctags_kinds),
                        (SELECT count(*) FROM ctags_roles)
                    """
                ).fetchone()
            )
            self.assertEqual(counts[0], 1)
            self.assertGreaterEqual(counts[1], 3)
            self.assertGreater(counts[2], counts[1])
            self.assertGreater(counts[3], 0)

            repeated = list(session.analyze(*sources[0]))
            repeated_parser = next(
                event
                for event in repeated
                if isinstance(event, ctags.CtagsParser) and event.language == "C"
            )
            self.assertEqual(repeated_parser.id, c_parser.id)
            self.assertEqual(
                tuple(
                    connection.execute(
                        """
                        SELECT
                            (SELECT count(*) FROM ctags_profiles),
                            (SELECT count(*) FROM ctags_parsers),
                            (SELECT count(*) FROM ctags_kinds),
                            (SELECT count(*) FROM ctags_roles)
                        """
                    ).fetchone()
                ),
                counts,
            )

        with ctags.CtagsSession(connection) as second_session:
            second = list(second_session.analyze(*sources[0]))
        self.assertEqual(
            next(event.id for event in second if isinstance(event, ctags.CtagsProfile)),
            profile.id,
        )

    def test_ctags_blob_analysis_storage_normalizes_and_replaces_tags(self) -> None:
        if shutil.which("ctags") is None:
            self.skipTest("Universal Ctags is not installed")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        repository = manifest["repositories"][0]
        connection = database.open_database(self.base / "ctags-storage.db", create=True)
        self.addCleanup(connection.close)
        repository_index.synchronize_catalog(connection, manifest["repositories"])

        content = (
            b"#include <linux/types.h>\n"
            b"struct device { int state; };\n"
            b"static int driver_start(struct device *device) {\n"
            b"    return device->state;\n"
            b"}\n"
        )
        oid = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content
        ).digest()

        with ctags.CtagsSession(connection) as session:
            stored = ctags_index.store_blob_analysis(
                connection,
                session,
                repository["id"],
                oid,
                "driver.c",
                content,
            )
            self.assertGreater(stored.tags, 0)
            self.assertGreater(stored.roles, 0)
            self.assertGreater(stored.qualified_names, 0)
            self.assertGreater(stored.enclosing_links, 0)

            analysis = connection.execute(
                """
                SELECT id, profile_id, input_name, input_parser_id
                FROM ctags_analyses
                WHERE repository_id = ? AND blob_oid = ?
                """,
                (repository["id"], oid),
            ).fetchone()
            self.assertEqual(analysis["id"], stored.id)
            self.assertEqual(analysis["profile_id"], stored.profile_id)
            self.assertEqual(analysis["input_name"], "driver.c")
            self.assertEqual(analysis["input_parser_id"], stored.input_parser_id)

            tags = connection.execute(
                """
                SELECT
                    ctags_tags.*,
                    ctags_parsers.language,
                    ctags_kinds.name AS kind
                FROM ctags_tags
                JOIN ctags_parsers ON ctags_parsers.id = ctags_tags.parser_id
                JOIN ctags_kinds ON ctags_kinds.id = ctags_tags.kind_id
                WHERE analysis_id = ?
                ORDER BY ordinal
                """,
                (stored.id,),
            ).fetchall()
            self.assertEqual(len(tags), stored.tags)
            self.assertEqual(
                [row["ordinal"] for row in tags], list(range(stored.tags))
            )
            self.assertFalse(any("::" in row["name"] for row in tags))
            self.assertEqual(
                sum(row["qualified_name"] is not None for row in tags),
                stored.qualified_names,
            )

            function = next(row for row in tags if row["name"] == "driver_start")
            self.assertEqual(function["language"], "C")
            self.assertEqual(function["kind"], "function")
            self.assertEqual(function["line_start"], 3)
            self.assertEqual(function["line_end"], 5)
            self.assertIsNotNone(function["signature"])
            self.assertEqual(function["is_file_restricted"], 1)
            self.assertEqual(function["is_reference"], 0)
            self.assertEqual(function["scope"], None)

            header = next(row for row in tags if row["name"] == "linux/types.h")
            self.assertEqual(header["is_reference"], 1)
            roles = connection.execute(
                """
                SELECT ctags_roles.name
                FROM ctags_tag_roles
                JOIN ctags_roles ON ctags_roles.id = ctags_tag_roles.role_id
                WHERE ctags_tag_roles.tag_id = ?
                """,
                (header["id"],),
            ).fetchall()
            self.assertEqual([row["name"] for row in roles], ["system"])

            state = next(row for row in tags if row["name"] == "state")
            self.assertEqual(state["qualified_name"], "device::state")
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM ctags_tags WHERE id = ?",
                    (state["enclosing_tag_id"],),
                ).fetchone()[0],
                "device",
            )
            self.assertEqual(state["scope"], "device")
            self.assertEqual(state["scope_kind"], "struct")
            self.assertEqual(state["typeref"], "typename:int")
            self.assertEqual(
                connection.execute(
                    "SELECT typeof(metadata) FROM ctags_tags WHERE id = ?",
                    (state["id"],),
                ).fetchone()[0],
                "blob",
            )
            metadata = json.loads(
                connection.execute(
                    "SELECT json(metadata) FROM ctags_tags WHERE id = ?",
                    (state["id"],),
                ).fetchone()[0]
            )
            self.assertIn("pattern", metadata)
            self.assertFalse(
                {
                    "line", "end", "signature", "typeref", "access", "scope",
                    "scopeKind", "nth", "file", "roles", "reference",
                }
                & metadata.keys()
            )

            replacement = ctags_index.store_blob_analysis(
                connection,
                session,
                repository["id"],
                oid,
                "renamed.c",
                content,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ctags_analyses"
                ).fetchone()[0],
                1,
            )
            replacement_tag_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM ctags_tags ORDER BY ordinal"
                )
            ]
            self.assertEqual(len(replacement_tag_ids), replacement.tags)
            self.assertEqual(
                connection.execute(
                    "SELECT input_name FROM ctags_analyses"
                ).fetchone()[0],
                "renamed.c",
            )

            with connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_ctags_tag_insert
                    BEFORE INSERT ON ctags_tags
                    BEGIN
                        SELECT RAISE(FAIL, 'test insertion failure');
                    END
                    """
                )
            with self.assertRaisesRegex(ResourceError, "test insertion failure"):
                ctags_index.store_blob_analysis(
                    connection,
                    session,
                    repository["id"],
                    oid,
                    "driver.c",
                    content,
                )
            self.assertEqual(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM ctags_tags ORDER BY ordinal"
                    )
                ],
                replacement_tag_ids,
            )
            with connection:
                connection.execute("DROP TRIGGER reject_ctags_tag_insert")

            cpp_content = (
                b"namespace outer {\n"
                b"class widget {\n"
                b"private:\n"
                b"    int value;\n"
                b"public:\n"
                b"    void set(int next) { value = next; }\n"
                b"};\n"
                b"}\n"
            )
            cpp_oid = hashlib.sha1(
                f"blob {len(cpp_content)}\0".encode("ascii") + cpp_content
            ).digest()
            cpp = ctags_index.store_blob_analysis(
                connection,
                session,
                repository["id"],
                cpp_oid,
                "widget.cpp",
                cpp_content,
            )
            cpp_tags = connection.execute(
                """
                SELECT child.name, child.qualified_name, parent.name AS parent
                FROM ctags_tags AS child
                LEFT JOIN ctags_tags AS parent
                    ON parent.id = child.enclosing_tag_id
                WHERE child.analysis_id = ? AND child.is_reference = 0
                """,
                (cpp.id,),
            ).fetchall()
            widget = next(row for row in cpp_tags if row["name"] == "widget")
            value = next(row for row in cpp_tags if row["name"] == "value")
            method = next(row for row in cpp_tags if row["name"] == "set")
            self.assertEqual(widget["qualified_name"], "outer::widget")
            self.assertEqual(widget["parent"], "outer")
            self.assertEqual(value["qualified_name"], "outer::widget::value")
            self.assertEqual(value["parent"], "widget")
            self.assertEqual(method["qualified_name"], "outer::widget::set")
            self.assertEqual(method["parent"], "widget")
            self.assertEqual(
                ctags_index.resolve_enclosing_tags(connection, cpp.id),
                cpp.enclosing_links,
            )

            empty_content = b"\x00\xff\x00"
            empty_oid = hashlib.sha1(
                f"blob {len(empty_content)}\0".encode("ascii") + empty_content
            ).digest()
            empty = ctags_index.store_blob_analysis(
                connection,
                session,
                repository["id"],
                empty_oid,
                "unrecognized.paper-resource",
                empty_content,
            )
            self.assertEqual(empty.tags, 0)
            self.assertIsNone(empty.input_parser_id)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ctags_tags WHERE analysis_id = ?",
                    (empty.id,),
                ).fetchone()[0],
                0,
            )

        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ctags_tag_normalization_promotes_fields_without_duplication(self) -> None:
        event = ctags.CtagsTag(
            parser_id=1,
            kind_id=2,
            name="member",
            language="C++",
            kind="member",
            roles=("foreign",),
            role_ids=(3,),
            extras=("reference", "fileScope", "subparser"),
            fields={
                "line": 10,
                "end": 12,
                "signature": "(int value)",
                "typeref": "typename:int",
                "access": "private",
                "scope": "widget",
                "scopeKind": "class",
                "nth": 0,
                "file": True,
                "implementation": "virtual",
                "parserSpecific": ["one", "two"],
            },
        )
        tag = ctags_index.normalize_tag(event)
        self.assertEqual(tag.line_start, 10)
        self.assertEqual(tag.line_end, 12)
        self.assertEqual(tag.signature, "(int value)")
        self.assertEqual(tag.typeref, "typename:int")
        self.assertEqual(tag.access, "private")
        self.assertEqual(tag.scope, "widget")
        self.assertEqual(tag.scope_kind, "class")
        self.assertEqual(tag.nth, 0)
        self.assertTrue(tag.is_file_restricted)
        self.assertTrue(tag.is_reference)
        self.assertEqual(
            json.loads(tag.metadata or "null"),
            {
                "extras": ["subparser"],
                "implementation": "virtual",
                "parserSpecific": ["one", "two"],
            },
        )
        with self.assertRaisesRegex(ResourceError, "invalid end"):
            ctags_index.normalize_tag(
                ctags.CtagsTag(
                    parser_id=1,
                    kind_id=2,
                    name="broken",
                    language="C",
                    kind="function",
                    roles=("def",),
                    role_ids=(),
                    extras=(),
                    fields={"line": 10, "end": 9},
                )
            )

    def test_index_command_scans_selected_revision_blobs(self) -> None:
        if shutil.which("ctags") is None:
            self.skipTest("Universal Ctags is not installed")
        self.tool("populate", "--root", str(self.resources))

        indexed = self.tool("index", "--root", str(self.resources))
        self.assertIn("ok         test-repository:v1", indexed.stdout)
        self.assertIn("6 paths, 4 indexed, 0 reused", indexed.stdout)
        self.assertIn("alias.c: reused README analysis", indexed.stdout)

        with sqlite3.connect(self.resources / "resources.db") as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ctags_revision_paths"
                ).fetchone()[0],
                6,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ctags_analyses"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT count(*) FROM ctags_revision_paths
                    WHERE revision_id = 'alternate'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ctags_tags WHERE name = 'driver_start'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        current = json.loads(
            self.tool(
                "index", "--root", str(self.resources), "--json"
            ).stdout
        )
        revision = current["code"]["revisions"][0]
        self.assertEqual(revision["indexed"], 0)
        self.assertEqual(revision["reused"], 4)
        self.assertFalse(current["code"]["failed"])

    def test_populate_one_worktree(self) -> None:
        self.tool(
            "populate", "--root", str(self.resources),
            "--worktree", "test-repository", "v1", "default",
        )
        self.assertTrue((self.resources / "worktrees/test-v1").is_dir())
        self.assertFalse((self.resources / "worktrees/test-alternate").exists())
        self.tool(
            "check", "--root", str(self.resources),
            "--worktree", "test-repository", "v1", "default",
        )

    def test_compare_revisions_and_diff_one_file(self) -> None:
        self.tool(
            "populate", "--root", str(self.resources),
            "--revision", "test-repository", "alternate",
        )
        comparison = json.loads(
            self.tool(
                "compare", "--root", str(self.resources), "--limit", "2", "--json",
                "test-repository", "v1", "alternate",
            ).stdout
        )
        self.assertEqual(comparison["total"], 4)
        self.assertEqual(comparison["status_counts"], {"A": 2, "D": 1, "M": 1})
        self.assertEqual(len(comparison["changes"]), 2)

        readme = json.loads(
            self.tool(
                "compare", "--root", str(self.resources), "--path", "README", "--json",
                "test-repository", "v1", "alternate",
            ).stdout
        )
        self.assertEqual(
            readme["changes"], [{"status": "M", "path": "README"}]
        )

        final_page = json.loads(
            self.tool(
                "compare", "--root", str(self.resources), "--offset", "3", "--limit", "2", "--json",
                "test-repository", "v1", "alternate",
            ).stdout
        )
        self.assertEqual(len(final_page["changes"]), 1)

        file_diff = self.tool(
            "diff", "--root", str(self.resources),
            "test-repository", "v1", "alternate", "README",
        ).stdout
        self.assertIn("-first", file_diff)
        self.assertIn("+second", file_diff)

        directory = self.tool(
            "diff", "--root", str(self.resources),
            "test-repository", "v1", "alternate", "drivers",
            expected=2,
        )
        self.assertIn("path is not a file", directory.stderr)
        missing = self.tool(
            "diff", "--root", str(self.resources),
            "test-repository", "v1", "alternate", "missing.c",
            expected=2,
        )
        self.assertIn("file does not exist", missing.stderr)
        traversal = self.tool(
            "compare", "--root", str(self.resources), "--path", "../outside",
            "test-repository", "v1", "alternate",
            expected=2,
        )
        self.assertIn("invalid repository-relative path", traversal.stderr)

    def test_patch_constructs_pinned_revision_deterministically(self) -> None:
        patch_path = self.base / "change.patch"
        patch_path.write_text(
            "diff --git a/README b/README\n"
            "--- a/README\n"
            "+++ b/README\n"
            "@@ -1 +1 @@\n"
            "-first\n"
            "+patched\n",
            encoding="utf-8",
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        patch_resource = {
            "id": "test-change",
            "description": "test source change",
            "author": "test",
            "path": "patches/change.patch",
            "url": patch_path.as_uri(),
            "sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        }
        manifest["patches"].append(patch_resource)
        repository = manifest["repositories"][0]
        draft = {
            "id": "patched",
            "description": "derived test release",
            "author": "test",
            "index": False,
            "commit": "0" * 40,
            "tree": "0" * 40,
            "derived_from": "v1",
            "patches": ["test-change"],
            "worktrees": [
                {"id": "default", "path": "worktrees/test-patched"}
            ],
        }
        repository["revisions"].append(draft)
        commit, tree = git_resources.construct_revision(
            repository,
            self.source / ".git",
            draft,
            {"test-change": {"path": str(patch_path)}},
            Path("/"),
            verify=False,
        )
        draft["commit"] = commit
        draft["tree"] = tree
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.tool(
            "populate", "--root", str(self.resources),
            "--revision", "test-repository", "patched",
        )
        self.assertIn("ok       test-repository:patched", result.stdout)
        self.assertFalse((self.resources / "worktrees/test-patched").exists())
        self.tool(
            "check", "--root", str(self.resources),
            "--revision", "test-repository", "patched",
        )
        self.tool(
            "populate", "--root", str(self.resources),
            "--worktree", "test-repository", "patched", "default",
        )
        self.assertEqual(
            (self.resources / "worktrees/test-patched/README").read_text(
                encoding="utf-8"
            ),
            "patched\n",
        )
        repository_path = self.resources / "git/test.git"
        self.assertEqual(
            git("--git-dir", str(repository_path), "rev-parse", "refs/paper-resources/revisions/patched"),
            commit,
        )
        self.assertEqual(
            git("--git-dir", str(repository_path), "merge-base", "--is-ancestor", manifest["repositories"][0]["revisions"][0]["commit"], commit),
            "",
        )

    def test_changed_document_fails_check(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        (self.resources / "documents/reference.pdf").write_text("changed", encoding="utf-8")
        result = self.tool("check", "--root", str(self.resources), "test-document", expected=1)
        self.assertIn("changed  test-document", result.stdout)

    def test_unknown_resource_is_an_error(self) -> None:
        result = self.tool("populate", "--root", str(self.resources), "unknown", expected=2)
        self.assertIn("unknown resource ID", result.stderr)

    def test_env_and_path_discovery(self) -> None:
        dotenv_root = self.base / "dotenv-resources"
        shell_root = self.base / "shell-resources"
        (self.base / ".env").write_text(
            f"PAPER_RESOURCES_DIR={dotenv_root}\n"
            "PAPER_RESOURCES_DEFAULT_EXTRACTOR=pypdf-layout\n",
            encoding="utf-8",
        )
        self.assertEqual(self.tool("path").stdout.strip(), str(dotenv_root.resolve()))
        self.assertEqual(
            self.tool("env", environment={"PAPER_RESOURCES_DIR": str(shell_root)}).stdout.splitlines(),
            [
                f"PAPER_RESOURCES_DIR={shell_root.resolve()}",
                f"PAPER_RESOURCES_DB={shell_root.resolve() / 'resources.db'}",
                "PAPER_RESOURCES_DEFAULT_EXTRACTOR=pypdf-layout",
            ],
        )

    def test_missing_resource_directory_configuration(self) -> None:
        result = self.tool("path", expected=2, environment={"PAPER_RESOURCES_DIR": ""})
        self.assertIn("PAPER_RESOURCES_DIR is not configured", result.stderr)

    def test_cli_reuses_loaded_manifest_for_resource_manager(self) -> None:
        with (
            patch(
                "paper_resources.manager.load_manifest",
                wraps=manager_module.load_manifest,
            ) as load_manifest,
            patch("builtins.print"),
        ):
            result = cli.main(
                [
                    "--manifest",
                    str(self.manifest),
                    "index-status",
                    "--root",
                    str(self.resources),
                ]
            )
        self.assertEqual(result, 1)
        load_manifest.assert_called_once_with(self.manifest)

    def test_index_search_page_extract_and_json(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")

        indexed = self.tool("index", "--root", str(self.resources))
        self.assertIn(
            "indexed    test-document (new; 2 chunks; 2 pages; 0 sections)",
            indexed.stdout,
        )
        self.assertTrue((self.resources / "resources.db").is_file())

        current = self.tool("index", "--root", str(self.resources))
        self.assertIn("0 updated, 1 already current", current.stdout)
        status = self.tool("index-status", "--root", str(self.resources))
        self.assertIn("ok         test-document (current)", status.stdout)

        search = self.tool("search", "--root", str(self.resources), "power sequence")
        self.assertIn("test-document, PDF page 2", search.stdout)
        self.assertIn("[[power]] [[sequence]]", search.stdout.lower())

        filtered = self.tool(
            "search", "--root", str(self.resources), "--tag", "display", "sequence", "--json"
        )
        results = json.loads(filtered.stdout)
        self.assertEqual(results[0]["resource_id"], "test-document")
        self.assertEqual(results[0]["pages"], [2])

        page = json.loads(
            self.tool(
                "page", "--root", str(self.resources), "test-document", "2", "--json"
            ).stdout
        )
        self.assertEqual(page["page_number"], 2)
        self.assertIn("Display power sequence", page["content"])

        with sqlite3.connect(self.resources / "resources.db") as connection:
            section_id = connection.execute(
                "INSERT INTO document_sections(document_id, section_index, name) VALUES (?, ?, ?)",
                ("test-document", 0, "Power sequencing"),
            ).lastrowid
            chunk_id = connection.execute(
                "SELECT id FROM document_chunks "
                "WHERE document_id = ? AND chunk_index = ?",
                ("test-document", 1),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO document_section_chunks"
                "(document_id, section_id, chunk_id) VALUES (?, ?, ?)",
                ("test-document", section_id, chunk_id),
            )
        section = json.loads(
            self.tool(
                "section", "--root", str(self.resources), "test-document", "0", "--json"
            ).stdout
        )
        self.assertEqual(section["name"], "Power sequencing")
        self.assertEqual(section["pages"], [2])
        self.assertIn("Display power sequence", section["content"])

        extraction = json.loads(
            self.tool(
                "extract",
                "--root",
                str(self.resources),
                "--page",
                "1",
                "--json",
                "test-document",
            ).stdout
        )
        self.assertEqual(extraction["extractor"], "pypdf")
        self.assertEqual([page["page_number"] for page in extraction["pages"]], [1])
        self.assertEqual(len(extraction["chunks"]), 1)
        self.assertIn("Reference material", extraction["chunks"][0]["content"])
        self.assertEqual(
            extraction["chunk_pages"], [{"chunk_index": 0, "page_number": 1}]
        )

        with sqlite3.connect(self.resources / "resources.db") as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM document_chunks_fts"
                ).fetchone()[0],
                2,
            )

    def test_manifest_extractor_and_cli_override_precedence(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        self.tool("index", "--root", str(self.resources))

        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["documents"][0]["extractor"] = "pypdf-layout"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        reindexed = self.tool(
            "index",
            "--root",
            str(self.resources),
        )
        self.assertIn("reindexed  test-document", reindexed.stdout)
        self.assertIn("extractor pypdf -> pypdf-layout", reindexed.stdout)

        manifest["documents"][0]["description"] = "updated test document"
        manifest["documents"][0]["tags"].append("updated")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        metadata = self.tool(
            "index",
            "--root",
            str(self.resources),
            "--extractor",
            "pypdf-layout",
        )
        self.assertIn("updated    test-document (metadata)", metadata.stdout)

        overridden = self.tool(
            "index",
            "--root",
            str(self.resources),
            "--extractor",
            "pypdf",
            environment={"PAPER_RESOURCES_DEFAULT_EXTRACTOR": "not-used"},
        )
        self.assertIn("extractor pypdf-layout -> pypdf", overridden.stdout)

        del manifest["documents"][0]["extractor"]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        from_environment = self.tool(
            "index",
            "--root",
            str(self.resources),
            environment={"PAPER_RESOURCES_DEFAULT_EXTRACTOR": "pypdf-layout"},
        )
        self.assertIn("extractor pypdf -> pypdf-layout", from_environment.stdout)

    def test_mcp_tools_and_resources(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        self.tool(
            "populate", "--root", str(self.resources),
            "--revision", "test-repository", "alternate",
        )
        self.tool("index", "--root", str(self.resources))
        with sqlite3.connect(self.resources / "resources.db") as connection:
            section_id = connection.execute(
                """
                INSERT INTO document_sections(document_id, section_index, name)
                VALUES ('test-document', 0, 'Power sequencing')
                """
            ).lastrowid
            chunk_id = connection.execute(
                """
                SELECT id FROM document_chunks
                WHERE document_id = 'test-document' AND chunk_index = 1
                """
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO document_section_chunks(
                    document_id, section_id, chunk_id
                )
                VALUES ('test-document', ?, ?)
                """,
                (section_id, chunk_id),
            )
        settings = ResourceSettings.load(self.manifest, self.resources)
        manager = ResourceManager.load(settings)
        self.addCleanup(manager.close)
        server = create_server(manager)

        async def exercise_server() -> None:
            tools = await server.list_tools()
            names = {tool.name for tool in tools}
            self.assertEqual(
                names,
                {
                    "get_catalog_info",
                    "list_resources",
                    "get_resource",
                    "search_documents",
                    "get_document_page",
                    "get_index_status",
                    "index_documents",
                    "list_repositories",
                    "get_repository",
                    "list_revisions",
                    "get_revision",
                    "compare_revisions",
                    "diff_revision_file",
                    "list_patches",
                    "get_patch",
                    "list_worktrees",
                    "get_worktree",
                },
            )
            self.assertNotIn("populate_resources", names)
            tools_by_name = {tool.name: tool for tool in tools}
            self.assertTrue(
                tools_by_name["search_documents"].annotations.read_only_hint
            )
            self.assertTrue(
                tools_by_name["compare_revisions"].annotations.read_only_hint
            )
            self.assertFalse(
                tools_by_name["index_documents"].annotations.read_only_hint
            )

            resources = await server.list_resources()
            self.assertEqual(
                {str(resource.uri) for resource in resources},
                {
                    "paper-resource://catalog",
                    "paper-resource://documents",
                    "paper-resource://repositories",
                    "paper-resource://worktrees",
                    "paper-resource://patches",
                    "paper-resource://revisions",
                },
            )
            templates = await server.list_resource_templates()
            self.assertEqual(
                {template.uri_template for template in templates},
                {
                    "paper-resource://documents/{document_id}",
                    "paper-resource://documents/{document_id}/pages/{page_number}",
                    "paper-resource://documents/{document_id}/sections/{section_index}",
                    "paper-resource://repositories/{repository_id}",
                    "paper-resource://patches/{patch_id}",
                    "paper-resource://revisions/{repository_id}",
                    "paper-resource://revisions/{repository_id}/{revision_id}",
                    "paper-resource://worktrees/{repository_id}",
                    "paper-resource://worktrees/{repository_id}/{revision_id}",
                    "paper-resource://worktrees/{repository_id}/{revision_id}/{worktree_id}",
                },
            )

            catalog = await server.call_tool("get_catalog_info", {})
            self.assertEqual(catalog.structured_content["documents"], 1)

            listed = await server.call_tool(
                "list_resources", {"kind": "document", "tag": "display"}
            )
            self.assertEqual(
                listed.structured_content["result"][0]["id"], "test-document"
            )

            detail = await server.call_tool(
                "get_resource", {"resource_id": "test-document"}
            )
            self.assertTrue(detail.structured_content["available"])

            comparison = await server.call_tool(
                "compare_revisions",
                {
                    "repository_id": "test-repository",
                    "from_revision_id": "v1",
                    "to_revision_id": "alternate",
                    "path": "README",
                },
            )
            self.assertEqual(comparison.structured_content["total"], 1)
            self.assertEqual(
                comparison.structured_content["changes"],
                [{"status": "M", "path": "README"}],
            )
            file_diff = await server.call_tool(
                "diff_revision_file",
                {
                    "repository_id": "test-repository",
                    "from_revision_id": "v1",
                    "to_revision_id": "alternate",
                    "path": "README",
                },
            )
            self.assertIn("+second", file_diff.structured_content["diff"])

            with self.assertRaisesRegex(ToolError, "unknown resource ID"):
                await server.call_tool(
                    "get_resource", {"resource_id": "not-present"}
                )

            search = await server.call_tool(
                "search_documents", {"query": "power sequence"}
            )
            self.assertFalse(search.is_error)
            self.assertEqual(
                search.structured_content["result"][0]["resource_id"],
                "test-document",
            )
            self.assertEqual(search.structured_content["result"][0]["pages"], [2])

            page = await server.call_tool(
                "get_document_page",
                {"document_id": "test-document", "page_number": 2},
            )
            self.assertFalse(page.is_error)
            self.assertIn("Display power sequence", page.structured_content["content"])

            status = await server.call_tool(
                "get_index_status", {"resource_ids": ["test-document"]}
            )
            self.assertEqual(status.structured_content["result"][0]["status"], "ok")

            indexed = await server.call_tool(
                "index_documents", {"resource_ids": ["test-document"]}
            )
            self.assertFalse(indexed.is_error)
            self.assertEqual(indexed.structured_content["unchanged"], 1)

            resource = await server.read_resource("paper-resource://catalog")
            self.assertIn("test-document", list(resource)[0].content)

            document = await server.read_resource(
                "paper-resource://documents/test-document"
            )
            self.assertEqual(
                json.loads(list(document)[0].content)["kind"], "document"
            )

            page_resource = await server.read_resource(
                "paper-resource://documents/test-document/pages/2"
            )
            page_content = json.loads(list(page_resource)[0].content)
            self.assertEqual(page_content["page_number"], 2)
            self.assertIn("Display power sequence", page_content["content"])

            section_resource = await server.read_resource(
                "paper-resource://documents/test-document/sections/0"
            )
            section_content = json.loads(list(section_resource)[0].content)
            self.assertEqual(section_content["name"], "Power sequencing")
            self.assertEqual(section_content["pages"], [2])

            repository = await server.read_resource(
                "paper-resource://repositories/test-repository"
            )
            self.assertEqual(
                json.loads(list(repository)[0].content)["kind"], "repository"
            )

            worktree = await server.read_resource(
                "paper-resource://worktrees/test-repository/v1/default"
            )
            self.assertEqual(
                json.loads(list(worktree)[0].content)["revision_id"], "v1"
            )

            revision = await server.read_resource(
                "paper-resource://revisions/test-repository/v1"
            )
            self.assertEqual(
                json.loads(list(revision)[0].content)["author"], "test"
            )

            with self.assertRaises(ResourceNotFoundError):
                await server.read_resource(
                    "paper-resource://documents/not-present"
                )

        with patch(
            "paper_resources.database.open_database",
            wraps=database.open_database,
        ) as open_database:
            anyio.run(exercise_server)
        open_database.assert_called_once_with(settings.database, create=False)

    def test_resource_manager_reopens_database_after_close(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        settings = ResourceSettings.load(self.manifest, self.resources)
        manager = ResourceManager.load(settings)
        self.addCleanup(manager.close)

        with patch(
            "paper_resources.database.open_database",
            wraps=database.open_database,
        ) as open_database:
            manager.index_documents()
            manager.search_documents("power sequence")
            manager.get_document_page("test-document", 2)
            open_database.assert_called_once_with(settings.database, create=True)

            manager.close()
            manager.index_status()
            self.assertEqual(open_database.call_count, 2)

    def test_failed_reindex_preserves_previous_content(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        self.tool("index", "--root", str(self.resources))

        document_path = self.resources / "documents/reference.pdf"
        document_path.write_bytes(b"not a PDF")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["documents"][0]["sha256"] = hashlib.sha256(
            document_path.read_bytes()
        ).hexdigest()
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        failed = self.tool("index", "--root", str(self.resources), expected=1)
        self.assertIn("failed     test-document", failed.stdout)
        preserved = self.tool(
            "search", "--root", str(self.resources), "power sequence"
        )
        self.assertIn("test-document, PDF page 2", preserved.stdout)

    def test_generator_events_assign_ids_and_create_relationships(self) -> None:
        database_path = self.base / "events.db"
        connection = database.open_database(database_path, create=True)
        document = {
            "id": "event-document",
            "description": "event document",
            "path": "documents/event.html",
            "sha256": "0" * 64,
            "tags": ["event"],
        }
        extractor = catalog_index.Extractor(
            "test", "test;pipeline=1", lambda path: iter(())
        )

        def events():
            page = catalog_index.Page(label="iv")
            section = catalog_index.Section(name="Introduction")
            yield page
            yield section
            yield catalog_index.Chunk(
                "Generated content", pages=(page,), sections=(section,)
            )

        try:
            with connection:
                catalog_index.insert_document_metadata(
                    connection, document, extractor
                )
                counts = catalog_index.consume_extraction(
                    connection, document["id"], events()
                )
            self.assertEqual(counts, catalog_index.ExtractionCounts(1, 1, 1))
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT page_number, page_label FROM document_pages"
                ).fetchall()[0]),
                (1, "iv"),
            )
            self.assertEqual(
                tuple(connection.execute(
                    """
                    SELECT document_sections.section_index, document_sections.name,
                           document_chunks.chunk_index, document_chunks.content
                    FROM document_section_chunks
                    JOIN document_sections
                      ON document_sections.id = document_section_chunks.section_id
                    JOIN document_chunks
                      ON document_chunks.id = document_section_chunks.chunk_id
                    """
                ).fetchall()[0]),
                (0, "Introduction", 0, "Generated content"),
            )

            def broken_events():
                page = catalog_index.Page()
                yield page
                yield catalog_index.Chunk("Partial replacement", pages=(page,))
                raise catalog_index.CatalogIndexError("deliberate extraction failure")

            with self.assertRaisesRegex(
                catalog_index.CatalogIndexError, "deliberate extraction failure"
            ):
                with connection:
                    connection.execute(
                        "DELETE FROM documents WHERE id = ?", (document["id"],)
                    )
                    catalog_index.insert_document_metadata(
                        connection, document, extractor
                    )
                    catalog_index.consume_extraction(
                        connection, document["id"], broken_events()
                    )
            self.assertEqual(
                connection.execute(
                    "SELECT content FROM document_chunks"
                ).fetchone()[0],
                "Generated content",
            )
            future_page = catalog_index.Page()
            with self.assertRaisesRegex(
                catalog_index.CatalogIndexError, "before yielding it"
            ):
                catalog_index.consume_extraction(
                    connection,
                    document["id"],
                    [catalog_index.Chunk("Out of order", pages=(future_page,))],
                )
        finally:
            connection.close()

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is not installed")
    def test_pdftotext_extractors(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        for extractor in ("pdftotext", "pdftotext-layout"):
            extraction = json.loads(
                self.tool(
                    "extract",
                    "--root",
                    str(self.resources),
                    "--extractor",
                    extractor,
                    "--page",
                    "2",
                    "--json",
                    "test-document",
                ).stdout
            )
            self.assertEqual(len(extraction["pages"]), 1)
            self.assertEqual(extraction["pages"][0]["page_number"], 2)
            self.assertEqual(len(extraction["chunks"]), 1)
            self.assertIn("Display power sequence", extraction["chunks"][0]["content"])


if __name__ == "__main__":
    unittest.main()
