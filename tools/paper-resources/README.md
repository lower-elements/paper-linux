# Paper Resources

Paper Resources populates large source repositories and third-party reference
documents from a JSON manifest. It is developed inside Paper Linux but kept as
a self-contained Python project so its source, tests, packaging metadata and
dependency lock can be extracted together later.

It requires Python 3.10 or newer and Git. It uses `python-dotenv` to load the
repository-local `.env` configuration file.

## Running the tool

From the Paper Linux repository root, the `Justfile` provides the shortest
interface:

```sh
just resource list
just resource env
just resource path
just resource populate
just resource check
just resource-test
```

Arguments after `resource` are passed directly to the `paper-resources` CLI.
The wrapper supplies Paper Linux's root `external-resources.json` manifest.
The resource directory is read from `PAPER_RESOURCES_DIR` in `.env`; an
already-set shell variable takes precedence. Use `--root PATH` on `populate`
or `check` for a one-off override.

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
