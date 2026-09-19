# Paper Resources agent guide

Paper Resources is the project-specific catalog, document index, and source
archaeology service for Paper Linux. Its source tools operate on immutable Git
revisions named in `external-resources.json`, so prefer them when provenance or
comparison across kernel trees matters. A worktree is useful for ordinary
filesystem exploration, but it is not the authority for pinned source.

Keep requests bounded. Start with summaries, paths, outlines, or search hits;
read only the definitions, pages, or line ranges that answer the question. Do
not request a whole kernel diff or ingest a large source file merely to locate
one relevant region.

## Choose the first operation

- Known function, type, field, macro, or other language object:
  `search_code_tags`.
- Known file whose structure is unfamiliar: `outline_file`.
- Known source line, such as a compiler diagnostic or log-derived location:
  `read_code_at_line`. It returns the containing tag when indexed and otherwise
  falls back to nearby Git source.
- Hardware name, register, compatible string, configuration symbol, log text,
  device-tree property, or prose-like code fragment: `search_source_text`.
- Unknown path or renamed file across revisions: `find_revision_files`.
- One checkout and no need for pinned provenance: direct `rg` in its worktree is
  often the shortest route.
- Hardware question likely to span code and datasheets:
  `search_hardware_references`.
- Reference-document question: `search_documents`, followed by the relevant
  page or section resource.
- Developing a configured Buildroot patch stack: start with
  `inspect_buildroot_package`, then use the workspace operations below. Do not
  treat a mutable workspace path as an immutable revision selector.

Public source selectors are `repository`, `revision`, and repository-relative
`path`. A configured `worktree_path` may be accepted as a convenience alias,
but it resolves to the pinned Git object rather than reading dirty checkout
contents. Tag IDs are opaque, database-local handles: discover them in the
current query rather than retaining them as durable identifiers.

## Source navigation loop

1. Search for a definition with `search_code_tags`, or for literal evidence
   with `search_source_text`.
2. Use `outline_file` before reading a large file. If a result is inside a
   large type or function, use `outline_scope` or `read_enclosing_scope` to zoom
   out.
3. Batch related tag IDs into `read_tagged_code`; avoid one call per definition
   when the needed regions are already known.
4. Use `find_references` to find likely callers or uses. Its Ctags relationships
   are best-effort navigation hints, not a compiler-accurate call graph.
5. Use `read_code_at_line` when a search hit, diagnostic, blame result, or
   reference supplies a precise line.
6. Use `compare_file_outlines` or `compare_symbol_definitions` before requesting
   source diffs. Use `diff_tagged_code` for two selected tagged regions and
   `diff_revision_file` only for one repository-relative file.

`search_code_tags` can include source for a small, already precise result set.
For exploratory queries, omit source first and inspect the outline or selected
tags afterward.

## Vendor-to-mainline archaeology

Paper Linux often needs hardware facts from an old vendor kernel without
forward-porting its obsolete implementation. A productive sequence is:

1. Locate the vendor board registration, platform data, callbacks, and driver.
2. Outline the relevant files and read only the definitions which encode
   wiring, GPIO polarity, register values, timing, or power sequencing.
3. Corroborate each fact against a datasheet, a nearby Freescale or other vendor
   tree, bootloader code, device-tree bindings, or observed stock-device
   behavior.
4. Find the corresponding modern subsystem, driver, binding, or helper in the
   selected mainline revision.
5. Translate the established hardware facts into modern kernel interfaces;
   do not mechanically preserve a vendor-only API.
6. Compare focused symbols or file outlines across revisions to explain which
   behavior survived, moved, or disappeared.

Use `compare_revisions` for a paginated changed-path summary, narrowed by
`path` where possible. `find_blob_occurrences` proves that exact Git blob
content is reused at other manifest paths. `show_file_history`,
`search_revision_history`, `blame_file_lines`, and `trace_symbol_history` answer
when and why a path or symbol changed without producing an unbounded diff.

## Provenance and confidence

Report the strongest evidence actually available, and label inference as
inference. These claims are not interchangeable:

- `derived_from` records an exact, known construction relationship.
- `reference_base` is deliberately approximate; read and preserve its reason.
- An identical blob proves byte-for-byte content reuse, not original
  authorship or historical ancestry.
- A matching name or similar implementation is weaker than identical content.
- Code presence proves that an implementation exists, not that the board used
  it at runtime.
- Static configuration is not equivalent to observed device behavior.
- A datasheet describes a component's capabilities, not necessarily the
  board's wiring or populated parts.

For important conclusions, give enough coordinates for another agent or human
to reproduce them: repository, revision, path, and symbol or lines for code;
manifest document ID and physical page or named section for documents; and a
clear description of any live-device observation. If sources disagree, state
the disagreement instead of silently choosing one.

## Index coverage and missing results

Git is authoritative for revision contents; Ctags is a selective acceleration
index. A missing tag does not prove that source is absent. Use
`describe_file_index` to distinguish these cases:

- the path is absent from the pinned revision;
- revision indexing is disabled;
- include/exclude globs did not select the path;
- the blob was selected but not analyzed successfully;
- the blob is indexed but produced no matching tag.

Git-backed file, text, history, blame, blob-occurrence, and line-reading tools
remain useful for unindexed files. Fall back to `search_source_text`,
`find_revision_files`, or direct worktree inspection when Ctags coverage is not
available.

## Documents and citations

Use document search to find a small result set, then read the relevant physical
page or extracted section. Cite the manifest resource ID together with the PDF
page or section context. Treat extracted text as a navigation aid: tables,
diagrams, unusual encodings, and scanned pages may require checking the source
PDF itself.

Population is intentionally not available over MCP because it performs network
and filesystem setup. A human or authorized agent can use
`just resource populate` and `just resource check`. Index updates are exposed
separately and should be requested only when changing the shared local index is
within the task's scope.

## Package patch workspace loop

Use `inspect_buildroot_package` first. Read its ordered prerequisite/editable
patches, source binding kind, selected directories, excluded hooks, and
warnings. If automatic Git source identity is not provable, supply both a
repository and revision to `open_workspace`; archive-to-Git correspondence is
explicit and must not be described as proven archive/tree equality.

`open_workspace` creates immutable base/imported revisions and a mutable branch
under the configured resource root. Inspect and edit that returned filesystem
path with ordinary Git. Git is authoritative for HEAD, branch, index, worktree,
notes, and interrupted operations. Existing source-reading tools address the
named immutable local revisions, not uncommitted workspace contents.

Before export:

1. Use `get_workspace_status` and finish any rebase, merge, or other Git
   operation. Keep the worktree and index clean.
2. Preserve imported destination notes through normal amend/rebase. For a new
   or split commit, call `annotate_workspace_commit` with an exact path inside
   a selected writable patch directory when numbered anchors cannot infer it.
3. Call `export_workspace` with `dry_run=true`. Resolve missing/conflicting
   notes, layer ambiguity, numbering collisions, stale inputs, external file
   edits, empty/merge commits, or missing patch headers before publishing.
4. Publish with `dry_run=false`. Manually review and remove any reported
   obsolete patch files; they remain in the Buildroot stack until removed.

`attach_workspace` is never an implicit side effect. It makes the mutable
checkout the configured package source and bypasses normal extraction,
patching, and reported hooks. Use it only for an explicitly requested test,
then `detach_workspace`. Follow the returned clean/rebuild advice because
Buildroot's `rsync -au` can retain deleted files. `close_workspace` without
force protects dirty or unexported work and changed patch outputs. Forced close
may discard workspace-owned work but cannot override a conflicting attachment
or remove an unrelated worktree.
