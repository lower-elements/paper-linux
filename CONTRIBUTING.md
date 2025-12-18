# Contributing to Paper Linux

Thanks for helping turn old e‑ink readers into pocketable Linux PDAs. This guide collects the nuts-and-bolts details contributors tend to need. We build on vendor kernels, not mainline: our first target device—the Kindle 3—ships a 2.6.26 RT kernel, and future devices will bring their own (often equally old) kernels. Assume you’ll be working with aging vendor kernels and keep changes compatible with what each device ships.

## How to contribute

Start with `README.md` and `ROADMAP.md` to get the big picture. If you’re unsure where to jump in, open an issue or discussion and we’ll point you at something useful. We favor upstream-first fixes, small composable changes, and minimal forks—only fork when the hardware forces us. Whatever you touch should stay compatible with the vendor kernel in use (usually old), so avoid newer kernel APIs unless guarded. Real hardware testing matters—share what you tried, what worked, and what broke. Prefer lightweight tooling—BusyBox for core utilities, musl for libc, shared libs where possible.

## Technical details

Our baseline hardware looks like early-2010s e‑ink readers: an ARM11 around 532 MHz, roughly 256 MB of RAM, no GPU, and slow flash. The kernel comes from each device vendor and is usually old; for the first device that’s a 2.6.26 RT build from the Lab126 tree. We patch headers sparingly to keep the syscall ABI intact and avoid replacing kernels for now.

Builds are driven by the Buildroot external tree in this repo. The toolchain is musl-based with C and C++ and prefers shared libraries. BusyBox supplies init/mdev and most core utilities; device nodes are currently static and `/usr` is merged. Networking leans on the legacy WEXT path in wpa_supplicant with `nl80211` disabled to match the old kernel API. Power and resource posture is strict: minimize redraws, keep radios off by default, and pause or evict background work when resources are tight. Today everything runs as a chroot/alt-root alongside the vendor bootloader and kernel; turning this into a flashable image remains on the roadmap.

## Repository layout

Paper Linux is organized as a Buildroot external tree. That means upstream Buildroot does the heavy lifting while this repo supplies configs, patches, and packages. If you’re new to the pattern, the [Buildroot manual](https://buildroot.org/docs.html) has a good overview of external trees and how they plug into a build.

Key paths to know:
- `Makefile` — wrapper that drives Buildroot with `BR2_EXTERNAL` pointing here; outputs are under `output/`.
- `configs/kindle_k3w_defconfig` — reference defconfig for the current hardware baseline.
- `configs/busybox.config` — BusyBox feature set tuned for small/slow systems.
- `package/linux-headers/` — header patches that let modern userland build against old vendor kernel headers without breaking ABI (e.g., 2.6.26 on the first device).
- `package/wpa_supplicant/` — minimal patching for old WEXT-only Wi‑Fi stacks.
- `dl/` — download cache (created after the first build).
- `buildroot/` — Buildroot sources (kept in-tree for reproducibility).
- `output/` — per-defconfig build output (created after building).

## Getting set up
1) Install standard Buildroot prerequisites for your host distro.  
2) Fetch submodules: `git submodule update --init --recursive`  
3) Use the reference config: `make kindle_k3w_defconfig`  
4) Build: `make`  
Artifacts land under `output/kindle_k3w/`; downloads cache in `dl/`.

## Contribution workflow
- Open a small PR when possible; keep patches focused.
- Include repro steps or test notes. If you touched power, Wi‑Fi, or display behavior, mention which device you tested on and what changed functionally.
- If adding packages, justify their resource footprint and E‑ink friendliness; prefer static/diet alternatives.
