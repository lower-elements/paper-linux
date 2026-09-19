import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from paper_resources import buildroot
from paper_resources.config import ResourceError


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


if __name__ == "__main__":
    unittest.main()
