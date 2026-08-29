set positional-arguments

resource-project := "tools/paper-resources"
resource-manifest := "external-resources.json"

# Pass a command and its arguments to the external resource manager.
@resource +args:
    uv run --project {{resource-project}} paper-resources --manifest {{resource-manifest}} "$@"

# Create or update the resource manager's environment and lockfile.
@resource-sync:
    uv sync --project {{resource-project}}

# Run the resource manager's network-free integration tests.
@resource-test:
    uv run --project {{resource-project}} python -m unittest discover -s tools/paper-resources/tests -v
