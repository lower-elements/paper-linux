# Kindle 3 power-management evidence

This document records what is known about power regulation on the Amazon
Kindle 3 (Lab126 Luigi platform), with particular attention to the Wi-Fi-only
Shasta board tested during mainline bring-up. Its purpose is to keep future
Device Tree and driver work conservative: a plausible rail assignment is not
treated as a fact, and a programmed voltage is not treated as a measurement.

The conclusions below combine:

- read-only inspection of a running stock Kindle;
- the GPL Lab126 Linux 2.6.26-rt source in `../lab126/linux-2.6.26`;
- Barebox's Kindle 3 board support;
- component manufacturers' documentation; and
- board photographs and third-party teardowns.

No public Kindle 3 board schematic was found. Consequently, several physical
loads remain deliberately unidentified even when their regulator name and
programmed state are known.

## Confidence language

The following terms have specific meanings in this document:

- **Confirmed** means directly supported by the inspected stock system and/or
  an explicit assignment in Lab126's board code.
- **Corroborated** means that two independent sources agree, but the physical
  PCB trace has not been measured.
- **Hypothesis** means the electrical values and component requirements fit,
  but no source explicitly identifies the connection.
- **Unknown** means there is not enough evidence to assign a load safely.

These distinctions matter. A regulator's selector says what the PMIC was
asked to produce; it is not a voltmeter reading at the component. Similarly,
Linux regulator `enabled_count` is driver-consumer bookkeeping and is not
necessarily the PMIC's hardware enable bit.

## Inspected hardware and software

The live observations in this document came from a Wi-Fi-only Kindle 3 running
the stock firmware:

- kernel: `2.6.26-rt-lab126 #5`, built 2012-09-01;
- firmware: `3.4.3`, build `001-S403_luigi-354362`;
- board-ID prefix: `SP1B`, decoded by the vendor board-ID macros as Shasta,
  PVT, hardware revision 1, 256 MiB;
- Wi-Fi serial family prefix: `B008`; and
- no 3G modem.

Only non-unique prefixes are recorded here. The full device serial and board
ID are intentionally omitted.

The Lab126 tree is shared by several Luigi variants and contains WAN/modem
code even on Wi-Fi-only hardware. The mere presence of that code or of WAN
sysfs entries is not evidence that a rail is populated on this board.

## Power architecture

There is more than one power-management device:

