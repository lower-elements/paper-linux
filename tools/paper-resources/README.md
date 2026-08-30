# Paper Resources

Paper Resources populates large source repositories and third-party reference
documents from a JSON manifest. It is developed inside Paper Linux but kept as
a self-contained Python project so its source, tests, packaging metadata and
dependency lock can be extracted together later.

It requires Python 3.10 or newer and Git. It uses `python-dotenv` to load the
repository-local `.env` configuration file and pypdf for its default PDF text
extractor. Poppler's `pdftotext` command is optional.

## Running the tool

From the Paper Linux repository root, the `Justfile` provides the shortest
interface:

```sh
just resource list
just resource env
just resource path
just resource populate
just resource check
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

## Document index

`just resource index` extracts each manifest PDF into one chunk per physical
page and stores it in an SQLite FTS5 database. The database defaults to
`<resource-dir>/resources.db`; `PAPER_RESOURCES_DB` can override it with an
absolute path or a path relative to the resource directory.

Indexing is incremental. A document is extracted only when it is new or its
manifest checksum, extractor, or extractor version has changed. Description,
path, and tags are updated without extracting again. Each changed document is
reported as it is indexed, and a failed extraction leaves its previous index
intact. `just resource index-status` reports stale or missing entries without
changing the database. Optional document IDs restrict either command.

Search terms are ANDed and safely quoted by default:

```sh
just resource search "power sequence"
just resource search --tag display "waveform mode"
just resource search --document ti-tps6518x-datasheet-g "power good"
just resource search --fts 'VCOM OR VBNEG'
```

Results include the resource ID, physical PDF page, description, snippet, and
resolved file path. `page` prints the full indexed text for a page. `extract`
runs an extractor directly without opening or changing the database, which is
useful for comparing backends. `index`, `index-status`, `search`, `page`, and
`extract` also accept `--json`.

Four PDF extraction modes are available:

- `pypdf` and `pypdf-layout`
- `pdftotext` and `pdftotext-layout`

Select one with `--pdf-backend` on `index`, `index-status`, or `extract`, or
set `PAPER_RESOURCES_PDF_BACKEND` in `.env`. An existing process environment
variable takes precedence over `.env`; an explicit command-line option takes
precedence over both. The default is `pypdf`.

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
package. Another project can use the tool with its own version-1 manifest by
passing `--manifest PATH` before the subcommand.
