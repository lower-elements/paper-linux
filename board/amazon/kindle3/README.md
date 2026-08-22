# Amazon Kindle 3

The Kindle 3 board has separate configurations for its original vendor
kernel and for mainline development:

- `vendor-2.6.26` contains the established chroot/rootfs integration for the
  Lab126 kernel.
- `mainline` is the home for the Device Tree, kernel configuration, RAM-boot
  assets, and patches developed for upstream Linux.

The mainline configuration carries a RAM-booted bring-up Device Tree based on
the upstream i.MX35 SoC description. The following has been exercised on a
Wi-Fi-only Shasta PVT1 Kindle 3:

- 256 MiB RAM, the debug UART, Barebox-to-Linux boot and an embedded BusyBox
  initramfs;
- USB ACM console and ECM networking;
- the keyboard matrix, five-way, volume keys and PMIC power slider through
  evdev;
- both I2C controllers;
- eMMC discovery and partition enumeration using the conservative 1-bit
  fallback (wider-bus bring-up remains unresolved);
- MC13892 discovery over SPI, its RTC, ADC and power button;
- the corrected revision-2 VPLL description and a deliberately narrow
  regulator policy registering only the confirmed 3.15 V VGEN2 eMMC rail;
  and
- the BQ27210 fuel gauge through the upstream power-supply driver, with NVM
  updates disabled.

The green MC13892 status LED is described conservatively but remains
unverified. The headphone-only WM8960 configuration binds successfully and
registers an ALSA card under mainline; actual playback routing remains to be
tested. The internal speakers, charger policy, suspend, orderly power-off,
display and Wi-Fi remain outside the enabled mainline hardware set.

## Source provenance

Kernel archaeology is maintained in the sibling `../kernel-worktrees`
collection. Its exact Amazon GPL snapshot is
`amazon-kindle3-3.4.3` (`archive/amazon-kindle3-3.4.3`); the known Freescale
MX35 BSP snapshot, upstream and stable bases, PREEMPT_RT reconstruction, NXP
branches and Linux 7.0.11 source are separate worktrees. Provenance notes and
the generated per-file matrix are under `meta-provenance`.

The current evidence says the Amazon archive is based generally on upstream
Linux v2.6.26, descends from the known 2009-03-17 Freescale MX35 BSP snapshot,
and incorporates an adapted 2.6.26.8-rt16 layer plus later unresolved work.
The reconstruction is a comparison aid, not historical commit ancestry. In
particular, an `amazon-only` file means only that no matching file was found
in the compared source states; it is not an authorship claim.

See [power-management.md](power-management.md) for the evidence gathered from
the stock Wi-Fi-only hardware, the provenance-separated kernel sources,
component documentation and board teardowns. It distinguishes confirmed
regulator assignments from unverified physical connections that must not yet
be encoded in the Device Tree.

See [audio.md](audio.md) for the established WM8960 wiring, the deliberately
headphone-only first mainline configuration, and the evidence that must be
collected before enabling the internal speakers or automatic jack switching.

For bring-up, the mainline initramfs exposes a composite USB gadget containing
the `ttyACM` console and an ECM network link. The Kindle uses `192.168.2.2/24`;
the directly connected development host is expected to use `192.168.2.1/24`.
Dropbear listens only on the Kindle's USB address. The RAM-only test image has
an empty root password, so this interface is intentionally unsuitable for a
normal or multi-user installation.

Files under `common` must describe hardware or behavior shared by both
kernel lines.