1. The **Freescale/NXP MC13892AJVL** is the primary system PMIC. It contains
   four buck converters, multiple LDOs, battery charging, an ADC, an RTC and
   power-control logic. See the [MC13892 data
   sheet](https://www.nxp.com/docs/en/data-sheet/MC13892.pdf) and [NXP product
   page](https://www.nxp.com/products/power-management/pmics-and-sbcs/5-v-pmic-solutions/power-management-ic-pmic-for-i-mx35-51%3AMC13892).
2. An I2C device called **Papyrus** by the vendor driver controls the e-ink
   panel's specialised high-voltage supplies, VCOM and temperature sensing.
   It is separate from the MC13892. The exact TI part number is not established
   by the available code or live identification.
3. An I2C fuel gauge at address `0x55` is exposed as `Luigi_Battery`. Its exact
   part number is not established, so this document does not guess one.

The live kernel logged `mc13892 Rev 2.1 FinVer 2 detected`, registered the PMIC
regulator driver, initialized the RTC as `rtc0`, and exposed the PMIC character
device. Board photographs independently identify an MC13892AJVL package; see
the [Electronics360 teardown](https://electronics360.globalspec.com/article/3199/amazon-kindle-gen-3-wireless-ebook-reader-teardown)
and [Wikimedia board photograph](https://commons.wikimedia.org/wiki/File%3AAmazon_Kindle_3_%28model_D00901%29_-_board_-_Freescale_Semiconductor_MC13892AJVL-0520.jpg).

## MC13892 connection

Lab126 registers the MC13892 on the i.MX35's first CSPI controller, chip select
0, active-high, with a maximum SPI clock of 4 MHz. Its interrupt is routed via
`ATA_IORDY`, used as GPIO2_12. The evidence is in
`arch/arm/mach-mx35/mx35_luigi.c` (`mxc_spi_board_info`) and the associated pin
setup.

Barebox independently describes the same physical controller, chip select and
polarity in `arch/arm/boards/kindle3/kindle3.c`. Barebox numbers that controller
`spi0`/bus 0 whereas the vendor Linux tree calls it CSPI1/bus 1; this is a
software-numbering difference, not different wiring. Barebox probes the PMIC
but does not encode the Kindle's complete regulator policy, so it corroborates
the SPI wiring only.

## Reproducible live snapshot

The PMIC snapshot below was read through the stock kernel's read-only
`PMIC_READ_REG` ioctl. The helper had no write operation. Conditions were:

- normal awake operation;
- CPU at 512 MHz under the `ondemand` governor (available states 256/512 MHz);
- Wi-Fi enabled;
- USB attached; and
- the stock system reporting charging.

Those conditions are essential context: charger registers and dynamic rails
can differ at another instant or in suspend.

```text
register  value
24        0x456318
25        0xc501ce
26        0x807380
27        0x80739c
28        0x202044
29        0x001800
30        0x024dd0
31        0x0001d8
32        0x01b008
33        0x180000
34        0x018200
48        0x810323
49        0x000040
50        0x000000
```

The decode below follows the Rev. 19 MC13892 data sheet's register map and
voltage tables. These raw values are evidence, not a configuration template.
Writing them wholesale from mainline would bypass sequencing, board-revision
logic and charger policy.

For auditing the decode: registers 24-27 contain the SW1-SW4 voltage
selectors; register 28 contains the SW1/SW2 operating modes; register 29
contains the SW3/SW4 modes and SWBST control; registers 30-31 contain the LDO
voltage selectors; registers 32-33 contain the LDO enables and standby/mode
bits; and register 34 controls GPO/PWGT outputs. Registers 48-50 belong to the
charger/USB block. Bit names and polarities come from the MC13892 data sheet,
not from inference based on the numeric values.

## Initial mainline RAM-boot snapshot

A second read-only snapshot was taken from the initial mainline bring-up image
running Linux 7.0.11. The conditions differed from the stock snapshot:

- the kernel and initramfs had been loaded into RAM by Barebox;
- no eMMC partition was mounted;
- Wi-Fi was not enabled and no Wi-Fi driver was present;
- the CPU clock was 532 MHz;
- USB ACM and ECM gadgets were active, with the inspection performed over the
  ECM link; and
- the regulator framework driver was intentionally disabled.

The MC13892 core identified the same hardware as the stock kernel: revision
2.1, FinVer 2, Fab 0. The RTC, ADC and power-button children bound successfully.
The LED and regulator children remained unbound. `/sys/class/regulator`
therefore contained only Linux's dummy regulator, not representations of the
MC13892 outputs.

The live MC13892 power registers were:

```text
register  value
24        0x456318
25        0xc5294a
26        0x80739c
27        0x80739c
28        0x21284a
29        0x100a0a
30        0x024dd0
31        0x0001d8
32        0x049208
33        0x040000
34        0x218000
48        0x400003
49        0x000040
50        0x000009
```

Registers 30 and 31, which contain the LDO voltage selectors, matched the
stock snapshot. The enable and switcher registers did not. The directly
decodable differences were:

| Rail | Initial mainline RAM-boot state | Stock awake comparison |
| --- | --- | --- |
| SW1 | 1.20 V; PWM pulse-skipping in normal and standby states | Same voltage, but stock Wi-Fi-on state used Auto normal/off standby |
| SW2 | 1.35 V normal, DVS and standby; PWM pulse-skipping | Stock was 1.45 V normal with distinct DVS/standby selectors and Auto/Auto |
| SW3 | 1.80 V; PWM pulse-skipping | Stock held SW3 off |
| SW4 | 1.80 V; PWM pulse-skipping | Same voltage; stock used Auto/Auto |
| SWBST | Enabled | Stock disabled it |
| VIOHI | Enabled | Same |
| VDIG | Enabled at its 1.25 V selector | Stock disabled it |
| VGEN2 | Enabled at 3.15 V | Same; this keeps the eMMC supply present |
| VPLL | Enabled at its revision-dependent selector | Same enable and selector |
| VUSB2 | Enabled at 2.60 V | Stock disabled it |
| VSD | Enabled at 3.15 V | Stock disabled it |

The MC13892 data sheet defines switcher mode `0xa` as PWM pulse-skipping in
both normal and standby states. It is not an enable bit that can be interpreted
independently of the mode table.

These values are an **observed inherited/startup state**, not mainline's chosen
board policy. `CONFIG_REGULATOR_MC13892` was disabled, and inspection of the
bound mainline drivers found no writes to switcher or LDO registers 24-34:

- the MFD core sets `WDIRESET` in power-control register 15 and manages IRQ
  mask/status registers;
- the power-button child configures button fields in register 15;
- RTC accesses are confined to the RTC block; and
- ADC conversions temporarily use ADC registers 43-45.

The power registers were identical before and after reading every exported ADC
channel. Barebox's Kindle 3 board code registers and identifies the PMIC but
does not establish Amazon's regulator policy either. This combination strongly
supports treating the values as state inherited from PMIC startup and the
preceding recovery/boot path. It does not prove which earlier state transition
produced every bit.

### Mainline ADC, RTC and wake observations

The ADC driver exposed its attributes on the `mc13892-adc` platform device.
The first live sample reported:

- `in2_input`: 4010 mV battery-path reading;
- `temp1_input`: 31790 millidegrees Celsius (31.79 degrees Celsius);
- `in16_input`: 2353 mV from the internally selected UID channel; and
- general ADIN readings on channels 5-7 and 12-15.

Only the battery-path and die-temperature conversions have a generic meaning
that is useful without board-specific channel assignments. The remaining
numbers do not establish physical consumers or signal names.

The RTC registered as `rtc0` and initialized the system clock, but contained
2025-12-18 while the inspection occurred in August 2026. Its persistence works;
its time was stale and should not be used as evidence of accuracy. The PMIC
power button generated interrupts and input events, but neither it nor the RTC
was registered as a wakeup source. Only the matrix keypad and eMMC host appeared
in the kernel wakeup-source list. Wake-from-suspend is therefore not wired yet.

### Upstream regulator-driver revision hazard

Amazon's regulator driver treats the MC13892 VPLL selector value 2 as 1.50 V
on PMIC revision 2.0 or newer and as 1.65 V on earlier silicon. The inspected
part is revision 2.1. Upstream `mc13892-regulator.c` has a single fixed table
that labels selector 2 as 1.65 V. Git history shows that table was present in
the [driver's original 2010 commit](https://github.com/torvalds/linux/commit/5e428d5cecc3f109b52e993a1bd91f82137867b3),
which was tested on an i.MX51 Babbage board; it is not a later correction based
on observed MC13892 behaviour.

The Kindle 3 mainline patch layer retains the existing table for pre-2.0
silicon and selects the data-sheet table for revision 2.0 or newer. This only
corrects Linux's selector-to-voltage mapping and does not write a voltage
selector during probe. More generally, binding the driver is only the
mechanism for exposing regulators; Device Tree constraints and consumer links
are still required to express safe Kindle policy and prevent the regulator
core from disabling an apparently unused but physically required rail. The
initial regulator bring-up therefore registers only VGEN2, constrains it
to its observed 3.15 V boot state, marks it always on, and connects it to the
eMMC controller. All other PMIC rails remain unregistered and retain their
bootloader-programmed state.

## Awake regulator state and known consumers

| Rail | Programmed state in snapshot | Consumer or status | Confidence |
| --- | --- | --- | --- |
| SW1 | 1.20 V; Auto in normal state, off in standby | Atheros Wi-Fi core rail | Confirmed by `wifi.c` and live state |
| SW2 | 1.45 V; Auto/Auto | i.MX35 CPU core rail | Confirmed by `cpufreq.c`; voltage decoded from live register |
| SW3 | 1.10 V selector, but mode off/off | No active Luigi consumer found | Confirmed off; load unknown/unused |
| SW4 | 1.80 V normal and standby; Auto/Auto, PFM in memory hold | Physical consumers not proved | Voltage/state confirmed; load unknown |
| VIOHI | Enabled | Physical consumers not proved | State confirmed; load unknown |
| VPLL | 1.50 V, enabled and standby-controlled | Intended PLL supply domain | Corroborated by name, initialization and i.MX35 requirement; PCB trace unmeasured |
| VDIG | 1.25 V, disabled | None identified | State confirmed |
| VGEN1 | 1.20 V, disabled | Accessory supply only on Shasta EVT1 | Confirmed not the PVT1 accessory path |
| VGEN2 | 3.15 V, enabled and standby-controlled | Soldered eMMC supply | Corroborated by explicit `power_mmc = "VGEN2"`, live PMIC state and working eMMC |
| VUSB2 | 2.60 V, disabled | No active load identified | State confirmed |
| VGEN3 | 2.90 V, disabled | No active load identified | State confirmed |
| VCAM | 2.75 V, disabled | No active load identified | State confirmed |
| VVIDEO | 2.50 V, disabled | No active load identified | State confirmed |
| VAUDIO | 2.50 V, disabled | Not used by the active WM8960 codec code | State confirmed; do not assign it to the codec |
| VSD | 3.15 V selector, disabled; standby/mode bits set | External/removable SD path, not soldered eMMC | Corroborated by SDHCI driver behaviour and live state |
| SWBST | Disabled | No active load identified | State confirmed |
| USB regulator/pass path | Disabled in register 50 | Charger/USB policy is dynamic | Snapshot only |
| GPO1-GPO4 | All off; GPO2 standby bit set | GPO2/GPO4 participate in accessory power on this board revision | Assignment confirmed; snapshot is no-accessory state |
| PWGT1/PWGT2 | Disabled by their active-low SPI enable bits | No active load identified | State confirmed |

The stock regulator sysfs directory listed the same MC13892 outputs. It
reported switcher values such as `1200` and `1450` through files named as if
they contained microvolts. The vendor driver actually returns millivolts for
these entries; the MC13892 register decode is the authoritative unit check.
Sysfs `enabled_count` also disagreed with raw hardware state for VGEN2, which
is why consumer counts must not be used to reconstruct boot state.

### SW2 voltage discrepancy

Lab126 comments and symbolic setup describe a nominal 1.40 V CPU setting. The
running PMIC contained SW2 selector 14 with the high-range bit set. Under the
official MC13892 table this is 1.45 V, and the stock sysfs view also reported
`1450`. That value is inside the i.MX35's 532 MHz CPU supply range of
1.33-1.47 V in the [i.MX35 data
sheet](https://www.nxp.com/docs/en/data-sheet/MCIMX35SR2CEC.pdf). The source
comment and encoded value therefore disagree; mainline should not silently
copy the comment as a regulator constraint.

## Vendor initialization policy

`arch/arm/mach-mx35/dvfs.c` is the main source for Luigi's initial MC13892
policy. Despite its filename, its comments state that Luigi uses CPUFreq rather
than the i.MX35 DVFS implementation. The executable initialization does the
following:

- holds SW1, the Wi-Fi rail, off until the Wi-Fi driver requests it;
- configures SW2 for CPU operating, DVS and standby values;
- leaves SW3 off;
- configures SW4 for 1.80 V in both normal and standby state;
- enables VIOHI;
- disables VDIG, VUSB2 and SWBST;
- gives VPLL, VGEN2 and VSD standby behaviour; and
- enables PMIC watchdog-reset handling and power-key debounce.

An introductory comment in that file mentions 1.65 V for SW4 in suspend, but
the executable setup explicitly programs 1.80 V for SW4 standby and the live
register agrees. The executable code and hardware state are stronger evidence
than the stale comment.

## eMMC power and signalling

The soldered Samsung eMMC identifies as `M4G1EM` (manufacturer `0x15`). A
teardown identifies the package as KLM4G1EEHM-B101. Its [manufacturer data
sheet mirror](https://datasheet4u.com/pdf/749070/KLM4G1EEHM-B101.pdf) specifies:

- flash-array supply VDDF: 2.7-3.6 V;
- interface/controller VDD: either 1.70-1.95 V or 2.7-3.6 V;
- 1-, 4- and 8-bit buses; and
- operation up to 52 MHz.

The stock kernel logged an 8-bit, high-speed eMMC connection. In
`mx35_luigi.c`, eSDHC1/controller 0 has `power_mmc = "VGEN2"`, an OCR range of
3.1-3.3 V and a maximum clock of 52 MHz. The Shasta board-revision macros
enable the extra DAT4-DAT7 pins except on EVT1. Those pins are multiplexed
from FEC pads when Ethernet is absent.

The selected stock driver is `CONFIG_MMC_IMX_ESDHCI=y`, implemented by
`drivers/mmc/host/mx_sdhci.c`. It does not consume each controller's
`power_mmc` string. Instead it obtains the global VSD regulator while probing
controllers, then disables VSD after controller 2 finds no removable card.
Its suspend/resume VSD handling is restricted to controller 2. The resulting
live state is decisive:

- VGEN2 is enabled at 3.15 V;
- VSD is off; and
- the soldered eMMC is operating in 8-bit high-speed mode.

Therefore VSD must not be modelled as the Kindle 3 eMMC's required supply.
VGEN2 is the confirmed board-level eMMC supply assignment.

The vendor pin setup applies SION to SD1 CLK, CMD and DAT0-DAT3 and selects
the i.MX35 1.8 V output-driver characteristics for all eight data signals.
The [i.MX35 reference
manual](https://www.nxp.com/docs/en/reference-manual/IMX35RM.pdf) is explicit
that this DVS pad bit changes driver characteristics; it does **not** change
the pad's physical supply. It must agree with the external I/O supply.

It is therefore a strong **hypothesis**, not a proved trace, that VGEN2's
3.15 V feeds the eMMC flash-array rail while an active 1.8 V rail such as SW4
feeds the eMMC interface and the i.MX35 NVCC_SDIO bank. This fits the eMMC
limits, SW4's live state and the vendor pad setting, but only a schematic or
measurement can identify that second connection.

The eMMC data sheet also recommends keeping VDDF stable for at least 500 ms
after a CPU reset during a write, to reduce corruption risk. Mainline must not
turn VGEN2 off during reboot or experimental shutdown until that sequencing is
understood.

## Wi-Fi power and signalling

The stock kernel detected its Atheros AR6000-family Shasta device on `mmc1`.
Board inspection identifies an AR6102G-BM2D module containing an AR6002GZ.
Lab126's `arch/arm/mach-mx35/wifi.c` explicitly obtains SW1, fixes it at
1.20 V, enables it before releasing the module's GPIO power/reset sequence,
and reverses the order on shutdown. This makes SW1's role a confirmed mapping.

The same code identifies:

- module power enable: `ATA_DATA5` used as a GPIO; and
- module reset: `ATA_DATA7` used as a GPIO.

The archived [Atheros AR6102 data
sheet](https://www.dzsc.com/uploadfile/company/193739/201192094443150.pdf)
specifies 1.14-1.26 V for its 1.2 V core rails, 1.71-1.89 V for VDD18,
1.71-3.46 V for SDIO/GPIO/BT I/O, and 3.0-3.6 V for VCC_FEM. It also requires
the I/O supplies to rise together and reset/power to be released only after
supplies are stable. SW1 at 1.20 V is safely inside the core range.

The vendor eSDHC2 platform entry says `power_mmc = "SW1"`, OCR 3.1-3.3 V and
25 MHz. That OCR value is not a measurement of the SDIO I/O rail, and the
selected vendor SDHCI driver ignores the per-controller `power_mmc` field.
The GPIO pads are configured with 1.8 V driver characteristics. It is
electrically plausible that SW4 supplies Wi-Fi VDD18/SDIO I/O and another
3.3 V source supplies VCC_FEM, but no available source traces either load.
Both remain hypotheses and must not become regulator links in the Device Tree
without more evidence.

The unused `mxc_unifi_platform_data` path in the vendor tree describes a CSR
UniFi configuration selected by `CONFIG_SDIO_UNIFI_FS`; it is not the active
Atheros hardware path and must not be used as Kindle 3 evidence.

## Accessory power

`arch/arm/mach-mx35/mx35_accessory.c` contains explicit board-revision policy:

- VGEN1 is used only on Shasta EVT1, not the inspected PVT1 board;
- GPO2 is the main accessory-power path on later revisions;
- GPO4 is required by Shasta DVT/PVT accessory/charging logic; and
- GPO2 is configured to turn off in standby.

With no accessory connected, the live snapshot had GPO1-GPO4 off and the
GPO2 standby bit set. Any mainline accessory implementation must retain the
board-revision conditions and charger interaction rather than exposing these
GPOs as unconditional user-controlled regulators.

## Display power

The stock system exposed a Papyrus device at I2C address `0x48`. The vendor
driver is `drivers/video/eink/broadsheet/broadsheet_papyrus.c`; it manages
panel power sequencing, power-good state, temperature and VCOM. The inspected
unit reported:

- panel ID `V220_052_60_M24`;
- VCOM `-2.12 V`; and
- an e-ink power-timer delay of 2000 ms.

These are live device values, not MC13892 rails. The Electronics360 teardown
identifies a separate TI power-management device near the display circuitry,
but does not establish the exact part number. Mainline display work should use
the Papyrus driver's sequencing as evidence and must not infer that MC13892
VAUDIO, VVIDEO or another conveniently named LDO powers the panel.

## Audio, battery and charger boundaries

The audio codec was present at I2C address `0x1a` and is identified by source
and teardown as a Wolfson WM8960. The active codec/board path does not request
MC13892 VAUDIO, and VAUDIO was off in the snapshot. There is no evidence for
linking VAUDIO to the codec.

The stock battery interface is custom sysfs rather than Linux
`/sys/class/power_supply`. Lab126 registers its `Luigi_Battery` device on the
first I2C bus at address `0x55`, and its register choices and 20 mOhm sense
resistor match the TI BQ27210 data sheet. Independent physical inspection of a
Kindle 3 battery identifies the ten-pin gauge marking as `27210`; see the
[Kindle 3 repair investigation](https://hackaday.io/project/202390-kindle-3-repair)
and the [TI BQ27210 data sheet](https://www.ti.com/lit/ds/symlink/bq27210.pdf).
A teardown identifies the battery pack as S11GTSF01A, 3.7 V, 1750 mAh.
Instantaneous charge, current and capacity readings are operating state, not
fixed board properties.

Mainline's `bq27xxx-battery` driver binds successfully using the
`ti,bq27210` compatible. A live RAM-boot test reported a present, healthy
battery at 4.181 V and 22.9 degrees C, 100% capacity, approximately 1.47 Ah
learned full capacity and 16 cycles, without I2C errors. The initial Device
Tree deliberately omits `monitored-battery`, and
`CONFIG_BATTERY_BQ27XXX_DT_UPDATES_NVM` remains disabled, so this enablement
does not update the pack gauge's EEPROM.

The USB gadget driver (`drivers/usb/gadget/arcotg_udc.c`) participates in a
custom MC13892 charger state machine. USB was attached and charging during the
register snapshot, so registers 48-50 are not an unplugged baseline. See
[`docs/kindle-3-hardware-enablement.md`](../../../docs/kindle-3-hardware-enablement.md)
for the broader stock charger, suspend and userspace-control survey.

## Suspend, reboot and power-off safety

The stock platform's power-off path in `mx35_luigi.c` coordinates RTC alarm
state, PMIC revision handling, regulator policy and the MC13892 user-off
mechanism. It is not equivalent to setting a single `power-off` bit. Suspend
also relies on programmed standby modes and state pins.

For conservative mainline work:

1. Do not change regulator voltages merely to test a guessed consumer.
2. Do not disable VGEN2 while eMMC may be active, including during reboot.
3. Keep SW1 sequencing inside the Wi-Fi power path; do not mark it always-on.
4. Preserve SW4 at 1.80 V in normal and standby operation until its loads are
   identified.
5. Preserve the vendor relationship between pad driver voltage selection and
   the actual I/O bank voltage; the pad bit alone cannot establish the rail.
6. Do not copy one awake register snapshot into boot code.
7. Add fixed constraints only for confirmed assignments. Leave unknown rails
   unmanaged rather than inventing consumer links.
8. Treat regulator-disable, suspend and power-off testing as potentially more
   destructive than read-only boot testing, especially with mounted eMMC.

## What remains unknown

The following questions require a public schematic, high-resolution trace
work, safe electrical measurement, or additional authoritative source code:

- the complete consumer list for SW4 and VIOHI;
- the physical source for eMMC VDD/interface and i.MX35 NVCC_SDIO;
- the sources for the Wi-Fi module's VDD18/SDIO and 3.3 V FEM rails;
- whether any populated load uses the disabled general-purpose LDOs;
- the exact Papyrus/TI display-PMIC model;
- all accessory-connector rail destinations; and
- a complete suspend-state voltage and current profile.

These are not safe gaps to fill by analogy with another i.MX35 board.

## Evidence index

### Lab126 GPL source

All paths below are relative to the external `../lab126/linux-2.6.26` source
checkout and are not vendored into this repository:

- `arch/arm/mach-mx35/mx35_luigi.c`: PMIC SPI registration, eSDHC platform
  data, board revision handling and platform power-off.
- `arch/arm/mach-mx35/dvfs.c`: initial MC13892 switcher/LDO modes and standby
  policy.
- `arch/arm/mach-mx35/cpufreq.c`: SW2 CPU-core consumer and 256/512 MHz policy.
- `arch/arm/mach-mx35/wifi.c`: SW1 Wi-Fi consumer and GPIO power/reset order.
- `arch/arm/mach-mx35/mx35_accessory.c`: revision-dependent VGEN1/GPO2/GPO4
  accessory policy.
- `arch/arm/mach-mx35/mx35_3stack_gpio.c`: eSDHC pin muxing, SION and pad
  driver-voltage selection.
- `drivers/mmc/host/mx_sdhci.c`: actual VSD acquisition and controller-2
  removable-slot handling.
- `drivers/video/eink/broadsheet/broadsheet_papyrus.c`: display-PMIC power,
  VCOM, temperature and power-good handling.
- `drivers/usb/gadget/arcotg_udc.c`: USB/charger state machine.
- the built stock `.config`: selected SDHCI and Atheros paths.

### Mainline source inspected during RAM boot

The paths below refer to the Linux 7.0.11 source built by Buildroot and are
recorded by upstream path rather than by the generated `output/` location:

- `drivers/mfd/mc13xxx-core.c`: PMIC identification, IRQ handling, WDIRESET
  setup and ADC conversions.
- `drivers/power/supply/bq27xxx_battery.c` and
  `drivers/power/supply/bq27xxx_battery_i2c.c`: upstream BQ27210 power-supply
  interface and I2C transport.
- `drivers/regulator/mc13892-regulator.c`: regulator register definitions,
  voltage tables and the fixed 1.65 V VPLL selector interpretation.
- `drivers/hwmon/mc13783-adc.c`: exported ADC channels and their unit
  conversions.
- `drivers/input/misc/mc13783-pwrbutton.c`: PMIC button configuration and
  input events.
- `drivers/rtc/rtc-mc13xxx.c`: RTC and alarm handling.

### Manufacturer documentation

- [NXP MC13892 data sheet, Rev. 19](https://www.nxp.com/docs/en/data-sheet/MC13892.pdf)
- [NXP MC13892 product page](https://www.nxp.com/products/power-management/pmics-and-sbcs/5-v-pmic-solutions/power-management-ic-pmic-for-i-mx35-51%3AMC13892)
- [NXP i.MX35 reference manual](https://www.nxp.com/docs/en/reference-manual/IMX35RM.pdf)
- [NXP i.MX35 data sheet](https://www.nxp.com/docs/en/data-sheet/MCIMX35SR2CEC.pdf)
- [Samsung KLM4G1EEHM-B101 preliminary data sheet mirror](https://datasheet4u.com/pdf/749070/KLM4G1EEHM-B101.pdf)
- [Atheros AR6102 data sheet archive](https://www.dzsc.com/uploadfile/company/193739/201192094443150.pdf)

The Samsung and Atheros links are third-party mirrors of manufacturer
documents because stable first-party copies were not located.

### Board identification and further research

- [Electronics360 Kindle 3 teardown](https://electronics360.globalspec.com/article/3199/amazon-kindle-gen-3-wireless-ebook-reader-teardown)
- [FCC filing XSX-1013](https://fccid.io/XSX-1013) and [internal
  photographs](https://fccid.io/XSX-1013/Internal-Photos/Internal-Photos-1295209)
- [iFixit Kindle 3 teardown](https://www.ifixit.com/Teardown/Kindle%2B3%2BTeardown/6540)
- [DeviWiki Kindle Keyboard Wi-Fi hardware page](https://deviwiki.com/wiki/Amazon_Kindle_Keyboard_Wi-Fi_%28D00901%29)
- [Wikimedia MC13892 board photograph](https://commons.wikimedia.org/wiki/File%3AAmazon_Kindle_3_%28model_D00901%29_-_board_-_Freescale_Semiconductor_MC13892AJVL-0520.jpg)
- [ElectricStuff Kindle hardware notes](https://www.electricstuff.co.uk/kindlehack.html)
- [Matt Brown, Under the cover of the Kindle 3](https://www.mattb.nz/w/2010/12/07/under-the-cover-of-the-kindle-3/)
- [MobileRead Kindle 3 hardware thread](https://www.mobileread.com/forums/showthread.php?t=96451)
- [MobileRead Luigi board and kernel discussion](https://www.mobileread.com/forums/showthread.php?p=2018953)
- [Kindle 3 stock kernel configuration tour](https://linux-tipps.blogspot.com/2011/05/kindle-3-discovery-tour-kernel-config.html)
- [Alternate AR6002 hardware document archive](https://www.akkit.org/info/AR6002-Atheros.pdf)
- [Barebox Kindle 3 documentation](https://www.barebox.org/doc/latest/boards/imx/amazon-kindle-3.html)
- [Original Barebox Kindle 3 support posting](https://lists.infradead.org/pipermail/barebox/2016-July/027696.html)

The Electronics360 teardown includes a 3G variant. Its component markings and
board photographs are useful corroboration, but optional modem circuitry is
not assumed to exist on the inspected Wi-Fi-only unit.
