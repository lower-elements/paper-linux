# Paper Resources

Paper Resources populates large source repositories and third-party reference
documents from a JSON manifest. It is developed inside Paper Linux but kept as
a self-contained Python project so its source, tests, packaging metadata and
dependency lock can be extracted together later.

It requires Python 3.10 or newer and Git. It uses `python-dotenv` to load the
repository-local `.env` configuration file, pypdf for its default PDF text
extractor, and the official MCP Python SDK for its agent-facing server.
Poppler's `pdftotext` command is optional.

## Running the tool

From the Paper Linux repository root, the `Justfile` provides the shortest
interface:

```sh
just resource list
just resource env
just resource path
just resource populate
just resource check
just resource revisions linux
just resource revision linux 2.6.26-rt-lab126
just resource worktrees linux 2.6.26-rt-lab126
just resource worktree linux 2.6.26-rt-lab126 default
just resource compare linux 2.6.26 2.6.26-imx35-pdk
just resource diff linux 2.6.26 2.6.26-imx35-pdk drivers/video/Kconfig
just resource index
just resource index-status
just resource search "display update waveform"
just resource page epson-s1d13521-hardware-spec-1.2 42
just resource extract --page 42 epson-s1d13521-hardware-spec-1.2
just resource-test
```

Arguments after `resource` are passed directly to the `paper-resources` CLI.
The wrapper supplies Paper Linux's root `external-resources.json` manifest.
The resource directory is read from `PAPER_RESOURCES_DIR` in `.env`; an
already-set shell variable takes precedence. Commands which access files
accept `--root PATH` as a one-off override.

## Source revisions

Manifest version 2 separates Git repositories, immutable revisions, and
worktrees. Revision names are scoped to a repository and pin both a commit and
its tree, so `linux:2.6.26-rt-lab126` identifies the same source regardless of
which remote supplied its objects. A revision can name an exact
`derived_from` revision, or an explicitly approximate `reference_base` with a
reason explaining why it is useful for comparison without claiming ancestry.

Patch files are independently fetched and checksum-pinned artifacts. A
derived revision can apply one or more declared patches to its exact parent;
Paper Resources creates deterministic commits and verifies their pinned commit
and tree identities. Worktrees are separately declared under a revision and
are always checked out detached at that revision. This keeps the revision as
the reproducible abstraction while allowing any number of convenient human-
or agent-readable checkouts.

The metadata commands `repositories`, `patches`, `revisions`, and `worktrees`
list these objects; their singular forms show one object. Repository and
revision names are separate positional arguments. Population and checking can
be restricted without ambiguity:

```sh
just resource populate --patch rt-2.6.26.8-rt16
just resource populate --repository linux
just resource populate --revision linux 2.6.26.8-rt16
just resource populate --worktree linux 2.6.26.8-rt16 default
just resource check --revision linux 2.6.26-rt-lab126
```

Population initializes an empty bare repository when necessary, fetches only
the objects needed for pinned source revisions, constructs patch-derived
revisions, and records private `refs/paper-resources/revisions/*` refs. A
revision selector prepares the revision and its exact ancestry without making
a checkout; a worktree selector creates the requested checkout, while full or
repository population creates every declared worktree. Existing compatible
object stores are reused.

### Comparing revisions

`compare` lists changed repository paths and their Git status without returning
file contents. It defaults to 200 entries so a kernel-wide comparison cannot
accidentally flood a terminal or agent context; use `--offset` and `--limit` to
page through the result, or `--path` to restrict it to one file or directory:

```sh
just resource compare linux 2.6.26 2.6.26-imx35-pdk
just resource compare --path drivers/video --limit 50 \
    linux 2.6.26-imx35-pdk 2.6.26-rt-lab126
just resource compare --offset 200 --limit 200 \
    linux 2.6.26 2.6.26-imx35-pdk
```

The result includes the total number of changed paths and status counts for the
complete filtered comparison even when only one page is returned. Rename
detection is deliberately disabled: renames appear as a deletion and addition,
which keeps summaries tree-only and avoids fetching file blobs merely to
calculate similarity.

`diff` requires exactly one normalized repository-relative file path and
returns its unified diff. It rejects directories, missing paths, absolute
paths, and parent traversal, preventing an unrestricted multi-file diff:

```sh
just resource diff linux 2.6.26-imx35-pdk 2.6.26-rt-lab126 \
    drivers/video/mxc/mxcfb_eink.c
```

Both commands accept `--json`. The MCP server exposes the same operations as
the read-only `compare_revisions` and `diff_revision_file` tools; comparison
results are likewise paginated and may be narrowed by repository path.

## Document index

