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


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write_test_pdf(path: Path, pages: list[str]) -> None:
    """Write a small Type 1-font PDF whose text is extractable by both backends."""
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
        source.mkdir()
        git("init", "-b", "main", cwd=source)
        (source / "README").write_text("first\n", encoding="utf-8")
        git("add", "README", cwd=source)
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

        document = self.base / "source-document.pdf"
        write_test_pdf(
            document,
            ["Reference material overview", "Display power sequence details"],
        )
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
        manifest = {
            "version": 1,
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
            "repositories": [
                {
                    "id": "test-repository",
                    "description": "test repository",
                    "path": "git/test.git",
                    "clone_url": str(source),
                    "remotes": [
                        {
                            "name": "alternate",
                            "url": str(source),
                            "fetch": [
                                "+refs/heads/alternate:refs/remotes/alternate/alternate"
                            ],
                        }
                    ],
                    "worktrees": [
                        {
                            "id": "test-v1",
                            "path": "worktrees/test-v1",
                            "ref": "v1",
                        },
                        {
                            "id": "test-alternate",
                            "path": "worktrees/test-alternate",
                            "ref": "refs/remotes/alternate/alternate",
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
            "PAPER_RESOURCES_PDF_BACKEND",
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
        self.assertIn("ok       test-v1", second.stdout)

    def test_populate_one_worktree(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-v1")
        self.assertTrue((self.resources / "worktrees/test-v1").is_dir())
        self.assertFalse((self.resources / "worktrees/test-alternate").exists())
        self.tool("check", "--root", str(self.resources), "test-v1")

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
            f"PAPER_RESOURCES_DIR={dotenv_root}\n", encoding="utf-8"
        )
        self.assertEqual(self.tool("path").stdout.strip(), str(dotenv_root.resolve()))
        self.assertEqual(
            self.tool("env", environment={"PAPER_RESOURCES_DIR": str(shell_root)}).stdout.splitlines(),
            [
                f"PAPER_RESOURCES_DIR={shell_root.resolve()}",
                f"PAPER_RESOURCES_DB={shell_root.resolve() / 'resources.db'}",
                "PAPER_RESOURCES_PDF_BACKEND=pypdf",
            ],
        )

    def test_missing_resource_directory_configuration(self) -> None:
        result = self.tool("path", expected=2, environment={"PAPER_RESOURCES_DIR": ""})
        self.assertIn("PAPER_RESOURCES_DIR is not configured", result.stderr)

    def test_index_search_page_extract_and_json(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")

        indexed = self.tool("index", "--root", str(self.resources))
        self.assertIn("indexed    test-document (new; 2 pages)", indexed.stdout)
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
        self.assertEqual(extraction["extractor"], "pypdf/plain")
        self.assertEqual([page["page_number"] for page in extraction["pages"]], [1])

        with sqlite3.connect(self.resources / "resources.db") as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0], 2)

    def test_reindexes_for_backend_and_updates_metadata_without_extraction(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        self.tool("index", "--root", str(self.resources))

        reindexed = self.tool(
            "index",
            "--root",
            str(self.resources),
            "--pdf-backend",
            "pypdf-layout",
        )
        self.assertIn("reindexed  test-document", reindexed.stdout)
        self.assertIn("extractor pypdf/plain -> pypdf/layout", reindexed.stdout)

        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["documents"][0]["description"] = "updated test document"
        manifest["documents"][0]["tags"].append("updated")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        metadata = self.tool(
            "index",
            "--root",
            str(self.resources),
            "--pdf-backend",
            "pypdf-layout",
        )
        self.assertIn("updated    test-document (metadata)", metadata.stdout)

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

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is not installed")
    def test_pdftotext_backends(self) -> None:
        self.tool("populate", "--root", str(self.resources), "test-document")
        for backend in ("pdftotext", "pdftotext-layout"):
            extraction = json.loads(
                self.tool(
                    "extract",
                    "--root",
                    str(self.resources),
                    "--pdf-backend",
                    backend,
                    "--page",
                    "2",
                    "--json",
                    "test-document",
                ).stdout
            )
            self.assertEqual(len(extraction["pages"]), 1)
            self.assertEqual(extraction["pages"][0]["page_number"], 2)
            self.assertIn("Display power sequence", extraction["pages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
