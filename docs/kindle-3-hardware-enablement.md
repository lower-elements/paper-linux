# Kindle 3 hardware enablement and stock-daemon survey

This is a read-only survey of a Kindle 3 Wi-Fi (B008) running stock firmware
3.4.3, build `001-S403_luigi-354362`, and kernel `2.6.26-rt-lab126`.
It deliberately ignores Amazon application plumbing except where it reveals a
hardware contract. The goal is to identify what Paper Linux needs for complete
hardware operation, what can be implemented against kernel interfaces, and
what old or opaque artifacts must still be preserved.

The short answer is encouraging: **no proprietary userspace library is needed
for normal operation of the display, input, audio, battery, charger, RTC,
suspend, CPU frequency, or USB gadget hardware.** Their Lab126 kernel drivers
are present in Amazon's released GPL source and expose fbdev, evdev, ALSA,
sysfs, procfs, uevents, and standard device nodes. The material binary
dependency is the Atheros AR6002 target firmware and board-data bundle. The
e-ink waveform is another opaque artifact, but on the inspected unit it lives
in the display controller's own flash. The driver can also load a newer
panel-matched waveform from the rootfs; that override path is not taken on the
inspected unit.

The exact matching Amazon archive is the
`archive/amazon-kindle3-3.4.3` ref in the external kernel-history repository.
Its provenance-separated upstream, stable, RT, Freescale/NXP and
current-mainline comparison trees and confidence notes are on the
`meta/provenance` branch. See the [external resource
catalog](external-resources.md) for the portable public subset. “Archive-only”
paths in that analysis are evidence of source presence, not proof of Amazon
authorship.

## What SysV init starts

`/etc/inittab` defaults to runlevel 2. That runlevel performs the low-battery
gate and advances to normal runlevel 5 when it is safe to boot. The live unit
was running as `init [5]`.

Normal boot starts these relevant layers, in order:

1. `video` loads the e-ink HAL, controller, and compatibility-shim modules.
2. `pre-wifid` loads and initializes the AR6002 and starts
   `wpa_supplicant`.
3. D-Bus and `pmond` start before most native Amazon daemons.
4. `powerd` starts, followed by `cmd` and `wifid`.
5. `volumd`, `audioServer`, and `ttsd` start later.
6. Amazon transfer, browser, and Java-framework services start last.

The live native processes included `syslog-ng`, `fsp`, `lifeguard`, D-Bus,
`pmond`, `powerd`, `cmd`, `wifid`, `phd`, `mcsd`, `volumd`, `audioServer`,
`ttsd`, `tmd`, `webreader`, `browserd`, the Java VM, and
`wpa_supplicant`. Most of these are Amazon policy or application services, not
hardware support.

`pmond` launches and restarts many daemons on behalf of their init scripts.
Paper Linux need not reproduce this indirection; BusyBox init and ordered
service scripts are sufficient.

## Hardware-facing daemon verdict

| Stock component | Actual hardware role | Interface used by a replacement | Must retain? |
| --- | --- | --- | --- |
| `video` scripts | Select and load the e-ink modules; optionally update controller flash | module loading, fbdev, driver sysfs/procfs | Replace with a small board init script |
| `lifeguard` | Last-resort recovery after the e-ink driver's own watchdog fails | FIFO notification followed by module restart/reboot | Replace with a tiny recovery hook |
| `powerd` | Suspend policy, battery safety, RTC wake, input/charger activity | sysfs, `/sys/power/state`, RTC, kernel uevents | Replace policy, not binary/library |
| `wifid` | Wi-Fi profile and connection policy | `wpa_supplicant`, DHCP, netlink/WEXT, Wi-Fi sysfs | Replace policy; retain AR6002 blobs initially |
| `cmd` | Selects Wi-Fi/cellular connection and idle-disconnect policy | ordinary network state | Fold into Paper Linux network daemon |
| `volumd` | USB mass-storage/network/serial mode and mounts | USB gadget modules/sysfs, mount APIs | Small optional replacement |
| `audioServer` | Playback RPC service | ALSA | Do not clone |
| `ttsd` | Nuance speech engine | ALSA after software synthesis | Replace with eSpeak NG |
| `mcsd`/`wand` | Cellular modem policy | USB serial and WAN GPIO | Omit on Wi-Fi-only target |
| `pmond` | Process launch/restart/memory policy | `/proc`, signals | Omit |
| `phd`, `tmd`, browser, Java framework | Amazon services and UI | none needed for hardware | Omit |