`just resource index` drives each document's extractor and stores its yielded
chunks, pages, sections, and relationships in an SQLite FTS5 database. The
current PDF extractors yield one chunk per physical page. The database defaults
to `<resource-dir>/resources.db`; `PAPER_RESOURCES_DB` can override it with an
absolute path or a path relative to the resource directory.

Indexing is incremental. A document is extracted only when it is new or its
manifest checksum, selected extractor, or extractor version has changed.
Description, path, and tags are updated without extracting again. Each changed
document is reported as it is indexed, and a failed extraction leaves its
previous index intact. `just resource index-status` reports stale or missing
entries without changing the database. Optional document IDs restrict either
command.

Search terms are ANDed and safely quoted by default:

```sh
just resource search "power sequence"
just resource search --tag display "waveform mode"
just resource search --document ti-tps6518x-datasheet-g "power good"
just resource search --fts 'VCOM OR VBNEG'
```

Results include the resource ID, physical PDF page, description, snippet, and
resolved file path. `page` prints the full indexed text for a page, while
`section` prints an extracted section and its associated pages. `extract`
runs an extractor directly without opening or changing the database, which is
useful for comparing extractors. `index`, `index-status`, `search`, `page`,
`section`, and `extract` also accept `--json`.

Four PDF extractors are available:

- `pypdf` and `pypdf-layout`
- `pdftotext` and `pdftotext-layout`

Set the default with `PAPER_RESOURCES_DEFAULT_EXTRACTOR` in `.env`, and select
the best extractor for an individual document with an optional manifest field:

```json
{
  "id": "example-datasheet",
  "extractor": "pdftotext-layout"
}
```

Use `--extractor` on `index`, `index-status`, or `extract` for a one-off
override or extractor comparison. Selection precedence is the command-line
override, the document manifest setting, the environment default, and finally
the built-in `pypdf` default. An existing process environment variable takes
precedence over `.env`.

Extractors are generators. They yield opaque page and section handles before
yielding chunks which reference them. The indexer assigns sequential page,
section, and chunk ordering and translates handles to SQLite identities while
driving the generator. Reindexing one document—including removal of its old
rows and all FTS updates—takes place in one transaction, so any extraction or
validation failure restores the complete previous index.

The equivalent direct uv invocation is:

```sh
uv run --project tools/paper-resources paper-resources \
    --manifest external-resources.json list
```

The effective resource settings can be inspected with:

```sh
uv run --project tools/paper-resources paper-resources \
    --manifest external-resources.json env
```

`uv run` creates or updates the project-local virtual environment as needed.
Use `just resource-sync` (or `uv sync --project tools/paper-resources`) to do
that explicitly.

## Using venv and pip

The project uses standard Python packaging and does not require uv. An editable
installation in a conventional virtual environment also works:

```sh
python3 -m venv tools/paper-resources/.venv
tools/paper-resources/.venv/bin/python -m pip install -e tools/paper-resources
tools/paper-resources/.venv/bin/paper-resources \
    --manifest external-resources.json list
```

## Development

Run the network-free integration tests from the Paper Linux repository root:

```sh
just resource-test
```

The root `external-resources.json` belongs to Paper Linux rather than this
package. Another project can use the tool with its own version-2 manifest by
passing `--manifest PATH` before the subcommand.

## MCP server

Paper Resources also provides an agent-neutral MCP server over stdio. It uses
the same manifest, dotenv configuration, application service, extractors, and
SQLite database as the CLI:

```sh
uv run --project tools/paper-resources paper-resources-mcp \
    --manifest external-resources.json
```

An MCP host should launch that command with the Paper Linux repository as its
working directory. `just resource-mcp` is an equivalent convenience command.
The server exposes typed tools to inspect documents, repositories, revisions,
patches, and worktrees; compare revision path summaries; diff one file; search
indexed text; read physical PDF pages; inspect index status; and update
document indexes. It also exposes corresponding JSON resources. Resource
templates use the following URI forms:

```text
paper-resource://documents/{document_id}
paper-resource://documents/{document_id}/pages/{page_number}
paper-resource://documents/{document_id}/sections/{section_index}
paper-resource://repositories/{repository_id}
paper-resource://patches/{patch_id}
paper-resource://revisions/{repository_id}
paper-resource://revisions/{repository_id}/{revision_id}
paper-resource://worktrees/{repository_id}
paper-resource://worktrees/{repository_id}/{revision_id}
paper-resource://worktrees/{repository_id}/{revision_id}/{worktree_id}
```

Resource population is deliberately not exposed over MCP; continue to use
`just resource populate` when downloads or repository creation are required.

Tool names, input schemas, outputs, and configuration are independent of any
particular model or agent host. Host-specific setup should point at the same
stdio command rather than adding host behavior to the server. The
project-scoped `.codex/config.toml` registration is one client example; other
MCP hosts can register the command using their own configuration format.
