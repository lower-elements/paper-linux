"""Persistent Universal Ctags protocol and lazy parser catalog discovery."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import PurePosixPath
import queue
import sqlite3
import subprocess
import tempfile
import threading
from typing import Any, Iterator

from .config import ResourceError


CTAGS_PIPELINE_VERSION = 1
CTAGS_RESPONSE_QUEUE_SIZE = 256
CTAGS_OUTPUT_OPTIONS = (
    "--options=NONE",
    "--fields=*",
    "--fields-all=*",
    "--extras=+{pseudo}{qualified}{reference}",
    "--extras=-{inputFile}",
    "--pseudo-tags={TAG_OUTPUT_VERSION}{TAG_PARSER_VERSION}"
    "{TAG_KIND_DESCRIPTION}{TAG_ROLE_DESCRIPTION}",
)


class CtagsError(ResourceError):
    """Universal Ctags could not produce a trustworthy analysis stream."""


@dataclass(frozen=True, slots=True)
class CtagsProfile:
    id: int
    program_name: str
    program_version: str
    output_version: str
    json_output_version: str
    configuration_sha256: bytes


@dataclass(frozen=True, slots=True)
class CtagsParser:
    id: int
    profile_id: int
    language: str
    version: str | None


@dataclass(frozen=True, slots=True)
class CtagsKind:
    id: int
    parser_id: int
    language: str
    name: str
    letter: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class CtagsRole:
    id: int
    kind_id: int
    language: str
    kind: str
    name: str
    description: str | None


@dataclass(frozen=True, slots=True)
class CtagsTag:
    parser_id: int
    kind_id: int
    name: str
    language: str
    kind: str
    roles: tuple[str, ...]
    role_ids: tuple[int, ...]
    extras: tuple[str, ...]
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CtagsCompleted:
    profile_id: int
    input_parser_id: int | None
    tags: int


CtagsEvent = (
    CtagsProfile | CtagsParser | CtagsKind | CtagsRole | CtagsTag | CtagsCompleted
)


def configuration_sha256() -> bytes:
    payload = json.dumps(
        {
            "pipeline_version": CTAGS_PIPELINE_VERSION,
            "output_options": CTAGS_OUTPUT_OPTIONS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _run_text(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise CtagsError("Universal Ctags is required for source indexing") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"exit status {error.returncode}"
        raise CtagsError(f"{' '.join(arguments)}: {detail}") from error
    return result.stdout


def discover_features(executable: str) -> set[str]:
    output = _run_text([executable, "--options=NONE", "--list-features"])
    return {
        line.split(None, 1)[0]
        for line in output.splitlines()
        if line and not line.startswith("#")
    }


def discover_language_maps(executable: str) -> list[tuple[str, tuple[str, ...]]]:
    output = _run_text([executable, "--options=NONE", "--list-maps"])
    mappings: list[tuple[str, tuple[str, ...]]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) > 1:
            mappings.append((fields[0], tuple(fields[1:])))
    return mappings


def _required_text(record: dict[str, Any], field: str, context: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise CtagsError(f"Ctags emitted {context} without a valid {field}")
    return value


def _comma_values(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise CtagsError(f"Ctags emitted a non-text {field} field")
    return tuple(item for item in value.split(",") if item)


class CtagsSession:
    """A synchronous, single-consumer Universal Ctags interactive session."""

    def __init__(self, connection: sqlite3.Connection, executable: str = "ctags"):
        self.connection = connection
        self.executable = executable
        features = discover_features(executable)
        missing = {"interactive", "json"} - features
        if missing:
            raise CtagsError(
                "Universal Ctags lacks required feature(s): "
                + ", ".join(sorted(missing))
            )
        interactive = (
            "--_interactive=sandbox" if "sandbox" in features else "--_interactive"
        )
        self._errors = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                [executable, *CTAGS_OUTPUT_OPTIONS, interactive],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._errors,
            )
        except FileNotFoundError as error:
            self._errors.close()
            raise CtagsError(
                "Universal Ctags is required for source indexing"
            ) from error
        if self._process.stdin is None or self._process.stdout is None:
            self._process.kill()
            self._process.wait()
            self._errors.close()
            raise CtagsError("cannot open Universal Ctags protocol streams")
        self._records: queue.Queue[bytes | None] = queue.Queue(
            maxsize=CTAGS_RESPONSE_QUEUE_SIZE
        )
        self._reader_stopping = threading.Event()

        def read_output() -> None:
            while line := self._process.stdout.readline():
                while not self._reader_stopping.is_set():
                    try:
                        self._records.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                # During shutdown, discard records but continue draining the
                # OS pipe so ctags can observe EOF and terminate.
            if not self._reader_stopping.is_set():
                self._records.put(None)

        self._reader = threading.Thread(
            target=read_output, name="ctags-output", daemon=True
        )
        self._reader.start()
        self._request_writer: threading.Thread | None = None
        self._request_done: threading.Event | None = None
        self._request_errors: list[BaseException] | None = None
        self._closed = False
        try:
            startup = self._read_record()
            if startup.get("_type") != "program":
                raise CtagsError(
                    "Universal Ctags did not identify its interactive process"
                )
            self.program_name = _required_text(startup, "name", "program record")
            self.program_version = _required_text(
                startup, "version", "program record"
            )
            if self.program_name != "Universal Ctags":
                raise CtagsError(
                    f"unsupported Ctags implementation: {self.program_name}"
                )
        except BaseException:
            try:
                self.close()
            except CtagsError:
                pass
            raise
        self._configuration_sha256 = configuration_sha256()
        self._language_maps = discover_language_maps(executable)
        self._profile: CtagsProfile | None = None
        self._parsers: dict[str, CtagsParser] = {}
        self._kinds: dict[tuple[int, str], CtagsKind] = {}
        self._roles: dict[tuple[int, str], CtagsRole] = {}

    def expected_language(self, filename: str) -> str | None:
        basename = PurePosixPath(filename).name
        for language, patterns in self._language_maps:
            if any(fnmatchcase(basename, pattern) for pattern in patterns):
                return language
        return None

    def ensure_profile(self) -> CtagsProfile:
        """Resolve and persist the session profile without analysing a real blob."""
        if self._profile is not None:
            return self._profile
        profile: CtagsProfile | None = None
        for event in self.analyze("paper-resources.unknown", b""):
            if isinstance(event, CtagsProfile):
                profile = event
        if profile is None:
            raise CtagsError("Universal Ctags did not emit an output profile")
        return profile

    def __enter__(self) -> "CtagsSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _stderr(self) -> str:
        self._errors.flush()
        self._errors.seek(0)
        return self._errors.read().decode("utf-8", errors="replace").strip()

    def _read_record(self) -> dict[str, Any]:
        line = self._records.get()
        if line is None:
            returncode = self._process.wait()
            detail = self._stderr() or f"exit status {returncode}"
            raise CtagsError(f"Universal Ctags stopped unexpectedly: {detail}")
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CtagsError("Universal Ctags emitted invalid JSON") from error
        if not isinstance(record, dict):
            raise CtagsError("Universal Ctags emitted a non-object JSON record")
        return record

    def _drain_request(self) -> None:
        """Consume the rest of a cancelled request so the session stays aligned."""
        while True:
            record = self._read_record()
            if record.get("_type") == "completed":
                self._finish_request()
                return

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._reader_stopping.set()
        try:
            self._process.stdin.close()
            try:
                returncode = self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                returncode = self._process.wait()
            if returncode:
                detail = self._stderr() or f"exit status {returncode}"
                raise CtagsError(f"Universal Ctags exited unsuccessfully: {detail}")
        finally:
            self._reader.join(timeout=5)
            self._process.stdout.close()
            self._errors.close()

    def _submit_request(self, command: bytes, content: bytes) -> None:
        if self._request_writer is not None:
            raise CtagsError("another Universal Ctags request is still active")
        done = threading.Event()
        errors: list[BaseException] = []

        def write_request() -> None:
            try:
                self._process.stdin.write(command + b"\n")
                self._process.stdin.write(content)
                self._process.stdin.flush()
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        writer = threading.Thread(target=write_request, name="ctags-input")
        self._request_writer = writer
        self._request_done = done
        self._request_errors = errors
        writer.start()

    def _finish_request(self) -> None:
        writer = self._request_writer
        done = self._request_done
        errors = self._request_errors
        if writer is None or done is None or errors is None:
            raise CtagsError("Universal Ctags request state is missing")

        # A completed response normally means ctags consumed all input. Keep
        # servicing stdout until the writer confirms that fact rather than
        # joining it while ctags may still be blocked on its output pipe.
        while not done.is_set():
            try:
                line = self._records.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            raise CtagsError(
                "Universal Ctags emitted output after completing a request"
            )

        writer.join()
        self._request_writer = None
        self._request_done = None
        self._request_errors = None
        if errors:
            error = errors[0]
            detail = self._stderr() or "broken protocol pipe"
            raise CtagsError(
                f"cannot submit blob to Universal Ctags: {detail}"
            ) from error

    def _clear_catalog_cache(self) -> None:
        self._profile = None
        self._parsers.clear()
        self._kinds.clear()
        self._roles.clear()

    def _ensure_profile(
        self, output_version: str, json_output_version: str
    ) -> CtagsProfile:
        expected = (
            self.program_name,
            self.program_version,
            output_version,
            json_output_version,
            self._configuration_sha256,
        )
        if self._profile is not None:
            actual = (
                self._profile.program_name,
                self._profile.program_version,
                self._profile.output_version,
                self._profile.json_output_version,
                self._profile.configuration_sha256,
            )
            if actual != expected:
                raise CtagsError(
                    "Universal Ctags output profile changed during a session"
                )
            return self._profile
        row = self.connection.execute(
            """
            INSERT INTO ctags_profiles(
                program_name, program_version, output_version,
                json_output_version, configuration_sha256
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(
                program_name, program_version, output_version,
                json_output_version, configuration_sha256
            ) DO UPDATE SET program_name = excluded.program_name
            RETURNING id
            """,
            expected,
        ).fetchone()
        if row is None:
            raise CtagsError("SQLite did not return a Ctags profile ID")
        self._profile = CtagsProfile(int(row[0]), *expected)
        return self._profile

    def _ensure_parser(
        self, profile: CtagsProfile, language: str, version: str | None
    ) -> CtagsParser:
        cached = self._parsers.get(language)
        if cached is not None:
            if cached.profile_id != profile.id or (
                version is not None
                and cached.version is not None
                and cached.version != version
            ):
                raise CtagsError(f"conflicting Ctags parser metadata for {language}")
            if version is None or cached.version is not None:
                return cached
        row = self.connection.execute(
            """
            INSERT INTO ctags_parsers(profile_id, language, parser_version)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_id, language)
            DO UPDATE SET parser_version = coalesce(
                ctags_parsers.parser_version,
                excluded.parser_version
            )
            RETURNING id, parser_version
            """,
            (profile.id, language, version),
        ).fetchone()
        if row is None or (
            version is not None and row[1] is not None and row[1] != version
        ):
            raise CtagsError(f"conflicting Ctags parser metadata for {language}")
        parser = CtagsParser(int(row[0]), profile.id, language, row[1])
        self._parsers[language] = parser
        return parser

    def _ensure_kind(
        self,
        parser: CtagsParser,
        name: str,
        letter: str | None,
        description: str | None,
    ) -> CtagsKind:
        key = (parser.id, name)
        cached = self._kinds.get(key)
        if cached is not None:
            if (
                letter is not None
                and cached.letter is not None
                and cached.letter != letter
            ) or (
                description is not None
                and cached.description is not None
                and cached.description != description
            ):
                raise CtagsError(
                    f"conflicting Ctags kind metadata for {parser.language}:{name}"
                )
            if (letter is None or cached.letter is not None) and (
                description is None or cached.description is not None
            ):
                return cached
        row = self.connection.execute(
            """
            INSERT INTO ctags_kinds(parser_id, name, letter, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(parser_id, name)
            DO UPDATE SET
                letter = coalesce(ctags_kinds.letter, excluded.letter),
                description = coalesce(
                    ctags_kinds.description,
                    excluded.description
                )
            RETURNING id, letter, description
            """,
            (parser.id, name, letter, description),
        ).fetchone()
        if row is None or (
            letter is not None and row[1] is not None and row[1] != letter
        ) or (
            description is not None
            and row[2] is not None
            and row[2] != description
        ):
            raise CtagsError(
                f"conflicting Ctags kind metadata for {parser.language}:{name}"
            )
        kind = CtagsKind(
            int(row[0]), parser.id, parser.language, name, row[1], row[2]
        )
        self._kinds[key] = kind
        return kind

    def _ensure_role(
        self,
        kind: CtagsKind,
        name: str,
        description: str | None,
    ) -> CtagsRole:
        key = (kind.id, name)
        cached = self._roles.get(key)
        if cached is not None:
            if (
                description is not None
                and cached.description is not None
                and cached.description != description
            ):
                raise CtagsError(
                    f"conflicting Ctags role metadata for "
                    f"{kind.language}:{kind.name}:{name}"
                )
            if description is None or cached.description is not None:
                return cached
        row = self.connection.execute(
            """
            INSERT INTO ctags_roles(kind_id, name, description)
            VALUES (?, ?, ?)
            ON CONFLICT(kind_id, name)
            DO UPDATE SET description = coalesce(
                ctags_roles.description,
                excluded.description
            )
            RETURNING id, description
            """,
            (kind.id, name, description),
        ).fetchone()
        if row is None or (
            description is not None
            and row[1] is not None
            and row[1] != description
        ):
            raise CtagsError(
                f"conflicting Ctags role metadata for "
                f"{kind.language}:{kind.name}:{name}"
            )
        role = CtagsRole(
            int(row[0]), kind.id, kind.language, kind.name, name, row[1]
        )
        self._roles[key] = role
        return role

    def analyze(self, filename: str, content: bytes) -> Iterator[CtagsEvent]:
        """Analyse one in-memory blob and yield catalog and tag events."""
        if self._closed:
            raise CtagsError("Universal Ctags session is closed")
        if not filename or "\0" in filename:
            raise CtagsError("Ctags input filename must not be empty or contain NUL")
        if not isinstance(content, bytes):
            raise CtagsError("Ctags input content must be bytes")
        last_event: CtagsEvent | None = None
        try:
            with self.connection:
                for event in self._analyze(filename, content):
                    last_event = event
                    yield event
        except GeneratorExit:
            try:
                if not isinstance(last_event, CtagsCompleted):
                    self._drain_request()
            finally:
                self._clear_catalog_cache()
            raise
        except sqlite3.Error as error:
            self._clear_catalog_cache()
            raise CtagsError(f"cannot update the Ctags catalog: {error}") from error
        except BaseException:
            self._clear_catalog_cache()
            raise

    def _analyze(self, filename: str, content: bytes) -> Iterator[CtagsEvent]:
        command = json.dumps(
            {"command": "generate-tags", "filename": filename, "size": len(content)},
            separators=(",", ":"),
        ).encode("utf-8")
        self._submit_request(command, content)

        output_version: str | None = None
        json_output_version: str | None = None
        profile: CtagsProfile | None = None
        profile_emitted = False
        request_parsers: dict[str, CtagsParser] = {}
        request_kinds: dict[tuple[str, str], CtagsKind] = {}
        request_roles: dict[tuple[str, str, str], CtagsRole] = {}
        pending_kinds: dict[str, list[tuple[str, str | None, str | None]]] = {}
        pending_roles: dict[str, list[tuple[str, str, str | None]]] = {}
        input_parser_id: int | None = None
        tag_count = 0
        request_error: str | None = None

        def require_profile() -> CtagsProfile:
            nonlocal profile
            if output_version is None or json_output_version is None:
                raise CtagsError(
                    "Universal Ctags emitted parser data before output versions"
                )
            profile = self._ensure_profile(output_version, json_output_version)
            return profile

        def resolve_pending(language: str) -> list[CtagsEvent]:
            events: list[CtagsEvent] = []
            parser = request_parsers[language]
            for name, letter, description in pending_kinds.pop(language, []):
                kind = self._ensure_kind(parser, name, letter, description)
                request_kinds[(language, name)] = kind
                events.append(kind)
            for kind_name, name, description in pending_roles.pop(language, []):
                kind = request_kinds.get((language, kind_name))
                if kind is None:
                    kind = self._ensure_kind(parser, kind_name, None, None)
                    request_kinds[(language, kind_name)] = kind
                    events.append(kind)
                role = self._ensure_role(kind, name, description)
                request_roles[(language, kind_name, name)] = role
                events.append(role)
            return events

        while True:
            record = self._read_record()
            record_type = record.get("_type")
            if record_type == "error":
                message = record.get("message")
                request_error = (
                    f"Universal Ctags rejected {filename}: "
                    f"{message if isinstance(message, str) else 'unknown error'}"
                )
                continue
            if request_error is not None and record_type != "completed":
                continue
            if record_type == "ptag":
                name = _required_text(record, "name", "pseudo-tag")
                if name == "JSON_OUTPUT_VERSION":
                    json_output_version = _required_text(
                        record, "path", "JSON output version pseudo-tag"
                    )
                elif name == "TAG_OUTPUT_VERSION":
                    output_version = _required_text(
                        record, "path", "output version pseudo-tag"
                    )
                elif name == "TAG_KIND_DESCRIPTION":
                    language = _required_text(record, "parserName", "kind pseudo-tag")
                    specification = _required_text(record, "path", "kind pseudo-tag")
                    try:
                        letter, kind_name = specification.split(",", 1)
                    except ValueError as error:
                        raise CtagsError(
                            f"invalid Ctags kind description: {specification}"
                        ) from error
                    description = record.get("pattern")
                    if description is not None and not isinstance(description, str):
                        raise CtagsError("Ctags emitted a non-text kind description")
                    pending_kinds.setdefault(language, []).append(
                        (kind_name, letter or None, description)
                    )
                    if language in request_parsers:
                        yield from resolve_pending(language)
                elif name == "TAG_ROLE_DESCRIPTION":
                    language = _required_text(record, "parserName", "role pseudo-tag")
                    kind_name = _required_text(record, "kindName", "role pseudo-tag")
                    role_name = _required_text(record, "path", "role pseudo-tag")
                    description = record.get("pattern")
                    if description is not None and not isinstance(description, str):
                        raise CtagsError("Ctags emitted a non-text role description")
                    pending_roles.setdefault(language, []).append(
                        (kind_name, role_name, description)
                    )
                    if language in request_parsers:
                        yield from resolve_pending(language)
                elif name == "TAG_PARSER_VERSION":
                    language = _required_text(record, "parserName", "parser pseudo-tag")
                    version = _required_text(record, "path", "parser pseudo-tag")
                    current_profile = require_profile()
                    if not profile_emitted:
                        profile_emitted = True
                        yield current_profile
                    parser = self._ensure_parser(current_profile, language, version)
                    request_parsers[language] = parser
                    if input_parser_id is None:
                        input_parser_id = parser.id
                    yield parser
                    yield from resolve_pending(language)
                continue
            if record_type == "tag":
                language = _required_text(record, "language", "tag")
                kind_name = _required_text(record, "kind", "tag")
                parser = request_parsers.get(language)
                kind = request_kinds.get((language, kind_name))
                current_profile = require_profile()
                if not profile_emitted:
                    profile_emitted = True
                    yield current_profile
                if parser is None:
                    parser = self._ensure_parser(current_profile, language, None)
                    request_parsers[language] = parser
                    yield parser
                if kind is None:
                    kind = self._ensure_kind(parser, kind_name, None, None)
                    request_kinds[(language, kind_name)] = kind
                    yield kind
                roles = _comma_values(record.get("roles"), "roles")
                extras = _comma_values(record.get("extras"), "extras")
                role_ids: list[int] = []
                for role_name in roles:
                    if role_name == "def":
                        continue
                    role = request_roles.get((language, kind_name, role_name))
                    if role is None:
                        role = self._ensure_role(kind, role_name, None)
                        request_roles[(language, kind_name, role_name)] = role
                        yield role
                    role_ids.append(role.id)
                fields = {
                    key: value
                    for key, value in record.items()
                    if key not in {
                        "_type", "name", "path", "language", "kind", "roles", "extras"
                    }
                }
                tag_count += 1
                yield CtagsTag(
                    parser_id=parser.id,
                    kind_id=kind.id,
                    name=_required_text(record, "name", "tag"),
                    language=language,
                    kind=kind_name,
                    roles=roles,
                    role_ids=tuple(role_ids),
                    extras=extras,
                    fields=fields,
                )
                continue
            if record_type == "completed":
                if record.get("command") != "generate-tags":
                    raise CtagsError("Universal Ctags completed an unexpected command")
                self._finish_request()
                if request_error is not None:
                    raise CtagsError(request_error)
                current_profile = require_profile()
                if not profile_emitted:
                    yield current_profile
                if pending_kinds or pending_roles:
                    raise CtagsError(
                        "Universal Ctags left unresolved catalog pseudo-tags"
                    )
                yield CtagsCompleted(current_profile.id, input_parser_id, tag_count)
                return
            raise CtagsError(
                f"Universal Ctags emitted unknown record type: {record_type!r}"
            )
