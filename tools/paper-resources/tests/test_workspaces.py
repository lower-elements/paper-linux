import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from paper_resources import (
    buildroot, database, patch_export, repository_index, workspace_store,
    workspaces,
)
from paper_resources.config import ResourceError, ResourceSettings
from paper_resources.manager import ResourceManager


class BuildrootInspectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.output = self.project / "output" / "test_board"
        self.buildroot = self.project / "buildroot"
        self.resources = self.root / "resources"
        self.output.mkdir(parents=True)
        self.buildroot.mkdir()
        self.resources.mkdir()
        (self.output / "Makefile").write_text("# generated\n", encoding="utf-8")
        (self.output / ".config").write_text("BR2_TEST=y\n", encoding="utf-8")
        (self.buildroot / "package/foo/1.0").mkdir(parents=True)
        (self.buildroot / "package/foo/1.0/0001-internal.patch").write_text(
            "internal\n", encoding="utf-8"
        )
        external = self.project / "patches/layer/foo"
        (external / "1.0").mkdir(parents=True)
        (external / "0001-wrong-version.patch").write_text(
            "wrong\n", encoding="utf-8"
        )
        (external / "1.0/0002-editable.patch").write_text(
            "editable\n", encoding="utf-8"
        )
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        make = self.fake_bin / "make"
        make.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json
            import sys

            args = sys.argv[1:]
            target = next(item for item in args if item in ('foo-show-info', 'show-vars'))
            cleared = 'FOO_OVERRIDE_SRCDIR=' in args
            if target == 'foo-show-info':
                assert cleared
                print(json.dumps({{'foo': {{'name': 'foo', 'version': '1.0'}}}}))
            elif cleared:
                values = {{
                    'FOO_RAWNAME': 'foo',
                    'FOO_NAME': 'foo',
                    'FOO_VERSION': '1.0',
                    'FOO_DL_VERSION': '1.0',
                    'FOO_SITE': 'https://example.invalid/foo.git',
                    'FOO_SITE_METHOD': 'git',
                    'FOO_SOURCE': 'foo-1.0.tar.gz',
                    'FOO_PATCH': '',
                    'FOO_PKGDIR': 'package/foo',
                    'FOO_DL_DIR': 'dl/foo',
                    'FOO_PRE_PATCH_HOOKS': 'FOO_PREPARE',
                    'FOO_POST_PATCH_HOOKS': 'FOO_FINISH',
                    'FOO_OVERRIDE_SRCDIR': '',
                    'BR2_PACKAGE_OVERRIDE_FILE': '"{self.output / 'custom.mk'}"',
                    'PAPER_PATCH_DIRS': 'package/foo/1.0 {external / '1.0'}',
                    'PAPER_PATCH_HASH_DIRS': 'package/foo {external}',
                }}
                print(json.dumps({{key: {{'raw': value, 'expanded': value}} for key, value in values.items()}}))
            else:
                print(json.dumps({{'FOO_OVERRIDE_SRCDIR': {{
                    'raw': '/tmp/live', 'expanded': '/tmp/live'
                }}}}))
            """), encoding="utf-8")
        make.chmod(0o755)

    def inspect(self) -> buildroot.PackageInspection:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.fake_bin}:{environment['PATH']}"
        with patch.dict(os.environ, environment, clear=True):
            return buildroot.inspect_package(
                self.project, self.resources, [], "test_board", "foo"
            )

    def test_make_metadata_preserves_pipeline_order_and_version_selection(self) -> None:
        result = self.inspect()
        self.assertEqual(result.effective_override, "/tmp/live")
        self.assertEqual(
            [item.path for item in result.prerequisite_patches],
            ["buildroot/package/foo/1.0/0001-internal.patch"],
        )
        self.assertEqual(
            [item.path for item in result.editable_patches],
            ["patches/layer/foo/1.0/0002-editable.patch"],
        )
        self.assertNotIn(
            "patches/layer/foo/0001-wrong-version.patch",
            [item.path for item in result.editable_patches],
        )
        self.assertEqual(result.pre_patch_hooks, ("FOO_PREPARE",))
        self.assertEqual(result.post_patch_hooks, ("FOO_FINISH",))
        self.assertTrue(any("not provably bound" in item for item in result.warnings))

    def test_rejects_unsupported_series_before_omitting_patches(self) -> None:
        (self.project / "patches/layer/foo/1.0/series").write_text(
            "0002-editable.patch\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ResourceError, "series file"):
            self.inspect()

    def test_validates_configuration_and_package_before_make(self) -> None:
        with self.assertRaisesRegex(ResourceError, "configuration name"):
            buildroot.inspect_package(
                self.project, self.resources, [], "../bad", "foo"
            )
        with self.assertRaisesRegex(ResourceError, "package name"):
            buildroot.inspect_package(
                self.project, self.resources, [], "test_board", "$(shell bad)"
            )


class WorkspacePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_schema_eight_migrates_without_losing_catalog_data(self) -> None:
        path = self.root / "resources.db"
        connection = __import__("sqlite3").connect(path)
        connection.executescript(
            database.SCHEMA.replace("PRAGMA user_version = 9;", "PRAGMA user_version = 8;")
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, description, path, sha256, extractor, extractor_version
            ) VALUES ('kept', 'kept', 'kept.pdf', ?, 'test', '1')
            """,
            (b"x" * 32,),
        )
        connection.commit()
        connection.close()

        migrated = database.open_database(path, create=True)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertEqual(
            migrated.execute("SELECT id FROM documents").fetchone()[0], "kept"
        )
        self.assertIsNotNone(
            migrated.execute(
                "SELECT name FROM sqlite_master WHERE name = 'workspaces'"
            ).fetchone()
        )

    def test_local_revisions_survive_manifest_catalog_synchronization(self) -> None:
        path = self.root / "resources.db"
        connection = database.open_database(path, create=True)
        self.addCleanup(connection.close)
        local = workspace_store.LocalRevision(
            repository_id="repo",
            repository_path="git/repo.git",
            revision_id="local-base",
            commit="11" * 20,
            tree="22" * 20,
            parent_commit=None,
            description="local base",
            author="Paper Resources",
            index=True,
            construction_policy="package-patches-v1",
            input_fingerprint="fingerprint",
            provenance={"kind": "workspace-base"},
            created_at=workspace_store.now(),
        )
        workspace_store.register_local_revision(connection, local)
        repository_index.synchronize_catalog(connection, [])
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM local_revision_definitions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM repository_revisions"
            ).fetchone()[0],
            1,
        )

        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps({"version": 2, "documents": [], "patches": [], "repositories": []}),
            encoding="utf-8",
        )
        settings = ResourceSettings(
            manifest_path=manifest,
            root=self.root,
            database=path,
            default_extractor="pypdf",
        )
        manager = ResourceManager.load(settings)
        self.addCleanup(manager.close)
        revision = manager.get_revision("repo", "local-base")
        self.assertEqual(revision.commit, "11" * 20)
        self.assertTrue(revision.index)

    def test_local_revision_identity_is_immutable(self) -> None:
        connection = database.open_database(self.root / "resources.db", create=True)
        self.addCleanup(connection.close)
        original = workspace_store.LocalRevision(
            "repo", "git/repo.git", "local", "11" * 20, "22" * 20,
            None, "local", "Paper Resources", False, "v1", "one", {},
            workspace_store.now(),
        )
        workspace_store.register_local_revision(connection, original)
        changed = workspace_store.LocalRevision(
            "repo", "git/repo.git", "local", "33" * 20, "44" * 20,
            None, "changed", "Paper Resources", False, "v1", "two", {},
            workspace_store.now(),
        )
        with self.assertRaisesRegex(ResourceError, "immutable"):
            workspace_store.register_local_revision(connection, changed)


