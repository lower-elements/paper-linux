# Paper Linux

Paper Linux is a Buildroot-based, low-footprint Linux OS for e‑ink “paper” devices. The first tests happen on an older, keyboarded reader class device, but the goal is to stay portable across e‑ink readers and other low-power handhelds.

## What we are building

Paper Linux aims to feel like a purpose-built, ready-to-use OS for e‑ink handhelds, freeing these devices from locked-down firmware and turning them into pocketable, weeks-long Linux PDAs. The idea is to take the quiet, distraction-resistant nature of a paper display and pair it with the flexibility of real Linux—no app stores, no lock-in, just a small machine that boots into a capable shell and lets you decide what it should do.

We want the out‑of‑box experience to feel complete: power on, land in a friendly home, drop into a shell, and start doing things that make sense on a slow, long‑lived, keyboarded device. Read and annotate docs, keep notes, sync a Git repo, run SSH sessions, tail logs, or monitor servers from your pocket. Think about a quiet train ride where an e‑ink status pane shows htop ticking along while newsboat pulls fresh feeds; flip to nnn to reshuffle files, open neovim or micro for a quick edit before a meeting, or jump into a lean Emacs that feels like a personal notebook. All of it runs on paper, in your pocket, and sips battery instead of drinking it.

To make that possible, Paper Linux will ship as a single integrated image: a lightweight kernel and userland with a curated set of tools that behave well on slow CPUs and limited RAM. Vendor kernels remain supported where hardware requires them, while mainline kernels are preferred where practical. Everything is tuned for E‑ink first—layouts that tolerate slow refresh, redraws kept to a minimum, radios dark until explicitly asked for, and a system that prefers to nap rather than spin.

## Current status

The established Kindle 3 target builds a dependable chroot-style environment that runs alongside the vendor kernel and bootloader. A separate mainline target provides a minimal userspace/initramfs scaffold for upstream-kernel development; it does not yet build a bootable Kindle kernel. Longer term, the same work will become a complete RAM-bootable and eventually installable OS image.

## Build instructions

Install the usual Buildroot prerequisites for your host distro (compiler
toolchain, `make`, `git`, flex/bison, ncurses headers; see the Buildroot manual
if unsure) and install `just`. Then pull submodules:

    git submodule update --init --recursive

Configure the established vendor-kernel output tree:

    just configure kindle3_vendor_2_6_26

Alternatively, configure the minimal mainline-development scaffold:

    just configure kindle3_mainline

Change an existing output configuration through Buildroot's config helper:

    just config kindle3_mainline --set-str BR2_PACKAGE_FOO y

Build everything by naming the configured output tree:

    just build kindle3_vendor_2_6_26

Buildroot targets can be passed after the configuration, for example:

    just build kindle3_mainline olddefconfig
    just build kindle3_mainline linux-rebuild

Artifacts end up under the corresponding directory in `output/`, such as `output/kindle3_vendor_2_6_26/` or `output/kindle3_mainline/`. Sources are cached in `dl/`.

Hardware research uses a separate, reproducible catalog for large Git
repositories and third-party documents that cannot be committed here. See
[`docs/external-resources.md`](docs/external-resources.md) to list or populate
it in any directory you choose. With `uv` and `just` installed, start with
`just resource list`.

### Using the vendor rootfs today

Copy the generated rootfs to your device (you can use USB networking, SD, internal flash, or whatever you have) and chroot into it from the stock system. Wi‑Fi and SSH are already included; the default root password is `paper`, so change it as soon as you boot.

## Design principles

We value a small, integrated system over an endless menu of packages. Text and TUI come first; dedicated E‑ink UIs are added only when they clearly help. Radios should be dark unless a foreground app or short-lived job explicitly asks for them. With limited RAM and a slow CPU, the system must keep wakeups low and be willing to pause or evict background tasks. And while the 3rd gen Amazon Kindle is the first target, portability matters—avoid baking device quirks into core logic so other devices can be supported in future.

## Contributing

If you want to help, start with this README and the roadmap, then hop to `CONTRIBUTING.md` for contributor guidance and technical details. Real-hardware testing is gold—logs, power numbers, and notes on screen behavior all help move the project forward.