Amazon's IPC, logging, crash-handling, and service libraries are irrelevant to
the hardware boundary and are intentionally not catalogued here.

## Hardware interfaces Paper Linux can use directly

### Battery, charging, suspend, and wake

`powerd` is the only stock daemon whose policy is genuinely important to
replace. The stock binary is a 68 KiB stripped ARM executable. Its live
process had three threads, about 1.5 MiB resident, and open descriptors for
`/dev/i2c/0`, `/dev/fb/0`, and a kernel-uevent netlink socket. Inspection of
the binary, its live configuration, the live sysfs tree, and the matching
released kernel gives the following division of responsibility.

#### What stock `powerd` does

Its main state machine is:

```
ACTIVE --600 s idle--> SCREEN SAVER --60 s idle--> READY TO SUSPEND
                                                    |
                                            5 s default grace
                                                    v
                                               SUSPENDED
```

Input or an explicit wake request returns to `ACTIVE`. Short and held power
button events, charger events, a cover magnet on models which have one, USB
gadget state, drive mode, suspend deferrals, and Amazon service readiness can
alter those transitions. The 600, 60, and 5 second values are the live 3.4.3
configuration, not hardware constants.

The complete set of hardware-facing work found in `powerd` is:

- Poll battery capacity, mAh, voltage, current, temperature, lifetime maximum
  discharge, and cycle statistics every 30 seconds. Publish/log changes and
  distinguish charging from not charging.
- Listen for charger-driver uevents, including `BATTERY=low`,
  `BATTERY=critical`, and `BATTERY=wanon`; on a valid critical event create
  `/var/local/system/low_batt`, show the low-battery display, and execute the
  configured orderly power-off command after a short grace period.
- Listen for the `luigibutton` driver's `online`/`offline` uevents and for
  keyboard/five-way activity. Debounce short power-button events in userspace.
- Before sleep, coordinate quiescence with Amazon services, optionally sync
  logs, lock the keyboard and five-way through `/proc/keypad` and
  `/proc/fiveway`, disable the accessory port through its sysfs attribute,
  blank the e-ink framebuffer, and write `mem` to `/sys/power/state`.
- After resume, unblank the framebuffer, re-enable the accessory port, unlock
  input, restore/check wall-clock state, and notify the rest of Amazon's stack.
- Optionally program a relative PMIC RTC wake alarm through
  `mxc_rtc.0/wakeup_enable`, and avoid entering sleep so close to an alarm that
  the suspend transition could miss it.
- Clear or display stock screens through `FBIOBLANK` and the released e-ink
  ioctls. The observed private ioctl numbers decode to
  `FBIO_EINK_CLEAR_SCREEN`, `FBIO_EINK_SPLASH_SCREEN_SLEEP` (argument 19 is
  the low-battery image), and `FBIO_EINK_SET_REBOOT_BEHAVIOR`.
- It contains CPU-governor switching code (`performance` around resume and
  back to `ondemand`), but the live configuration has
  `cpufreq_do_not_change=1`, so firmware 3.4.3 does not use it on this unit.

The remaining functions are Amazon policy rather than hardware support:
LIPC properties/events, screen-saver presentation, drive-mode integration,
Java-VM recovery hooks, log rotation, battery telemetry, and the readiness
protocol by which every Amazon service may delay suspend. Paper Linux should
replace that protocol with simple named inhibit leases.

