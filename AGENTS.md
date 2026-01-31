# Paper Linux quick brief for AI Coding Agents

- Goal: integrated Linux PDA OS for e-ink readers; current dev targets Kindle 3-class hardware (ARM11 ~532 MHz, 256 MB RAM, no GPU) but keep designs hardware-agnostic for future devices.
- Build: buildroot-based, repo is a buildroot external tree, buildroot subdirectory is a git submodule; make changes (config files, new buildroot packages, patches) to the external tree (repo root), not the buildroot checkout.
- Patch files: when creating `.patch` files, edit a copy directly and generate the patch via `diff -u`; always add an email-style header to `.patch` files.
- Commits: use descriptive Conventional Commit-style messages; use `git commit -m` multiple times (first for the subject, then a description of what the commit does and why it was needed when relevant).
- Resource policy: musl, busybox, shared libs, dropbear, CLI/TUI-first; avoid heavy daemons and GUIs.
- Kernel constraint: must ride vendor kernels (e.g., Kindle 3 ships 2.6.26 RT). Expect old syscalls/APIs only; available: epoll/inotify, threads, some namespaces, early cgroupsv1, netlink. Do not attempt new-driver work; keep syscall ABI intact when patching headers.
- Current state: custom chroot with modern musl, busybox, wpa_supplicant, OpenSSL, curl, etc.; some patches applied. Aim to grow into full flashable OS (kernel + bootloader + userland).
- System glue: prefer built-in tools—busybox init/mdev/uevent helpers; no systemd or heavyweight supervisors.
- UX target: tinkerers/power users wanting distraction-free, keyboarded, pocket e-ink Linux for reading, notes, SSH, small terminal apps; battery life and low write/refresh churn are critical.
- Roadmap highlights: Emacs (possibly fbink GUI backend); session manager for launching, focus/display/keyboard arbitration, background pause, LRU killing; slim networking daemon using wpa_supplicant + netlink + busybox udhcpcd with lease-based radio control; power daemon; e-ink-optimized home screen and file manager.
- Design reminders: e-ink refresh latency is hundreds of ms; flash is slow; memory is tight; minimize radios-on time; be frugal with RAM/CPU and filesystem writes.
- Docs tone: stay generalized to e-ink readers—refer to “Kindle 3-class” only when necessary; avoid implying the project is Kindle-only.

## Near-term engineering plans

- Init/boot: stick with BusyBox init; generate rcS ordering at boot from `/etc/paper/services.dep` via `tsort`; keep scripts simple (`start|stop|restart|status`); prefer `start-stop-daemon`; no runtime supervision.
- Networking daemon: orchestrates wpa_supplicant + busybox udhcpc; lease-based radio control so Wi-Fi powers up only when a lease is held; coordinates with session manager for user UI. `netctl` or similar command to request, renew, and release leases holding the network online.
- Session manager (home/launcher/compositor): owns framebuffer; keeps apps off `/dev/fb0`; reads evdev directly and re-emits via `uinput`; TUIs run on PTYs; GUIs render to shared memory and send damage rects; arbitrates focus, refresh, freeze/thaw, LRU kill; protect it with high OOM priority.
- App sandboxes/portal: per-app mount+net namespaces when available; default view is minimal; `paperfm` acts as portal for open/save—session manager opens files and passes fds (SCM_RIGHTS) or bind-mounts selected paths into the app namespace; optional full-FS toggle.
- File manager/home: custom e-ink-friendly file manager (`paperfm`) plus lightweight home UI (not Emacs) integrated with the session manager and portal flow.
