"""Buildroot package metadata and patch-stack resolution.

The generated output-tree Makefile is the authority for expanded package
metadata.  This module deliberately does not parse Buildroot makefiles.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Iterable

from .config import ResourceError


_CONFIGURATION = re.compile(r"^[A-Za-z0-9_]+$")
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+_.-]*$")


@dataclass(frozen=True)
class PatchInput:
    path: str
    resolved_path: str
    stage: str
    directory: str | None
    sha256: str


@dataclass(frozen=True)
class PatchDirectory:
    path: str
    resolved_path: str
    stage: str
    exists: bool
    selected: bool


@dataclass(frozen=True)
class SourceBinding:
    repository: str
    revision: str
    commit: str
    tree: str
    correspondence: str


@dataclass(frozen=True)
class PackageInspection:
    buildroot_config: str
    project_root: str
    output_path: str
    buildroot_root: str
    buildroot_revision: str | None
    package: str
    variable_prefix: str
    version: str
    download_version: str
    source: str
    site: str
    site_method: str
    recipe_directory: str
    download_directory: str
    override_file: str
    effective_override: str | None
    source_binding: SourceBinding | None
    source_fingerprint: str
    input_fingerprint: str
    prerequisite_patches: tuple[PatchInput, ...]
    editable_patches: tuple[PatchInput, ...]
    patch_directories: tuple[PatchDirectory, ...]
    pre_patch_hooks: tuple[str, ...]
    post_patch_hooks: tuple[str, ...]
    excluded_stages: tuple[str, ...]
    warnings: tuple[str, ...]


def _run(arguments: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ResourceError(f"{arguments[0]} is required for Buildroot inspection") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or f"exit status {error.returncode}"
        raise ResourceError(f"{' '.join(arguments)}: {detail}") from error
    return result.stdout


def _make_json(output: Path, target: str, assignments: Iterable[str]) -> dict[str, Any]:
    command = [
        "make", "-s", "--no-print-directory", "-C", str(output), target,
        *assignments,
    ]
    raw = _run(command, cwd=output.parent)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ResourceError(
            f"Buildroot {target} returned invalid JSON at line {error.lineno}: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise ResourceError(f"Buildroot {target} did not return a JSON object")
    return value


def _expanded(variables: dict[str, Any], name: str) -> str:
    item = variables.get(name)
    if item is None:
        return ""
    if not isinstance(item, dict) or not isinstance(item.get("expanded"), str):
        raise ResourceError(f"Buildroot returned invalid metadata for {name}")
    return item["expanded"].strip()


def _unquote_make_path(value: str, label: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value:
        raise ResourceError(f"Buildroot did not resolve {label}")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ResourceError(f"Buildroot resolved an unsafe {label}")
    return value


def _resolve_make_path(value: str, buildroot_root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = buildroot_root / path
    # Preserve Buildroot's lexical directory identity (including version-name
    # symlinks such as linux/7.x) while still producing an absolute path.
    return Path(os.path.abspath(path))


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return str(path)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ResourceError(f"cannot read patch {path}: {error}") from error


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_revision(path: Path) -> str | None:
    try:
        return _run(
            ["git", "-C", str(path), "rev-parse", "HEAD^{commit}"], cwd=path
        ).strip()
    except ResourceError:
        return None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_patch_directory(path: Path) -> list[Path]:
    if not path.exists():
        return []
    if not path.is_dir():
        raise ResourceError(f"selected patch directory is not a directory: {path}")
    series = path / "series"
    if series.exists():
        raise ResourceError(
            f"unsupported Buildroot patch selection: series file in {path}"
        )
    nested = sorted(
        item for item in path.rglob("*.patch") if item.parent != path
    )
    if nested:
        raise ResourceError(
            f"unsupported recursive patch layout in {path}: {nested[0]}"
        )
    unsupported = sorted(
        item
        for item in path.iterdir()
        if item.is_file()
        and (
            item.name.endswith((".patch.gz", ".patch.bz2", ".patch.xz", ".patch.lzma"))
            or item.suffix in {".gz", ".bz2", ".xz", ".tar"}
        )
    )
    if unsupported:
        raise ResourceError(
            f"unsupported compressed/archive patch in {path}: {unsupported[0].name}"
        )
    return sorted(path.glob("*.patch"), key=lambda item: item.name)


def _binding(
    repositories: list[dict[str, Any]],
    root: Path,
    site: str,
    source_ref: str,
    repository_id: str | None,
    revision_id: str | None,
) -> SourceBinding | None:
    if (repository_id is None) != (revision_id is None):
        raise ResourceError("repository and revision must be specified together")

    candidates: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for repository in repositories:
        urls = {repository.get("clone_url", "")}
        urls.update(item.get("url", "") for item in repository.get("remotes", []))
        for revision in repository.get("revisions", []):
            source = revision.get("source") or {}
            exact = site in urls and source_ref in {
                source.get("ref", ""), revision.get("id", ""), revision.get("commit", "")
            }
            if exact:
                candidates.append((repository, revision, "exact-git-source"))

    if repository_id is not None:
        repository = next(
            (item for item in repositories if item["id"] == repository_id), None
        )
        if repository is None:
            raise ResourceError(f"unknown repository ID: {repository_id}")
        revision = next(
            (item for item in repository.get("revisions", []) if item["id"] == revision_id),
            None,
        )
        if revision is None:
            raise ResourceError(f"unknown revision: {repository_id}:{revision_id}")
        correspondence = "exact-git-source" if any(
            candidate[0]["id"] == repository_id and candidate[1]["id"] == revision_id
            for candidate in candidates
        ) else "declared-source-correspondence"
    elif len(candidates) == 1:
        repository, revision, correspondence = candidates[0]
    elif len(candidates) > 1:
        names = ", ".join(
            f"{item[0]['id']}:{item[1]['id']}" for item in candidates
        )
        raise ResourceError(
            f"ambiguous package source binding ({names}); specify repository and revision"
        )
    else:
        return None

    repository_path = root / repository["path"]
    if not (repository_path / "HEAD").is_file():
        raise ResourceError(
            f"repository is not populated: {repository['id']}; run the existing resource population workflow"
        )
    from . import git_resources

    commit = git_resources.git_object(
        repository_path, f"{revision['commit']}^{{commit}}"
    )
    if commit is None:
        raise ResourceError(
            f"revision is not populated: {repository['id']}:{revision['id']}; run the existing resource population workflow"
        )
    git_resources.verify_revision_objects(
        repository_path, repository["id"], revision, commit
    )
    return SourceBinding(
        repository=repository["id"],
        revision=revision["id"],
        commit=revision["commit"],
        tree=revision["tree"],
        correspondence=correspondence,
    )


def inspect_package(
    project_root: Path,
    resource_root: Path,
    repositories: list[dict[str, Any]],
    buildroot_config: str,
    package: str,
    *,
    repository: str | None = None,
    revision: str | None = None,
) -> PackageInspection:
    """Resolve one configured package using Buildroot's evaluated metadata."""
    project_root = project_root.resolve()
    if not _CONFIGURATION.fullmatch(buildroot_config):
        raise ResourceError(f"invalid Buildroot configuration name: {buildroot_config}")
    if not _PACKAGE.fullmatch(package):
        raise ResourceError(f"invalid Buildroot package name: {package}")
    output = (project_root / "output" / buildroot_config).resolve()
    if not (output / "Makefile").is_file() or not (output / ".config").is_file():
        raise ResourceError(
            f"Buildroot configuration is not available: {output}; configure it first"
        )
    buildroot_root = (project_root / "buildroot").resolve()
    prefix = package.replace("-", "_").upper()
    override_assignment = f"{prefix}_OVERRIDE_SRCDIR="
    info = _make_json(output, f"{package}-show-info", [override_assignment])
    if package not in info or not isinstance(info[package], dict):
        available = ", ".join(sorted(info)) or "none"
        raise ResourceError(
            f"unknown or unconfigured Buildroot package: {package} (returned: {available})"
        )
    package_info = info[package]

    names = [
        f"{prefix}_RAWNAME", f"{prefix}_NAME", f"{prefix}_VERSION",
        f"{prefix}_DL_VERSION", f"{prefix}_SITE", f"{prefix}_SITE_METHOD",
        f"{prefix}_SOURCE", f"{prefix}_PATCH", f"{prefix}_PKGDIR",
        f"{prefix}_DL_DIR", f"{prefix}_PRE_PATCH_HOOKS",
        f"{prefix}_POST_PATCH_HOOKS", f"{prefix}_OVERRIDE_SRCDIR",
        "BR2_PACKAGE_OVERRIDE_FILE", "PAPER_PATCH_DIRS",
        "PAPER_PATCH_HASH_DIRS",
    ]
    make_variables = _make_json(
        output,
        "show-vars",
        [
            override_assignment,
            "VARS=" + " ".join(names),
            f"PAPER_PATCH_DIRS=$(call pkg-patches-dirs,{prefix})",
            f"PAPER_PATCH_HASH_DIRS=$(call pkg-patch-hash-dirs,{prefix})",
        ],
    )
    # Query the effective override separately; the normal-source query above
    # intentionally clears it without editing the configured override file.
    effective_variables = _make_json(
        output, "show-vars", [f"VARS={prefix}_OVERRIDE_SRCDIR"]
    )

    raw_name = _expanded(make_variables, f"{prefix}_RAWNAME")
    if raw_name != package.removeprefix("host-"):
        raise ResourceError(
            f"Buildroot package metadata mismatch: requested {package}, got {raw_name!r}"
        )
    version = _expanded(make_variables, f"{prefix}_VERSION")
    download_version = _expanded(make_variables, f"{prefix}_DL_VERSION") or version
    site = _expanded(make_variables, f"{prefix}_SITE")
    site_method = _expanded(make_variables, f"{prefix}_SITE_METHOD")
    source = _expanded(make_variables, f"{prefix}_SOURCE")
    source_ref = download_version or version
    binding = _binding(
        repositories, resource_root, site, source_ref, repository, revision
    )

    recipe_directory = _resolve_make_path(
        _expanded(make_variables, f"{prefix}_PKGDIR"), buildroot_root
    )
    download_directory = _resolve_make_path(
        _expanded(make_variables, f"{prefix}_DL_DIR"), buildroot_root
    )
    override_file = Path(_unquote_make_path(
        _expanded(make_variables, "BR2_PACKAGE_OVERRIDE_FILE"),
        "BR2_PACKAGE_OVERRIDE_FILE",
    ))
    if not override_file.is_absolute():
        override_file = (output / override_file).resolve()
    effective_override = _expanded(
        effective_variables, f"{prefix}_OVERRIDE_SRCDIR"
    ) or None

    selected_paths = [
        _resolve_make_path(item, buildroot_root)
        for item in _expanded(make_variables, "PAPER_PATCH_DIRS").split()
    ]
    candidate_paths = [
        _resolve_make_path(item, buildroot_root)
        for item in _expanded(make_variables, "PAPER_PATCH_HASH_DIRS").split()
    ]
    selected_set = set(selected_paths)
    directories: list[PatchDirectory] = []
    patch_inputs: list[PatchInput] = []
    seen_directories: set[Path] = set()
    for candidate in [*candidate_paths, *selected_paths]:
        if candidate in seen_directories:
            continue
        seen_directories.add(candidate)
        selected = candidate in selected_set
        external = not _inside(candidate, buildroot_root)
        stage = "editable" if external else "prerequisite"
        directories.append(PatchDirectory(
            path=_display_path(candidate, project_root),
            resolved_path=str(candidate),
            stage=stage,
            exists=candidate.is_dir(),
            selected=selected,
        ))

    # Downloaded PKG_PATCH inputs precede every directory in Buildroot's
    # generic patch pipeline and are always prerequisites in v1.
    downloaded = _expanded(make_variables, f"{prefix}_PATCH").split()
    for value in downloaded:
        name = value.rsplit("/", 1)[-1]
        path = (download_directory / name).resolve()
        if not path.is_file():
            raise ResourceError(
                f"required downloaded patch is missing: {path}; run the existing Buildroot download workflow"
            )
        if path.suffix != ".patch":
            raise ResourceError(f"unsupported downloaded patch form: {path.name}")
        patch_inputs.append(PatchInput(
            path=_display_path(path, project_root), resolved_path=str(path),
            stage="prerequisite", directory=None, sha256=_digest(path),
        ))

    for directory in selected_paths:
        stage = "editable" if not _inside(directory, buildroot_root) else "prerequisite"
        for path in _validate_patch_directory(directory):
            patch_inputs.append(PatchInput(
                path=_display_path(path, project_root),
                resolved_path=str(path),
                stage=stage,
                directory=_display_path(directory, project_root),
                sha256=_digest(path),
            ))

    stages = [item.stage for item in patch_inputs]
    if "editable" in stages:
        first_editable = stages.index("editable")
        if "prerequisite" in stages[first_editable:]:
            raise ResourceError(
                "patch classification is interleaved; expected prerequisite patches before the editable stack"
            )
    basenames: dict[str, str] = {}
    for item in patch_inputs:
        basename = PurePosixPath(item.path).name
        previous = basenames.get(basename)
        if previous is not None:
            raise ResourceError(
                f"duplicate patch basename {basename}: {previous} and {item.path}"
            )
        basenames[basename] = item.path

    pre_hooks = tuple(_expanded(
        make_variables, f"{prefix}_PRE_PATCH_HOOKS"
    ).split())
    post_hooks = tuple(_expanded(
        make_variables, f"{prefix}_POST_PATCH_HOOKS"
    ).split())
    excluded: list[str] = []
    if pre_hooks:
        excluded.append("pre-patch hooks: " + ", ".join(pre_hooks))
    if post_hooks:
        excluded.append("post-patch hooks: " + ", ".join(post_hooks))
    warnings: list[str] = []
    if binding is None:
        warnings.append(
            "normal package source is not provably bound to a populated Paper Resources revision"
        )
    if binding is not None and binding.correspondence != "exact-git-source":
        warnings.append(
            "source binding is a declared correspondence; archive/tree equality has not been proven"
        )
    if excluded:
        warnings.append(
            "excluded preparation stages are reported but are not executed by patch workspaces"
        )

    prerequisite = tuple(item for item in patch_inputs if item.stage == "prerequisite")
    editable = tuple(item for item in patch_inputs if item.stage == "editable")
    source_fingerprint = _fingerprint({
        "package": package, "version": version, "download_version": download_version,
        "source": source, "site": site, "site_method": site_method,
        "recipe_directory": str(recipe_directory),
    })
    input_fingerprint = _fingerprint({
        "source": source_fingerprint,
        "buildroot_revision": _git_revision(buildroot_root),
        "config": hashlib.sha256((output / ".config").read_bytes()).hexdigest(),
        "patches": [(item.path, item.stage, item.sha256) for item in patch_inputs],
        "directories": [
            (item.path, item.stage, item.exists, item.selected) for item in directories
        ],
        "hooks": [pre_hooks, post_hooks],
    })
    return PackageInspection(
        buildroot_config=buildroot_config,
        project_root=str(project_root),
        output_path=str(output),
        buildroot_root=str(buildroot_root),
        buildroot_revision=_git_revision(buildroot_root),
        package=package,
        variable_prefix=prefix,
        version=version,
        download_version=download_version,
        source=source,
        site=site,
        site_method=site_method,
        recipe_directory=_display_path(recipe_directory, project_root),
        download_directory=str(download_directory),
        override_file=str(override_file),
        effective_override=effective_override,
        source_binding=binding,
        source_fingerprint=source_fingerprint,
        input_fingerprint=input_fingerprint,
        prerequisite_patches=prerequisite,
        editable_patches=editable,
        patch_directories=tuple(directories),
        pre_patch_hooks=pre_hooks,
        post_patch_hooks=post_hooks,
        excluded_stages=tuple(excluded),
        warnings=tuple(warnings),
    )
