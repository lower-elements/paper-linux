# Paper Resources

Paper Resources populates large source repositories and third-party reference
documents from a JSON manifest. It is developed inside Paper Linux but kept as
a self-contained Python project so its source, tests, packaging metadata and
dependency lock can be extracted together later.

It requires Python 3.10 or newer and Git. It currently has no third-party
runtime dependencies.

## Running the tool

From the Paper Linux repository root, the `Justfile` provides the shortest
interface:

```sh
just resource list
just resource populate /path/to/paper-resources
just resource check /path/to/paper-resources
just resource-test
```

Arguments after `resource` are passed directly to the `paper-resources` CLI.
The wrapper supplies Paper Linux's root `external-resources.json` manifest.

The equivalent direct uv invocation is:

```sh
uv run --project tools/paper-resources paper-resources \
    --manifest external-resources.json list
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
