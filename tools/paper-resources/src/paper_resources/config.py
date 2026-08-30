"""Machine-local configuration for the Paper Resources application."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


class ResourceError(RuntimeError):
    """An invalid resource manifest or application configuration."""


RESOURCE_ROOT_ENV = "PAPER_RESOURCES_DIR"
RESOURCE_DATABASE_ENV = "PAPER_RESOURCES_DB"
DEFAULT_EXTRACTOR_ENV = "PAPER_RESOURCES_DEFAULT_EXTRACTOR"


def load_environment(manifest_path: Path) -> None:
    """Load the repository-local .env without overriding the process environment."""
    load_dotenv(
        dotenv_path=manifest_path.expanduser().resolve().parent / ".env",
        override=False,
    )


def resolve_root(manifest_path: Path, explicit_root: Path | None = None) -> Path:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    configured = os.environ.get(RESOURCE_ROOT_ENV)
    if not configured:
        raise ResourceError(
            f"{RESOURCE_ROOT_ENV} is not configured; set it in .env or pass --root PATH"
        )
    root = Path(configured).expanduser()
    if not root.is_absolute():
        root = manifest_path.expanduser().resolve().parent / root
    return root.resolve()


def resolve_database(root: Path) -> Path:
    configured = os.environ.get(RESOURCE_DATABASE_ENV)
    if not configured:
        return root / "resources.db"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def resolve_default_extractor() -> str:
    return os.environ.get(DEFAULT_EXTRACTOR_ENV, "pypdf")


@dataclass(frozen=True, slots=True)
class ResourceSettings:
    manifest_path: Path
    root: Path
    database: Path
    default_extractor: str

    @classmethod
    def load(
        cls, manifest_path: Path, explicit_root: Path | None = None
    ) -> "ResourceSettings":
        manifest_path = manifest_path.expanduser().resolve()
        load_environment(manifest_path)
        root = resolve_root(manifest_path, explicit_root)
        return cls(
            manifest_path=manifest_path,
            root=root,
            database=resolve_database(root),
            default_extractor=resolve_default_extractor(),
        )
