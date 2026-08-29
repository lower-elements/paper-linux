import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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

        document = self.base / "source-document.txt"
        document.write_bytes(b"reference material\n")
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
        manifest = {
            "version": 1,
            "documents": [
                {
                    "id": "test-document",
                    "description": "test document",
                    "path": "documents/reference.txt",
                    "url": document.as_uri(),
                    "sha256": digest,
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
        (self.resources / "documents/reference.txt").write_text("changed", encoding="utf-8")
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
            self.tool("env", environment={"PAPER_RESOURCES_DIR": str(shell_root)}).stdout.strip(),
            f"PAPER_RESOURCES_DIR={shell_root.resolve()}",
        )

    def test_missing_resource_directory_configuration(self) -> None:
        result = self.tool("path", expected=2, environment={"PAPER_RESOURCES_DIR": ""})
        self.assertIn("PAPER_RESOURCES_DIR is not configured", result.stderr)


if __name__ == "__main__":
    unittest.main()