#### What the kernel already does without `powerd`

The charger is not controlled by the daemon. `arcotg_udc.c` directly owns the
MC13892 charging state machine. Its ten-second kernel work item detects charger
type, selects charge current, handles USB enumeration, charger over-voltage,
low-voltage BPON charging, full-battery taper/restart, charge-timer expiry,
and the amber/green charge LED. Charging remains under PMIC/kernel control
while the CPU is suspended.

The gauge driver polls the battery independently. After five consecutive
out-of-range samples it sets an error for temperatures at or below 37 F or at
or above 113 F; I2C, voltage, missing-battery, and other gauge faults are also
reported. The charger work item sees those flags and sets charge current to
zero. The stock daemon also contains a 32--113 F temperature check and tries
to write `temp_ok` and `battery_full` under
`/sys/devices/platform/charger/`, but that directory does not exist on the
live 3.4.3 system. Those are stale compatibility paths, not an active safety
boundary. A replacement should still monitor and report `batt_error`, but it
must not reproduce those dead writes.

The charger driver itself detects 3.4 V low and 3.1 V critical conditions and
emits the uevents. It refuses system suspend below 3.42 V, since suspend would
be unsafe at that voltage. It does **not**, however, perform an orderly
shutdown on a critical uevent; userspace must do that before the PMIC reaches
hard cutoff.

Writing `mem` invokes the vendor kernel's complete i.MX35 suspend path: device
PM callbacks, process freezing, Papyrus/display GPIO handling, PLL and
oscillator gating, regulator low-power modes, and MX35 STOP power-off state.
The RTC driver maintains time across suspend. The e-ink driver independently
drops its controller to standby after two seconds of display inactivity
(`power_timer_delay=2000` on the live unit), so no daemon must keep polling or
power-cycle the display between updates. A normal `poweroff` reaches the
kernel's board-specific MC13892 power-cut routine.

#### Minimum Paper Linux contract

A safe first `paper-powerd` therefore needs only the following functionality:

1. Consume evdev activity and `luigibutton`/charger netlink uevents, maintain
   an inactivity timer, and support suspend-inhibit leases. There is no need
   to recreate the two-stage screen-saver state machine.
2. Read battery and charger values from sysfs at startup, on uevents, and at a
   modest fallback interval. Treat read errors conservatively. On an
   uncharged `BATTERY=critical` event (or a confirmed equivalent voltage),
   warn once, sync important data, and invoke orderly `poweroff` promptly.
3. Before suspend, stop accepting new work, ask the session and network
   managers to quiesce, sync mutable filesystems as required, and wait for
   bounded inhibit leases. The AR6002 driver used here deliberately returns
   `-EBUSY` from its suspend callback, so the network manager must unload it
   and/or complete the stock radio-power-down sequence before `mem` can
   succeed. This is a whole-system requirement even if radio ownership stays
   outside `paper-powerd`.
4. Write `lock` to `/proc/keypad` and `/proc/fiveway`. This is required on this
   vendor kernel: locking cancels polling/work, disables the keypad clock and
   keyboard/five-way IRQs, and changes the GPIO state. Both drivers' suspend
   callbacks explicitly assume userspace has already locked them.
5. Write `0` to
   `/sys/devices/system/mx35_accessory/mx35_accessory0/mx35_accessory_state`
   before suspend. The platform suspend file claims this is done, but only
   declares the function and never calls it; `powerd` performs the write. It
   removes power from the lighted-cover/accessory regulator and disables its
   detection IRQ. This may be omitted only on a deliberately unsupported
   board/feature after measuring that no accessory rail remains powered.
6. Optionally blank `/dev/fb0` with `FBIOBLANK` for presentation. This is not
   needed to retain an e-ink image or to obtain the driver's normal two-second
   idle power saving, but is sensible before system sleep.