def run_git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class WorkspaceOpenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.resources = self.root / "resources"
        self.source = self.root / "source"
        self.project.mkdir()
        self.resources.mkdir()
        self.source.mkdir()
        run_git("init", "-b", "main", cwd=self.source)
        (self.source / "file.txt").write_text("base\n", encoding="utf-8")
        run_git("add", "file.txt", cwd=self.source)
        self.commit("base")
        self.base_commit = run_git("rev-parse", "HEAD", cwd=self.source)
        self.base_tree = run_git("rev-parse", "HEAD^{tree}", cwd=self.source)

        patch_dir = self.project / "patches/foo/1.0"
        patch_dir.mkdir(parents=True)
        (self.source / "file.txt").write_text("base\ninternal\n", encoding="utf-8")
        run_git("add", "file.txt", cwd=self.source)
        self.commit("internal prerequisite")
        internal = self.project / "internal.patch"
        internal.write_bytes(subprocess.run(
            ["git", "format-patch", "-1", "--stdout", "--no-signature"],
            cwd=self.source, check=True, stdout=subprocess.PIPE,
        ).stdout)
        (self.source / "file.txt").write_text(
            "base\ninternal\nfirst\n", encoding="utf-8"
        )
        run_git("add", "file.txt", cwd=self.source)
        self.commit("first editable")
        first = patch_dir / "0001-first-editable.patch"
        first.write_bytes(subprocess.run(
            ["git", "format-patch", "-1", "--stdout", "--no-signature"],
            cwd=self.source, check=True, stdout=subprocess.PIPE,
        ).stdout)
        (self.source / "file.txt").write_text(
            "base\ninternal\nfirst\nsecond\n", encoding="utf-8"
        )
        run_git("add", "file.txt", cwd=self.source)
        self.commit("second editable")
        second = patch_dir / "0002-second-editable.patch"
        second.write_bytes(subprocess.run(
            ["git", "format-patch", "-1", "--stdout", "--no-signature"],
            cwd=self.source, check=True, stdout=subprocess.PIPE,
        ).stdout)

        repository_path = self.resources / "git/repo.git"
        repository_path.parent.mkdir(parents=True)
        run_git("clone", "--bare", str(self.source), str(repository_path), cwd=self.root)
        self.repository_path = repository_path
        self.connection = database.open_database(
            self.resources / "resources.db", create=True
        )
        self.addCleanup(self.connection.close)
        patch_input = lambda path, stage, directory=None: buildroot.PatchInput(
            path=path.relative_to(self.project).as_posix(),
            resolved_path=str(path), stage=stage, directory=directory,
            sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        )
        binding = buildroot.SourceBinding(
            "repo", "v1", self.base_commit, self.base_tree,
            "declared-source-correspondence",
        )
        self.inspection = buildroot.PackageInspection(
            buildroot_config="test_board", project_root=str(self.project),
            output_path=str(self.project / "output/test_board"),
            buildroot_root=str(self.project / "buildroot"),
            buildroot_revision=None, package="foo", variable_prefix="FOO",
            version="1.0", download_version="1.0", source="foo.tar.gz",
            site="https://example.invalid/foo", site_method="https",
            recipe_directory="buildroot/package/foo",
            download_directory=str(self.project / "dl/foo"),
            override_file=str(self.project / "output/test_board/local.mk"),
            effective_override=None, source_binding=binding,
            source_fingerprint="source-fingerprint",
            input_fingerprint="input-fingerprint",
            prerequisite_patches=(patch_input(internal, "prerequisite"),),
            editable_patches=(
                patch_input(first, "editable", "patches/foo/1.0"),
                patch_input(second, "editable", "patches/foo/1.0"),
            ),
            patch_directories=(buildroot.PatchDirectory(
                "patches/foo/1.0", str(patch_dir), "editable", True, True,
            ),),
            pre_patch_hooks=(), post_patch_hooks=("FOO_POST",),
            excluded_stages=("post-patch hooks: FOO_POST",), warnings=(),
        )
        self.repositories = {
            "repo": {"id": "repo", "path": "git/repo.git", "revisions": []}
        }

    def commit(self, message: str) -> None:
        run_git(
            "-c", "user.name=Patch Author", "-c",
            "user.email=author@example.invalid", "-c", "commit.gpgSign=false",
            "commit", "-m", message, "-m",
            "Signed-off-by: Patch Author <author@example.invalid>", cwd=self.source,
        )

    def test_open_imports_stack_and_native_rebase_keeps_isolated_notes(self) -> None:
        first = workspaces.open_workspace(
            self.connection, self.inspection, self.repositories,
            self.resources, "display",
        )
        self.assertTrue(first.created)
        self.assertTrue(first.status.exported)
        self.assertEqual(first.status.commit_count, 2)
        self.assertEqual(first.status.annotated_commits, 2)
        checkout = Path(first.status.path)
        self.assertEqual(
            (checkout / "file.txt").read_text(encoding="utf-8"),
            "base\ninternal\nfirst\nsecond\n",
        )
        second = workspaces.open_workspace(
            self.connection, self.inspection, self.repositories,
            self.resources, "reader",
        )
        old_commits = run_git(
            "rev-list", "--reverse", f"{first.status.export_base}..HEAD",
            cwd=checkout,
        ).splitlines()
        env = os.environ.copy()
        env.update({
            "GIT_COMMITTER_NAME": "Rewriter",
            "GIT_COMMITTER_EMAIL": "rewriter@example.invalid",
        })
        rebase = subprocess.run(
            [
                "git", "-c", "commit.gpgSign=false", "rebase",
                "--force-rebase", first.status.export_base,
            ],
            cwd=checkout, env=env, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(rebase.returncode, 0, rebase.stderr)
        status = workspaces.get_workspace_status(
            self.connection,
            workspace_store.get_workspace(self.connection, "foo", "display"),
        )
        self.assertEqual(status.annotated_commits, 2)
        self.assertEqual(status.missing_annotations, ())
        new_commits = run_git(
            "rev-list", "--reverse", f"{status.export_base}..HEAD", cwd=checkout
        ).splitlines()
        self.assertNotEqual(old_commits, new_commits)
        reader = workspace_store.get_workspace(self.connection, "foo", "reader")
        self.assertEqual(
            workspaces.read_note(
                self.repository_path, reader.notes_ref, new_commits[-1]
            ),
            (),
        )
        self.assertTrue(second.status.exported)

    def test_annotation_rejects_paths_outside_selected_layers(self) -> None:
        opened = workspaces.open_workspace(
            self.connection, self.inspection, self.repositories,
            self.resources, "display",
        )
        workspace = workspace_store.get_workspace(
            self.connection, "foo", "display"
        )
        with self.assertRaisesRegex(ResourceError, "outside"):
            workspaces.annotate_workspace_commit(
                self.connection, workspace, "HEAD", "elsewhere/0001.patch"
            )
        result = workspaces.annotate_workspace_commit(
            self.connection, workspace, "HEAD",
            "patches/foo/1.0/0009-renamed.patch",
        )
        self.assertEqual(result.patch_path, "patches/foo/1.0/0009-renamed.patch")

    def test_export_amended_history_allocates_replays_and_publishes(self) -> None:
        opened = workspaces.open_workspace(
            self.connection, self.inspection, self.repositories,
            self.resources, "display",
        )
        checkout = Path(opened.status.path)
        editor = self.root / "edit-todo.py"
        editor.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            from pathlib import Path
            import sys
            path = Path(sys.argv[1])
            lines = path.read_text().splitlines()
            lines[0] = lines[0].replace('pick ', 'edit ', 1)
            path.write_text('\\n'.join(lines) + '\\n')
            """), encoding="utf-8")
        editor.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "GIT_SEQUENCE_EDITOR": str(editor),
            "GIT_COMMITTER_NAME": "Patch Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        })
        rebase = subprocess.run(
            [
                "git", "-c", "commit.gpgSign=false", "rebase", "-i",
                opened.status.export_base,
            ],
            cwd=checkout, env=environment, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(rebase.returncode, 0, rebase.stderr)
        self.assertTrue((checkout / ".git").is_file())
        (checkout / "early.txt").write_text("early edit\n", encoding="utf-8")
        run_git("add", "early.txt", cwd=checkout)
        subprocess.run(
            [
                "git", "-c", "commit.gpgSign=false", "commit", "--amend",
                "--no-edit",
            ], cwd=checkout, env=environment, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgSign=false", "rebase", "--continue"],
            cwd=checkout, env=environment, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        (checkout / "third.txt").write_text("third\n", encoding="utf-8")
        run_git("add", "third.txt", cwd=checkout)
        run_git(
            "-c", "user.name=Patch Author", "-c",
            "user.email=author@example.invalid", "-c", "commit.gpgSign=false",
            "commit", "-m", "third editable", "-m",
            "Signed-off-by: Patch Author <author@example.invalid>", cwd=checkout,
        )
        workspace = workspace_store.get_workspace(
            self.connection, "foo", "display"
        )
        head = run_git("rev-parse", "HEAD", cwd=checkout)
        self.assertEqual(
            workspaces.read_note(self.repository_path, workspace.notes_ref, head), ()
        )
        dry_run = workspaces.export_workspace(
            self.connection, workspace, self.inspection, dry_run=True
        )
        self.assertEqual(dry_run.created_paths, (
            "patches/foo/1.0/0003-third-editable.patch",
        ))
        self.assertFalse(
            (self.project / dry_run.created_paths[0]).exists()
        )
        self.assertEqual(
            workspaces.read_note(self.repository_path, workspace.notes_ref, head), ()
        )
        exported = workspaces.export_workspace(
            self.connection, workspace, self.inspection
        )
        self.assertFalse(exported.dry_run)
        self.assertTrue((self.project / exported.created_paths[0]).is_file())
        self.assertIn(
            b"Signed-off-by: Patch Author <author@example.invalid>",
            (self.project / exported.created_paths[0]).read_bytes(),
        )
        self.assertEqual(
            workspaces.read_note(self.repository_path, workspace.notes_ref, head),
            (exported.created_paths[0],),
        )
        status = workspaces.get_workspace_status(
            self.connection, workspace
        )
        self.assertTrue(status.exported)


class ExportPlannerTest(unittest.TestCase):
    def commit(self, oid: str, note: str | None) -> patch_export.ExportCommit:
        return patch_export.ExportCommit(
            oid * 40, f"change {oid}", (note,) if note else ()
        )

    def plan(self, commits, existing=()):
        return patch_export.plan_export(
            commits, directories=("patches/foo",), existing_paths=existing,
            managed_paths=existing,
        )

    def test_allocates_numbered_gaps_and_leading_trailing_runs(self) -> None:
        plan = self.plan([
            self.commit("a", "patches/foo/0005-five.patch"),
            self.commit("b", None),
            self.commit("c", "patches/foo/0007-seven.patch"),
            self.commit("d", None),
        ], existing=(
            "patches/foo/0005-five.patch", "patches/foo/0007-seven.patch",
        ))
        self.assertEqual(
            [item.path for item in plan.patches],
            [
                "patches/foo/0005-five.patch",
                "patches/foo/0006-change-b.patch",
                "patches/foo/0007-seven.patch",
                "patches/foo/0008-change-d.patch",
            ],
        )
        leading = self.plan([
            self.commit("a", None),
            self.commit("b", "patches/foo/0002-two.patch"),
        ], existing=("patches/foo/0002-two.patch",))
        self.assertEqual(leading.patches[0].path, "patches/foo/0001-change-a.patch")

    def test_rejects_full_gap_unannotated_and_cross_layer_runs(self) -> None:
        with self.assertRaisesRegex(ResourceError, "no ordered filename slots"):
            self.plan([
                self.commit("a", "patches/foo/0005-five.patch"),
                self.commit("b", None),
                self.commit("c", "patches/foo/0006-six.patch"),
            ], existing=(
                "patches/foo/0005-five.patch", "patches/foo/0006-six.patch",
            ))
        with self.assertRaisesRegex(ResourceError, "entirely unannotated"):
            self.plan([self.commit("a", None)])
        with self.assertRaisesRegex(ResourceError, "cannot infer patch layer"):
            patch_export.plan_export(
                [
                    self.commit("a", "one/0001-one.patch"),
                    self.commit("b", None),
                    self.commit("c", "two/0001-two.patch"),
                ],
                directories=("one", "two"), existing_paths=(), managed_paths=(),
            )

    def test_duplicate_destination_splits_after_first_and_respects_occupancy(self) -> None:
        plan = self.plan([
            self.commit("a", "patches/foo/0005-five.patch"),
            self.commit("b", "patches/foo/0005-five.patch"),
        ], existing=("patches/foo/0005-five.patch",))
        self.assertEqual(plan.patches[1].path, "patches/foo/0006-change-b.patch")
        with self.assertRaisesRegex(ResourceError, "numeric patch prefix collision"):
            self.plan(
                [self.commit("a", "patches/foo/0005-new.patch")],
                existing=("patches/foo/0005-old.patch",),
            )


if __name__ == "__main__":
    unittest.main()
