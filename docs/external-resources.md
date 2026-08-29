# External resource catalog

Paper Linux records large source repositories and third-party reference
documents in `external-resources.json` instead of assuming a particular
developer's directory layout. The manifest contains descriptions and topic
tags as well as enough information to fetch public resources. Documents are
accepted only when their SHA-256 checksum matches the catalog.

The tool is a self-contained Python project under
[`tools/paper-resources`](../tools/paper-resources). It needs Python 3.10 or
newer and Git. Set `PAPER_RESOURCES_DIR` in the repository-local, gitignored
`.env` file (see `.env.example`), then discover and populate the catalog:

```sh
just resource list
just resource env
just resource path
just resource populate
just resource check
```

The shell environment takes precedence over `.env`, so a temporary directory
can be selected with `PAPER_RESOURCES_DIR=/tmp/resources just resource check`.
An explicit `--root PATH` option is also available for `populate` and `check`.

The [tool README](../tools/paper-resources/README.md) documents direct `uv`
usage and a standard `venv`/`pip` alternative.

An individual document, repository, or worktree ID may be supplied after the
command. For example:

```sh
just resource populate \
    linux-upstream-2.6.26 epson-s1d13521-hardware-spec-1.2
```

Repository resources are stored as bare Git repositories under `git/`.
Checkouts under `worktrees/` are linked worktrees sharing that object store.
Running `populate` again is safe: matching documents and existing worktrees
are retained, remotes are reconciled with the manifest, and remotes are
fetched. The tool never resets or updates an existing worktree's checked-out
commit, so local investigation branches and modifications are preserved.

Some historical documents have no stable, authorised download URL. They are
still catalogued with a checksum and source hint. `populate` reports these as
`manual`; after the named file is obtained independently and placed at its
manifest path, `check` verifies it normally.

## Manifest format

The top-level `version` is currently `1`. A document has a stable ID, relative
destination `path`, SHA-256 digest, and either a direct `url` or a
`source_page` hint. A repository has a clone URL, additional named remotes with
optional narrow fetch refspecs, and worktrees consisting of an ID, relative
path, and Git ref. Paths must be relative and cannot contain `..`, keeping
every write inside the selected resource directory.

Add a new entry by editing the JSON manifest, choosing a descriptive stable ID
and narrow topic tags, and running the tests:

```sh
just resource-test
just resource list
```

The first version intentionally does not build synthetic Git histories or
unpack source archives into commits. Amazon's historyless Kindle GPL release
is fetched from a public Git mirror. The locally reconstructed comparison
branches are catalogued as manual worktrees: once their named refs have been
imported, `populate` creates them, but constructing those refs remains future
recipe work. Public Amazon/Lab126, upstream, stable, and NXP states are
reproducible now.
Documentation refers to reproducible entries by manifest ID and to those
historical states by their Git ref, never by a developer-specific filesystem
path.