7. If a timed wake is requested, write the relative number of seconds to
   `/sys/devices/platform/mxc_rtc.0/wakeup_enable`, allowing a safety margin,
   then write `mem` to `/sys/power/state`. A zero write does not cancel an
   existing alarm in this old driver; timed-wake cancellation needs explicit
   testing or an RTC ioctl path.
8. On return from the blocking `mem` write, re-enable the accessory port,
   write `unlock` to both input proc files, unblank the framebuffer, restart
   radio only if a network lease requires it, and resume clients. Every
   pre-suspend step needs rollback if a later step or kernel suspend fails.

For battery life while the machine is awake, the network manager must also
bring the interface down, unload/quiesce the AR6002 host driver, and write `0`
to `/sys/devices/system/wifi/wifi0/wifi_enable` whenever no network lease
exists. The GPL board module powers Wi-Fi on during its own initialization;
merely leaving `wlan0` unassociated does not remove power from the AR6002 and
its SW1 rail. This belongs to radio policy, but omitting it would dominate any
savings made by `paper-powerd`.

No old userspace binary is needed for this contract. All required controls,
including the unusual procfs locking commands and e-ink ioctl definitions,
are implemented and documented in the released Lab126 kernel. The
proprietary `libgasgauge.so.0` is avoidable, as described below.

It links the proprietary `libgasgauge.so.0`, but that library contains no
hidden algorithm or vendor runtime. ELF inspection shows that it depends only
on libc and exports thin functions such as `gasgauge_charge_percent`,
`gasgauge_temperature`, `gasgauge_milliamp_hours`, `gasgauge_current`, and
`gasgauge_voltage`. It opens `/dev/i2c/0` and performs I2C ioctls.

The released GPL driver already performs the same I2C transactions and exports
the useful results at:

```
/sys/devices/system/luigi_battery/luigi_battery0/
```

Observed attributes include `battery_capacity`, `battery_current`,
`battery_voltage`, `battery_temperature`, `battery_mAH`, `battery_lmd`,
`battery_cycl`, `battery_cyct`, `battery_id`, `battery_error`,
`battery_suspend_current`, thresholds, polling intervals, and resume
statistics. The live gauge was at I2C address `0x55`.

The USB-device-controller driver owns charger detection and PMIC charging. It
exposes `connected`, `charging`, `charger_state`, `ichrg_setting`, `voltage`,
`battery_current`, `batt_error`, `battery_id`, `third_party`,
`charger_adc_voltage`, and low-battery state below:

```
/sys/devices/platform/fsl-usb2-udc/
```

It sends uevents for attach/detach, `CHARGER=thirdparty`, `BATTERY=low`, and
`BATTERY=critical`. Charging therefore continues in the kernel; userspace must
monitor and apply orderly-shutdown policy, not drive the PMIC register by
register.

Other direct controls are:

```
/sys/power/state                         # supports standby and mem
/sys/devices/platform/mxc_rtc.0/wakeup_enable
/sys/devices/system/cpu/cpu0/cpufreq/   # standard governors
/sys/devices/system/mx35_accessory/mx35_accessory0/mx35_accessory_state
/proc/keypad and /proc/fiveway           # vendor lock/unlock controls
/dev/rtc0 and /dev/rtc1
```

The power button is unusual: `/dev/luigibutton` is a misc node, but the driver
does not implement a read API. It signals short/long state using online/offline
uevents. Keyboard, five-way, and volume activity is available through evdev
and also produces activity uevents. A replacement power daemon should listen
to kernel uevents, read battery/charger sysfs, perform the pre-suspend
input/accessory sequence above, program RTC wake when needed, and finally
write `mem` to `/sys/power/state`.

Relevant released source:

- `drivers/power/luigi_battery.c`
- `drivers/usb/gadget/arcotg_udc.c`
- `drivers/char/luigi_button.c`
- `drivers/input/keyboard/mxc_keyb.c`
- `drivers/input/fiveway/fiveway.c`
- `drivers/input/volume/volume.c`
- `arch/arm/mach-mx35/mx35_accessory.c`
- `arch/arm/mach-mx35/pm.c`
- `drivers/rtc/rtc-mxc.c`

