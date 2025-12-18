# Paper Linux

Paper Linux is a Buildroot-based, low-footprint Linux OS for e‑ink “paper” devices. The first tests happen on an older, keyboarded reader class device, but the goal is to stay portable across e‑ink readers and other low-power handhelds.

## What we are building

Paper Linux aims to feel like a purpose-built, ready-to-use OS for e‑ink handhelds, freeing these devices from locked-down firmware and turning them into pocketable, weeks-long Linux PDAs. The idea is to take the quiet, distraction-resistant nature of a paper display and pair it with the flexibility of real Linux—no app stores, no lock-in, just a small machine that boots into a capable shell and lets you decide what it should do.

We want the out‑of‑box experience to feel complete: power on, land in a friendly home, drop into a shell, and start doing things that make sense on a slow, long‑lived, keyboarded device. Read and annotate docs, keep notes, sync a Git repo, run SSH sessions, tail logs, or monitor servers from your pocket. Think about a quiet train ride where an e‑ink status pane shows htop ticking along while newsboat pulls fresh feeds; flip to nnn to reshuffle files, open neovim or micro for a quick edit before a meeting, or jump into a lean Emacs that feels like a personal notebook. All of it runs on paper, in your pocket, and sips battery instead of drinking it.

To make that possible, Paper Linux will ship as a single integrated image: vendor kernel underneath, a lightweight userland on top, and a curated set of tools that behave well on slow CPUs and limited RAM. Everything is tuned for E‑ink first—layouts that tolerate slow refresh, redraws kept to a minimum, radios dark until explicitly asked for, and a system that prefers to nap rather than spin.

## Current status

Near term, the focus is a dependable chroot-style environment that you can drop onto a device and immediately use: modern userland, BusyBox for the core pieces, and a small set of useful applications chosen for low resource use. You can build that root filesystem today with the Buildroot external tree in this repo and run it alongside the vendor kernel and bootloader. Longer term, the same work will roll into a full OS image that can be flashed onto supported devices.

## Build instructions

Install the usual Buildroot prerequisites for your host distro (compiler toolchain, `make`, `git`, flex/bison, ncurses headers; see the Buildroot manual if unsure). Then pull submodules:

    git submodule update --init --recursive

Select the reference config for the current hardware:

    make kindle_k3w_defconfig

Build everything with:

    make

Artifacts end up under `output/kindle_k3w/` (rootfs tarball, toolchain, host tools) and sources are cached in `dl/`.

### Using the rootfs today

Copy the generated rootfs to your device (you can use USB networking, SD, internal flash, or whatever you have) and chroot into it from the stock system. Wi‑Fi and SSH are already included; the default root password is `paper`, so change it as soon as you boot.

## Design principles

We value a small, integrated system over an endless menu of packages. Text and TUI come first; dedicated E‑ink UIs are added only when they clearly help. Radios should be dark unless a foreground app or short-lived job explicitly asks for them. With limited RAM and a slow CPU, the system must keep wakeups low and be willing to pause or evict background tasks. And while the 3rd gen Amazon Kindle is the first target, portability matters—avoid baking device quirks into core logic so other devices can be supported in future.

## Contributing

If you want to help, start with this README and the roadmap, then hop to `CONTRIBUTING.md` for contributor guidance and technical details. Real-hardware testing is gold—logs, power numbers, and notes on screen behavior all help move the project forward.
