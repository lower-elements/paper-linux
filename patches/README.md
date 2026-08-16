# Patch layers

Paper Linux composes Buildroot patch directories explicitly in each
defconfig. A target must opt into every layer it needs through
`BR2_GLOBAL_PATCH_DIR`; architecture or board names do not implicitly select
patches.

Layers are ordered from general to specific:

1. `patches/common` contains fixes required by every Paper Linux target.
2. `patches/compat/<profile>` adapts userspace to a kernel ABI generation.
3. `board/<vendor>/<device>/common/patches` contains device-wide fixes.
4. `board/<vendor>/<device>/<variant>/patches` contains kernel-line or
   boot-mode-specific fixes.

Each layer uses Buildroot package names as subdirectories. For example, a
kernel patch belongs under `linux/`, while a BusyBox patch belongs under
`busybox/`. An exact package-version directory may be used when required,
such as `linux/7.0.11/`. Buildroot chooses that versioned directory instead
of the unversioned package directory in the same layer; it does not combine
the two.

Keep patches next to an external package under `package/<name>/` only when
the patch is intrinsic to that package and must apply whenever the package is
selected. Target-dependent patches belong in a layer.

Use zero-padded numeric filenames, alphabetical application order, and an
email-style header explaining what the patch does and why. Include upstream
status and a `Signed-off-by` line. Do not add new `series` files.