### Keyboard, five-way, and volume keys

These are ordinary Linux input devices:

| live event node | driver/device name |
| --- | --- |
| `/dev/input/event0` | `mxckpd` keyboard matrix |
| `/dev/input/event1` | five-way controller |
| `/dev/input/event2` | volume keys |

No daemon or library is required. Read evdev and identify devices from
`/proc/bus/input/devices` or sysfs rather than relying permanently on event
numbers. The matching drivers are enabled in
`arch/arm/configs/imx35_luigi_defconfig` and their source is released.

### E-ink display

The stack is three GPL modules: `eink_fb_hal`, the Broadsheet controller HAL,
and `eink_fb_shim`. They expose `/dev/fb0` as `eink_fb`, the legacy update
ioctls, and diagnostic/control attributes below
`/sys/devices/platform/eink_fb.0` and `/proc/eink_fb`.

Paper Linux already has an FBInk patch for the Kindle 3 extended refresh modes,
so normal rendering needs no Amazon userspace binary. The ioctl definitions
and complete Broadsheet/Papyrus implementation are in the released tree under
`drivers/video/eink/`.

The display controller has a 256 KiB serial flash image. The released driver
defines command data at offset `0x00000`, waveform data at `0x00886`, and panel
data at `0x30000`. On the inspected device:

```
panel EEPROM ID:  ED060SC7C1
panel ID:         V220_052_60_M24
waveform:         V220_C052_60_WJB701_D (M24, S/N 1540, 85Hz)
runtime commands: V0303 (C/S 0000EB65), compiled into the ISIS driver
physical flash:   262144 bytes, MD5 37a264fd8fe8e193da82c4caebd0c648
physical commands: P_0B.00, checksum 0xB2350C2B
flash waveform:   76557 bytes, MD5 b2c6563e16fe0d48b7e429a683a33f81
waveform CRC32:    embedded 0xC662DE78, computed 0xC662DE78
bs_bootstrap:     0
eink_rom_is_flash: 0
```

The name `eink_rom_is_flash` is misleading: zero means that this flash is
read-only to userspace on this Luigi/ISIS configuration, not that no flash is
present.

The MD5 of the whole flash is not expected to equal any `.wbf` checksum. It
covers the command region, the waveform region, unused/padding space, and
panel data. A `.wbf` has its own length in its header and its embedded CRC32 is
computed over exactly that many waveform bytes. The extracted C052 waveform's
embedded and recomputed CRC32 agree. The stock `eu` tool likewise reports
matching embedded/computed checksums for all six rootfs `.wbf` files.

The command versions differ for a related reason: on ISIS the active V0303
command set is compiled into the driver and loaded during controller
initialization. The P_0B.00 image at physical flash offset zero has the same
checksum as the rootfs `cmd0047_...bin` recovery/update file. `/proc` reports
the active built-in command set, whereas a base-zero physical flash dump
contains P_0B.00.

The rootfs `.wbf` files have **two roles**:

1. At every module load, `/etc/init.d/video` creates symlinks in
   `/var/local/eink/` named by panel ID. The kernel first tries `isis.wbf`, then
   the matching panel-ID symlink, then the waveform in controller flash. If a
   matching rootfs file exists, it compares its version with the flash version
   and loads the selected one into Broadsheet SDRAM. Thus these are genuine
   runtime waveform inputs, not only update files.
2. On hardware where the controller flash is writable, later boot code also
   compares the matching file with the installed version and can flash a newer
   waveform using `ewu`.

This unit has symlinks for six panel IDs, but none for its
`V220_052_60_M24` ID. The kernel therefore validates and loads the waveform
from physical flash. The runtime-reported version matches the 76,557-byte
waveform extracted from physical offset `0x886`; the tiny 1,668-byte fallback
compiled into the driver has a different header and was not selected.

