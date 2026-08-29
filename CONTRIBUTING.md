# Contributing to Paper Linux

Thanks for helping turn old e‑ink readers into pocketable Linux PDAs. This guide collects the nuts-and-bolts details contributors tend to need. Paper Linux supports both upstream kernels and old vendor kernels where hardware still requires them. Keep compatibility work scoped to the configuration and patch layer that needs it rather than imposing one kernel's constraints on every target.

## How to contribute

Start with `README.md` to get the big picture. If you’re unsure where to jump in, open an issue or discussion and we’ll point you at something useful. We favor upstream-first fixes, small composable changes, and minimal forks—only fork when the hardware forces us. Code used by a vendor-kernel target must respect that kernel's APIs, while mainline-only code need not carry those restrictions. Real hardware testing matters—share what you tried, what worked, and what broke. Prefer lightweight tooling—BusyBox for core utilities, musl for libc, shared libs where possible.

## Technical details

Our baseline hardware looks like early-2010s e‑ink readers: an ARM11 around 532 MHz, roughly 256 MB of RAM, no GPU, and slow flash. The established Kindle 3 chroot target uses the 2.6.26 RT Lab126 kernel, while a separate target is reserved for mainline enablement. The old-kernel target patches headers sparingly and keeps the syscall ABI intact.

Builds are driven by the Buildroot external tree in this repo. Toolchains are musl-based and prefer shared libraries. BusyBox supplies init/mdev and most core utilities. The vendor configuration uses static device nodes and legacy WEXT networking to match its old kernel; mainline configurations can use modern kernel facilities without inheriting those choices. Power and resource posture is strict: minimize redraws, keep radios off by default, and pause or evict background work when resources are tight. The usable system today is a chroot/alt-root alongside the vendor bootloader and kernel, while the mainline configuration is an enablement scaffold.

## Repository layout

Paper Linux is organized as a Buildroot external tree. That means upstream Buildroot does the heavy lifting while this repo supplies configs, patches, and packages. If you’re new to the pattern, the [Buildroot manual](https://buildroot.org/docs.html) has a good overview of external trees and how they plug into a build.

Key paths to know:

- `Justfile` — command runner for configuring and building output trees and for
  project tools; Buildroot outputs are under `output/`.
- `configs/kindle3_vendor_2_6_26_defconfig` — established Kindle 3 vendor-kernel userspace.
- `configs/kindle3_mainline_defconfig` — minimal mainline-development userspace scaffold.
- `board/paper/common/busybox.config` — BusyBox feature set shared by targets.
- `patches/common/` — patches selected by every target.
- `patches/compat/linux-2.6.26/` — old-kernel userspace compatibility patches.
- `board/amazon/kindle3/` — common, vendor-kernel, and mainline board variants.
- `package/` — Paper Linux package definitions and their intrinsic patches.
- `dl/` — download cache (created after the first build).
- `buildroot/` — Buildroot sources (kept in-tree for reproducibility).
- `output/` — per-defconfig build output (created after building).

## Getting set up

1) Install standard Buildroot prerequisites for your host distro and `just`.
2) Fetch submodules: `git submodule update --init --recursive`
3) Configure an output tree, for example: `just configure kindle3_vendor_2_6_26`
4) Build it: `just build kindle3_vendor_2_6_26`

Artifacts land under the matching `output/<configuration>/` directory; downloads cache in `dl/`.

Every defconfig lists its patch layers explicitly in `BR2_GLOBAL_PATCH_DIR`.
See `patches/README.md` before adding or moving a patch.

## Contribution workflow

- Open a small PR when possible; keep patches focused.
- Include repro steps or test notes. If you touched power, Wi‑Fi, or display behavior, mention which device you tested on and what changed functionally.
- If adding packages, justify their resource footprint and E‑ink friendliness; prefer static/diet alternatives.
