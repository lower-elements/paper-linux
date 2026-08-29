# SPDX-License-Identifier: GPL-2.0

set positional-arguments

project-root := justfile_directory()
download-dir := env_var_or_default("BR2_DL_DIR", project-root / "dl")
resource-project := "tools/paper-resources"
resource-manifest := "external-resources.json"

[private]
@default:
    just --list

# Configure output/<configuration> from configs/<configuration>_defconfig.
[group: 'build']
@configure configuration:
    case "$1" in *[!A-Za-z0-9_]*) echo "error: invalid configuration name: $1" >&2; exit 2;; esac
    test -f "configs/$1_defconfig" || { echo "error: unknown configuration: $1" >&2; exit 2; }
    make -C {{quote(project-root / "buildroot")}} BR2_EXTERNAL={{quote(project-root)}} O={{quote(project-root / "output")}}/"$1" "${1}_defconfig"
    buildroot/utils/config --file "output/$1/.config" --set-str DL_DIR {{quote(download-dir)}}
    make -C "output/$1" olddefconfig

# Build a configuration, optionally passing targets to its generated Makefile.
[group: 'build']
@build configuration *targets:
    case "$1" in *[!A-Za-z0-9_]*) echo "error: invalid configuration name: $1" >&2; exit 2;; esac; test -f "output/$1/Makefile" || { echo "error: configure $1 first with: just configure $1" >&2; exit 2; }; configuration="$1"; shift; make -C "output/$configuration" "$@"

# Pass a command and its arguments to the external resource manager.
[group: 'resources']
@resource +args:
    uv run --project {{resource-project}} paper-resources --manifest {{resource-manifest}} "$@"

# Create or update the resource manager's environment and lockfile.
[group: 'resources']
@resource-sync:
    uv sync --project {{resource-project}}

# Run the resource manager's network-free integration tests.
[group: 'resources']
@resource-test:
    uv run --project {{resource-project}} python -m unittest discover -s tools/paper-resources/tests -v