There is also exactly the update check the stock design implies. During the
second `video start`, the script creates `/var/local/eink/update_flash` only
when display preflight passed, the controller reports writable flash, and
`dont_update` is absent. Framework startup calls `eink_restart_check`, which
consumes that marker and runs `/etc/init.d/video update_flash`. That function
checks the installed and file versions, flashes a newer matching file, and
removes non-applicable update files. On this unit the marker is never created
because `eink_rom_is_flash` is zero, so the common rootfs files remain present
and are not flashed.

`ecu`, `ewu`, `eu`, and `bootstrap-broadsheet` implement parsing and flash
updates. They are not needed during an ordinary boot of this read-only-flash
panel, but `eu` remains useful for validating backups.

`lifeguard` is the one display-side process worth preserving in concept. It is
only an 871-byte shell loop: it creates `/tmp/.einkfb_reset_file` as a FIFO and
blocks reading it. If the driver's internal Broadsheet watchdog cannot restore
the controller, the kernel writes to that FIFO; stock userspace then restarts
the video stack or reboots. A Paper Linux equivalent needs no Amazon library,
but should create the same FIFO and perform a controlled reboot (or a proven
module reload sequence) so a terminal controller failure does not leave the
device alive with a frozen screen.

Consequences:

- Preserve the controller contents and do not attempt waveform updates from
  the independent OS.
- Back up each device before migration while the stock driver is loaded. The
  `/proc/eink_fb/eink_rom` file is banked: first save
  `eink_rom_select`, write `1` (`bs_flash_commands`) so reads begin at physical
  offset zero, dump exactly 262,144 bytes, and restore the saved selector.
  Reading it with the default selector `0` starts at the waveform base and is
  **not** a valid whole-flash backup.
- Archive the panel EEPROM text (`eeprom_whole`, `panel_id`, `panel_bcd`, and
  `vcom`) alongside the ROM. VCOM is panel-specific and is the item most likely
  to cause display damage or poor image quality if guessed.
- Archive every rootfs waveform as well: other panel variants may select one
  at every boot. They are not required for this particular C052 panel because
  its matching, valid waveform is in read-only controller flash.

The opaque waveform remains a supply-chain concern for replacement panels or
blank/corrupt controller flash. It is not a proprietary shared-library
dependency.

### Wi-Fi

The board power/reset GPIOs are already wrapped by the GPL platform driver:

```
/sys/devices/system/wifi/wifi0/wifi_enable
/sys/devices/system/wifi/wifi0/wifi_reset
```

After firmware startup, normal connection management uses
`wpa_supplicant`, BusyBox `udhcpc`, Wireless Extensions, sockets, and ordinary
network configuration. `wifid` and `cmd` contain no indispensable radio
implementation. Replace them with the planned lease-based network daemon.

Radio startup is the serious binary boundary. The AR6002 is a FullMAC device
whose target processor receives firmware at each load. Stock boot does this:

1. Power the SDIO device and load `ar6000.ko` with BMI enabled.
2. Use `bmiloader` private driver ioctls to inspect and configure the target.
3. Use `eeprom.AR6002` to transfer the Lab126 board/regulatory data and insert
   the MAC address read from IDME.
4. Upload `athwlan.bin.z77` and `data.patch.hw2_0.bin` into target RAM.
5. Send BMI done and then start `wpa_supplicant`.

The exact GPL host driver is
`drivers/net/wireless/ath6k22.133`. Its public headers document the private
BMI ioctls, target addresses, WMI protocol, and structures. The four stock
tools are ARM/glibc executables with only a libc dependency:

| tool | role | MD5 |
| --- | --- | --- |
| `/sbin/bmiloader` | BMI memory/register access and target start | `a9d254281f35912c56898122362369e7` |
| `/sbin/eeprom.AR6002` | board-data transfer and MAC insertion | `ac7bbcd6cf50f98ac584c4103acd1748` |
| `/sbin/wmiconfig` | vendor WMI diagnostics/configuration | `04392b11affe8bfdee91bec34249d75b` |
| `/usr/bin/recEvent` | driver debug-event capture | `f77ed20895007f7e170c9be0c33be96b` |

Only `bmiloader` and `eeprom.AR6002` are required by the stock bring-up path.
`recEvent` is debugging, and the boot-time `wmiconfig` use merely configures
debug logging. Their source is **not** in the extracted Amazon release. They
look like Atheros BSP utilities, not Lab126 application code.

Required `/opt/ar6k` payloads on the inspected image are:

| file | bytes | MD5 | status |
| --- | ---: | --- | --- |
| `athwlan.bin.z77` | 75,610 | `93ca930435d3aa46d68cdc2d9c072297` | indispensable target firmware |
| `data.patch.hw2_0.bin` | 1,328 | `cebf7153afdb47e9bae69f65febe44f9` | indispensable firmware data patch |
| `lab126_15dBm_nodiv_WWR_CTL.bin` | 768 | `00b31b5896144e41c9f45ef17ba9f925` | active Lab126 board/regulatory data |
| `calData_17dBm_lab126_NOdiversity.bin` | 768 | `967121dac8b8326e991a4517444c50e2` | alternate calibration data; not selected by stock init |
| `eeprom.data` | 48 | `e960888c4aa067ff7288051091b4e9c8` | target-side EEPROM helper data |
| `eeprom.bin` | 944 | `9f425aa494d575bb5c1d41c6e29f1725` | target-side EEPROM helper code |
| `loadAR6000.sh` | 15,876 | `8a73af60550a7d70572c3b9c86f77a92` | replaceable shell orchestration |
| platform setup script | 503 | `5c9214b557eb0ea391a4f47ac4d268b8` | replaceable module-load script |

The MAC address is not baked into the common board-data file; the init script
reads the device MAC from IDME and asks `eeprom.AR6002` to patch it into the
transferred image. Preserve IDME/bootloader identity data separately.

Upstream `ath6kl` is not a drop-in escape route on this vendor kernel. Current
Linux wireless documentation lists AR6003/AR6004/AR6005, not this AR6002 rev 2,
and explicitly describes AR600x target firmware as binary-only. The practical
path is:

1. Initially package the exact stock firmware, patch, board-data files, loader
   script, and two required libc-only utilities in a separately sourced vendor
   payload.
2. Reimplement the narrow `bmiloader` and board-data transfer operations from
   the released driver headers. This removes the glibc/tool dependency but not
   the firmware/calibration blobs.
3. Longer term, consider moving firmware loading into the existing driver via
   `request_firmware`, while preserving its userspace/network ABI. This is an
   engineering cleanup, not a way to eliminate the opaque target firmware.

Do not substitute similarly named AR6003 firmware or calibration files. Radio
board data controls RF characteristics and regulatory limits.

### Audio

The live ALSA card is `mx35luigi` with a WM8960 codec and standard playback and
capture nodes below `/dev/snd`. The complete ASoC machine and codec drivers are
released as:

- `sound/soc/imx/mx35luigi_wm8960.c`
- `sound/soc/codecs/wm8960.c`

`audioServer` is only an Amazon RPC wrapper around GStreamer 0.10, ALSA, and
TagLib. It is not hardware support and should not be carried forward. Use ALSA
directly and add audio-focus policy only when needed.

`ttsd` embeds Nuance Vocalizer Auto through `vautov5.so` and proprietary voice
data. This is a dead-end supply dependency, but not a hardware blocker; replace
it with eSpeak NG and feed the result to ALSA.

### USB and storage

`volumd` unmounts/remounts the user store and selects `g_file_storage`,
`g_ether`, or `g_serial`. It uses the USB gadget module parameters/sysfs,
`/proc/mounts`, and ordinary mount/statfs operations. The gadget controller,
charger detection, and gadget-function source are in the released kernel.

No vendor userspace library is required. A small privileged helper is useful
only if Paper Linux supports dynamic USB mass-storage export; fixed USB
networking can be configured from init scripts.

## Supply-chain classification

### Source available and sufficient

- The exact 2.6.26 Lab126 kernel platform support, including board GPIO,
  battery, charger/PMIC integration, e-ink/Broadsheet/Papyrus, input, audio,
  RTC, USB gadget, CPU-frequency, and AR6002 host-driver code.
- BusyBox init/DHCP, `wpa_supplicant`, ALSA userspace, FBInk, eSpeak NG, and
  other normal open-source userland selected by Paper Linux.
- The ABI definitions needed to replace the two Atheros loader programs.

### Opaque but required

- AR6002 `athwlan.bin.z77` target firmware.
- AR6002 firmware data patch and Lab126 RF board/regulatory data.
- Per-device MAC/identity data held in IDME.
- The installed panel-specific e-ink command/waveform image. It can remain in
  controller flash, but should be backed up for recovery.
- Panel EEPROM/VCOM calibration data, which should be read rather than guessed.

These should be treated as firmware assets with explicit provenance, hashes,
permitted redistribution status, and a device-extraction path. Do not silently
mix them into ordinary source-built packages.

### Proprietary but replaceable or irrelevant

- `libgasgauge.so`: superseded by the released battery sysfs driver.
- Amazon IPC/logging/crash libraries and native daemon implementations.
- Nuance TTS engine and voice data.
- GStreamer-based `audioServer`, Audible extensions, Amazon cloud clients,
  browser services, and Java framework.
- Cellular modem services on a Wi-Fi-only target.

### Build/toolchain risk

The vendor kernel is old and remains a platform dependency even though its
source is available. Preserve a reproducible ARM EABI kernel toolchain, the
exact kernel config, module ABI, and any local kernel patches. Paper Linux's
musl userland can use the kernel ABI directly; it does not need to share the
kernel build libc or run old glibc daemons.

For a transitional image, the two Atheros loader binaries are glibc-linked.
Run them from a small compatibility payload or retain the stock rootfs until
their narrow ioctl behavior is replaced. Nothing else identified here forces
the independent OS to retain an old glibc userspace.

## Recommended bring-up order

1. Archive per-device recovery data: IDME fields, e-ink ROM, panel EEPROM,
   VCOM, and hashes of every AR6002 payload.
2. Reproduce the stock kernel/module load order from a small board init script
   and validate fbdev, evdev, ALSA, RTC, charger sysfs, and USB networking with
   all Amazon daemons stopped.
3. Implement `paper-powerd` using uevents and sysfs, with critical-battery
   shutdown, named suspend-inhibit leases, RTC wake, and `/sys/power/state`.
4. Implement the network lease daemon around `wpa_supplicant` and `udhcpc`.
   Initially invoke the preserved AR6002 loader payload, then replace
   `bmiloader`/`eeprom.AR6002` with source-built equivalents.
5. Add optional USB storage-mode policy and audio arbitration only after the
   basic hardware path is stable.

The independent-OS blockers are therefore much smaller than the stock daemon
list suggests: safe power policy must be rewritten, and Wi-Fi firmware
bring-up must be packaged or reimplemented. Nearly everything else is already
behind a usable kernel ABI.

## External references

- Amazon's official Kindle source-code page provides the exact Kindle 3.4.3
  source archive: <https://digprjsurvey.amazon.com/csad/help/node/200203720>
- Linux Wireless documents the host/target split and binary-only AR600x target
  firmware: <https://wireless.docs.kernel.org/en/latest/en/users/drivers/ath6kl/architecture.html>
- Linux Wireless's supported-device page is useful when evaluating, but not
  assuming, an upstream-driver migration:
  <https://wireless.docs.kernel.org/en/latest/en/users/drivers/ath6kl.html>
