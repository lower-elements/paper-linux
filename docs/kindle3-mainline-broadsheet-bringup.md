# Kindle 3 Broadsheet first-light investigation

This report traces the known-working Kindle 3 display path in Amazon's
`2.6.26-rt-lab126` source and maps it onto Linux 7.0's existing
`broadsheetfb` driver. Its scope is deliberately narrow: instantiate the
current fbdev driver, retain its deferred-I/O update behaviour, and make one
safe display update. It does not propose a new userspace ABI, explicit refresh
ioctls, different deferred I/O, or DRM.

The source trees examined are:

* Amazon Kindle 3.4.3: external kernel-history ref
  `archive/amazon-kindle3-3.4.3`
* modern mainline: external resource `linux-stable-7.0.11`
* Paper Linux board DT: `board/amazon/kindle3/mainline/linux-dts/nxp/imx/imx35-kindle3.dts`

The most important result is that **Broadsheet is not connected through the
i.MX35 WEIM**. Amazon uses the asynchronous display-controller portion of the
old i.MX35 IPU, called “ADC” by Freescale's 2.6.26 driver. The IPU drives a
16-bit System-80 Type 2 bus on the LCD pins and generates chip select,
command/data select, write, and read strobes. Reset and ready/busy are separate
GPIOs. Modern mainline has the WEIM driver but no longer has an implementation
of this i.MX35 IPU/ADC interface, so a small host-interface driver is required.

There is one critical qualification. Amazon's driver supports both product
`0x0047` (S1D13521 “Broadsheet”) and product `0x004d` (“ISIS”) over this same
bus. The earlier live-unit survey in `docs/kindle-3-hardware-enablement.md`
records an ISIS runtime command set and a C052 85 Hz waveform on the inspected
Kindle 3. Because Amazon's `BS_ISIS()` test is based on the product register,
that is strong evidence—but not a preserved raw register reading—that this
unit is product `0x004d`. Linux 7.0's driver only implements the `0x0047`
initialization. The read-only product probe is therefore a hard branch in the
plan: host-interface first light is tractable for either chip, but an ISIS unit
cannot safely proceed through the unmodified S1D13521 initialization sequence.

## 1. Amazon display-driver source map

Paths and line numbers in this section are relative to the Amazon tree.

| File | Relevant code | Contribution |
| --- | --- | --- |
| `drivers/video/eink/broadsheet/broadsheet.c` | `bs_panel_init()` at 3358, `bs_sys_init()` at 4025, identification around 4202, `broadsheet_needs_dma()` at 4233, `bs_sw_init_controller()` at 4282, `bs_sw_init_panel()` at 4355, flash access around 4924 | Broadsheet command protocol, identification, panel/controller initialization, image/update operation, and serial-flash access. |
| `drivers/video/eink/broadsheet/broadsheet_def.h` | identity at 93–104, panel tables at 173–218, flash offsets at 319–327 | S1D13521 product/revision values, panel timings, and persistent-flash layout. |
| `drivers/video/eink/broadsheet/broadsheet_waveform.c` and `broadsheet_eeprom.c` | waveform parsing/version selection and panel-data access | Decode persistent waveform/panel metadata used to choose size, rate, and VCOM. |
| `drivers/video/eink/broadsheet/broadsheet_commands.c` | command-image access helpers | Reads the persistent command area used by diagnostics/recovery; the ISIS runtime command array itself is embedded in `broadsheet.c:508–621`. |
| `drivers/video/eink/broadsheet/broadsheet_mxc.c` | 22–62 | Thin MXC hardware wrapper. It includes `controller_common_mxc.c`, fills the bus timing properties, and calls `controller_hw_init()`. |
| `include/asm-arm/arch-mxc/controller_common_mxc.c` | `controller_wr_which()` at 68, PIO transfers at 175–239 and 300–350, `controller_hw_init()` at 356 | The decisive host-interface layer. It translates command/data words to `ipu_adc_write_cmd()`, reads through `ipu_adc_read_data()`, polls ready, and configures the IPU ADC. |
| `drivers/mxc/ipu/ipu_adc.c` | `ipu_adc_write_cmd()` at 184, `ipu_adc_read_data()` at 229, `ipu_adc_init_panel()` at 319, interface timing at 464 | Freescale's low-level IPU asynchronous-display implementation. Writes the IPU low-level-access registers and programs System-80 bus format and timing. |
| `drivers/mxc/ipu/ipu_common.c` | IPU initialization and ADC channel enable around 294–318 | Enables the IPU ADC and DI blocks and their clocking. |
| `include/asm-arm/arch-mxc/ipu_regs.h` | 143–157 | Defines low-level-access registers and the `ADC_EN`/`DI_EN` bits. |
| `arch/arm/mach-mx35/devices.c` | 233–267 | Registers the old IPU platform device with its 4 KiB MMIO resource. |
| `arch/arm/mach-mx35/mx35_luigi_gpio.c` | Papyrus at 184–306, LCD/IPU mux at 744–785, Broadsheet GPIOs at 790–897 | Kindle board pin mux, reset, ready, optional interrupt, and external E-Ink PMIC GPIOs. |
| `arch/arm/mach-mx35/mx35_pins.h` | 236–259 | Old IOMUX definitions for LCD data/control pins. |
| `drivers/mxc/ipu/ipu_l126_detect.c` | `ipu_test_broadsheet()` at 55–90 | Early read-only detection: reset, issue `RD_REG`, read product register `0x0002`, accept `0x0047` or `0x004d`. |
| `drivers/video/eink/broadsheet/broadsheet_pmic.c` | initialization at 140–175, VCOM at 211–225, power states at 249–295 | Selects the Papyrus PMIC, applies panel VCOM, enables rails, waits for power-good, and enters standby/sleep. |
| `drivers/video/eink/broadsheet/broadsheet_papyrus.c` | 39–205 | Old-style I2C detection at address `0x48` and Papyrus register access. |
| `drivers/video/eink/broadsheet/broadsheet_hal.c` | initialization around 1075–1133 | Connects Amazon's framebuffer HAL to controller and panel initialization. |

Amazon does not describe this through platform data. Its E-Ink HAL and
Broadsheet module call board/SoC helpers directly; `broadsheet_mxc.c` even
includes the MXC implementation source. That is appropriate historical
context, not a pattern to retain in a modern driver.

## 2. Hardware wiring

### Host path

The source-backed data path is:

```text
broadsheet.c command/data word
        |
controller_common_mxc.c
        |
ipu_adc_write_cmd()/ipu_adc_read_data()
        |
i.MX35 IPU low-level-access registers
        |
IPU System-80 Type 2, 16-bit asynchronous bus
        |
Epson S1D13521 Broadsheet
```

`controller_hw_init()` selects `IPU_ADC_IFC_MODE_SYS80_TYPE2`, a 16-bit bus,
burst-write chip select, and no serial read/write mode
(`controller_common_mxc.c:356–365`). `broadsheet_needs_dma()` returns false on
this platform (`broadsheet.c:4233–4245`), so Amazon transfers every command,
argument, and image word using programmed I/O. The unused ADC SYS1 DMA/EOF IRQ
path is not needed for first light.

The i.MX35 IPU register resource is `0x53fc0000` through `0x53fc0fff`:
`AIPS2_BASE_ADDR` is `0x53f00000` and `IPU_CTRL_BASE_ADDR` adds `0x000c0000`
(`include/asm-arm/arch-mxc/mx35.h:210–223`); the platform resource is declared
in `arch/arm/mach-mx35/devices.c:233–260`. Within it:

| CPU physical address | Register | Purpose |
| --- | --- | --- |
| `0x53fc01b8` | `DI_DISP_LLA_CONF` | Selects DISP0 and command, data, or read transaction. |
| `0x53fc01bc` | `DI_DISP_LLA_DATA` | Writes or reads the 16-bit host word. |

These offsets are defined at `ipu_regs.h:143–144`; their use is visible at
`ipu_adc.c:184–232`. They are **IPU registers, not a CPU-visible Broadsheet
window**. There is consequently no Broadsheet physical base address, WEIM chip
select, or address-line decode to put in a child `reg` property.
Amazon selects IPU display port `DISP0`, which appears on the board as
`IPU_DISPB_CS0`; no WEIM chip-select timing/configuration register is involved.

### Pins and signal ownership

`gpio_lcd_active()` selects the IPU function for LD0 through LD15
(`mx35_luigi_gpio.c:744–772`). `slcd_gpio_config()` selects alternate function
1 on LD20 through LD23 (`mx35_luigi_gpio.c:777–785`). Linux 7.0's
`imx35-pinfunc.h:432–540` names the same functions explicitly:

| Broadsheet function | i.MX35 pad / modern mux function | Ownership |
| --- | --- | --- |
| Host data D0–D15 | LD0–LD15 / `IPU_DISPB_DAT_0`…`15` | IPU, 16-bit bidirectional host data bus. |
| Chip select | LD20 / `IPU_DISPB_CS0` | Generated by IPU DISP0. |
| Command/data (RS) | LD21 / `IPU_DISPB_PAR_RS` | Generated by IPU from command/data transaction type. |
| Write strobe | LD22 / `IPU_DISPB_WR` | Generated by IPU timing engine. |
| Read strobe | LD23 / `IPU_DISPB_RD` | Generated by IPU timing engine. |
| Hardware reset | LD17 / GPIO2_17 | GPIO output, active low. |
| Host ready | LD18 / GPIO3_24 | GPIO input, ready is high. |
| Host interrupt (defined but unused) | LD19 / GPIO3_25 | Optional GPIO interrupt. |

There are no host address pins in this scheme. Command versus data is selected
by the IPU transaction written to `DI_DISP_LLA_CONF`; CS, RS, RD, and WR are
then emitted automatically.

Amazon asserts reset low for 100 ms, drives it high, and waits 400 ms
(`mx35_luigi_gpio.c:881–887`). `controller_common_ready()` returns the raw
level of LD18 (`889–892`), so the source establishes both reset and ready
polarity. `controller_wait_for_ready()` polls this GPIO with a five-second
timeout (`controller_common_mxc.c:152–155`). The normal setup passes `NULL` as
the HIRQ handler (`controller_common_mxc.c:417`), so GPIO3_25 is not requested.

### Host timing and clocks

The production property table uses:

| Setting | Amazon value |
| --- | ---: |
| `DI_HSP_CLK_PER` | `0x00100010` |
| Read cycle / up / down / latch | 110 / 1 / 100 / 110 ns |
| Write cycle / up / down | 83 / 1 / 72 ns |
| IPU ADC pixel clock | 5 MHz |

The values come from `broadsheet_def.h:382–427` and are installed by
`broadsheet_mxc.c:56–62`. `ipu_adc_init_ifc_timing()` converts the nanosecond
values against the live IPU clock (`ipu_adc.c:464–532`), so a modern port
should use the acquired clock rate and reproduce that calculation rather than
paste clock-dependent register bit patterns.

Linux 7.0 still exposes the i.MX35 `ipu_gate`, parented by `hsp`, in
`drivers/clk/imx/clk-imx35.c:63–80,190`; it is clock index 55 in that driver's
legacy index table. It has no surviving i.MX35 IPU ADC driver or DT node.
Amazon's 24 MHz “input clock” used to choose Broadsheet panel tables is not the
same thing as the IPU HSP clock. The examined board code does not show a
software-controlled Broadsheet reference-clock signal, so its physical source
remains unresolved.

### Panel, orientation, and controller identity

Amazon identifies product `0x0047` as Broadsheet/S1D13521 and recognizes
revisions `0x0000`, `0x0100`, and `0x0200` as B00, B01, and B02 respectively
(`broadsheet_def.h:93–104`). It also recognizes product `0x004d` as the later
“ISIS” controller. Early detection accepts either product
(`ipu_l126_detect.c:55–90`). This proves the software's supported variants,
not which chip revision is fitted to every Kindle 3. First hardware access
must log revision register `0x0000` and product register `0x0002` before
continuing. The current mainline driver expects `0x0047`/`0x0100` and only
warns on a mismatch (`broadsheetfb.c:841–854`). An actual `0x004d` result is a
stop condition, not a revision to force through the S1D13521 path. The existing
warning-only behaviour should be tightened before automatic initialization so
that an ISIS Kindle cannot accidentally continue with the wrong sequence.

The glass is 800×600. Amazon exposes it as 600×800 portrait in its framebuffer
stack and sends a rotation command (`broadsheet.c:4142–4195,4371–4375`). The
mainline driver is an 800×600 landscape framebuffer and does not initialize
rotation. That is acceptable for first light: an image may be physically
rotated, but changing orientation or fbdev semantics is not required to prove
the bus and controller.

### Datasheet identification and controller differences

The following documents are identified by stable IDs in
[`external-resources.json`](../external-resources.json):

* `epson-s1d13521-hardware-spec-1.2`
* `epson-s1d13522-product-brief-1.3`
* `epson-s1d13522-module-spec-1.2`

The last document is for Epson's complete S1D13522 controller module rather
than the bare IC, but it contains the controller host-command and register
interface. It states that `REG[0002h]` defaults to product code `0x004d`.
Together with Epson's description of S1D13522 as code-named ISIS, this makes
the mapping definitive:

```text
0x0047  S1D13521  “Broadsheet”
0x004d  S1D13522  “ISIS”
```

The chips are related but not register-compatible replacements:

| Property | S1D13521 / `0x0047` | S1D13522 / `0x004d` |
| --- | --- | --- |
| Host interface | 16-bit Intel-80 indirect | 8/16-bit Intel-80 indirect or serial host |
| Display memory | External 16/32-bit mobile SDRAM, up to 64 MiB | 2 MiB stacked memory |
| Published display limit | Up to 2048×1536 at 50 Hz; larger at lower rates | Up to 1200×825 at 50 Hz; 800×600 up to 85 Hz |
| Waveform depth | Up to 5-bit grayscale | Up to 4-bit grayscale |
| Update engine | Programmable command sequencer, multiple/overlapping regions | Advanced sequencer, 15-region pipeline, automatic waveform selection |
| Extra facilities | Rotation, packed writes, external waveform flash | Adds PIP, cursor, transparency, direct pen/touch support, and serial host mode |
| Persistent boot | Loads instruction code from external serial flash | Also supports serial-flash automatic loading when configured for it |

The last row corrects a possible over-generalization: volatile host loading is
not an intrinsic requirement of every S1D13522 design. Epson's module manual
shows a preprogrammed-flash boot requiring only `INIT_SYS_RUN`. It is Amazon's
Kindle S1D13522 path that sends PLL parameters, streams a 1298-byte compiled
instruction set, and copies the selected waveform into internal SDRAM.

The earlier live dump provides a plausible explanation. Its physical command
area contains `P_0B.00`, matching Amazon's `cmd0047` recovery image, while the
active ISIS command set is the driver-embedded `V0303`. This suggests the
panel-associated flash carries S1D13521-format material that the S1D13522
cannot use as its runtime instruction set. That explanation is an inference;
the source proves Amazon's host-loading behaviour but not the board-design
reason for it.

The public command sets overlap substantially, which explains why Amazon can
share most of its driver, but several differences are dangerous for an
unmodified mainline probe:

* Both publish the commands mainline relies on for register access, burst SDRAM
  access, image loading, waits, waveform-info reads, and ordinary updates.
* S1D13521 defines `0x12`–`0x14` as low-level serial-flash operations. In the
  S1D13522 module command set, `0x12`/`0x13` are unavailable and `0x14` disables
  PIP. Mainline's waveform-flash sysfs operation must therefore never be
  exposed as working on ISIS.
* S1D13521 defines `0x37` as gate-driver clear; the public S1D13522 module set
  marks it reserved. Amazon's uploaded ISIS instruction set may define it, but
  issuing it before that upload is not justified.
* The S1D13521 waveform registers begin at `0x0350`; Amazon reads ISIS waveform
  state at `0x0390`.
* Amazon sends S1D13521 pixel data inverted and nibble-swapped, but sends ISIS
  data unchanged. Mainline's current burst conversion is S1D13521-specific.
* Amazon adds pixel-invert and automatic-waveform bits to the ISIS display
  configuration. Mainline sends the S1D13521 value.
* Amazon reserves S1D13522 SDRAM from `0x000afc80` for the waveform and starts
  the image buffer at `0x000efc80`. Mainline's generic `800*600*2` image-buffer
  address is not the source-backed ISIS layout.

Amazon's 83 ns write and 110 ns read cycles are conservative enough for the
published parallel-interface timing of both parts. The physical i.MX35 host
shim therefore does not need to vary by product; the divergence begins above
it in firmware loading, memory layout, display configuration, and pixel
formatting.

### External panel power

The display rails and VCOM are managed by a separate “Papyrus” I2C PMIC. The
old driver detects address `0x48`, applies panel-derived VCOM, enables the
rails, and waits for power-good. Its board signals are:

| Function | Pad / GPIO | Evidence |
| --- | --- | --- |
| PMIC interrupt | FEC_MDC / GPIO3_13, falling edge | `mx35_luigi_gpio.c:184–215` |
| Power good | STXFS5 / GPIO1_3, rising edge | `mx35_luigi_gpio.c:220–243` |
| Wake/enable/reset | FEC_MDIO / GPIO3_14 | `mx35_luigi_gpio.c:248–306` |

GPIO3_14 polarity varies with board revision in Amazon's source. The old I2C
driver probes adapters rather than declaring a board-specific bus, so the
source examined does not prove which physical I2C controller carries address
`0x48`. Nor does the name “Papyrus” alone prove an exact modern compatible.
Linux 7.0 has a TPS65185 regulator driver and binding, but the PMIC identity,
bus, board-revision polarity, rail names, and voltage constraints must be
confirmed before using it. No voltage should be guessed.

This PMIC is a genuine cold-boot dependency. A first diagnostic may preserve
already-enabled bootloader rails, but a reliable mainline boot must eventually
represent and sequence it. Controller communication can be tested before
energizing the panel; a physical update must not be attempted until power-good
and panel VCOM are known correct.

## 3. Amazon operations mapped to `broadsheetfb`

Linux 7.0 defines `struct broadsheet_board` in
`include/video/broadsheetfb.h:57–73`. Its later “MMIO” hooks are the right
semantic fit even though the S1D13521 is not directly memory mapped: each hook
can issue one IPU low-level-access transaction.

| Mainline operation | Kindle equivalent |
| --- | --- |
| `init()` | Acquire/map the i.MX35 IPU block, enable `ipu_gate`, apply the LCD/System-80 pinctrl state, initialize DISP0 as 16-bit SYS80 Type 2 with Amazon's read/write timings, acquire reset and ready GPIOs, then perform the 100 ms low + 400 ms post-reset sequence. |
| `wait_for_rdy()` | Poll GPIO3_24 until high, with the Amazon five-second timeout. |
| `mmio_write(BS_MMIO_CMD, value)` | Wait for ready, select a DISP0 command transaction in `DI_DISP_LLA_CONF`, then write the low 16 bits to `DI_DISP_LLA_DATA`. |
| `mmio_write(BS_MMIO_DATA, value)` | Wait for ready, select a DISP0 data transaction, then write the word. |
| `mmio_read()` | Wait for ready, select a DISP0 read transaction, and read the low 16 bits from `DI_DISP_LLA_DATA`. |
| `set_ctl()` | Not used; IPU generates CS/RS/RD/WR. Leave NULL. |
| `set_hdb()` / `get_hdb()` | Not used; LD0–LD15 are controlled by IPU. Leave NULL. |
| `setup_irq()` | Return success without requesting an IRQ for first light. Amazon also leaves HIRQ unused and polls ready. GPIO3_25 may be added later if its controller semantics are established. |
| `get_panel_type()` | Initially select a Kindle-specific 800×600 profile, not mainline's superficially similar generic profile. |
| `cleanup()` | Quiesce the host interface, release GPIOs, and disable the clock; do not erase or program persistent flash. |

There is a subtle readiness requirement: mainline's GPIO path calls
`wait_for_rdy()` around writes, while its MMIO command routines invoke
`mmio_write()` directly (`broadsheetfb.c:158–184,277–292`). Amazon waits before
each word. The Kindle `mmio_write()` and `mmio_read()` implementations must
therefore perform the ready poll internally. A glue implementation which only
provides `wait_for_rdy()` would be incorrect.

Accesses also need serialization because `DI_DISP_LLA_CONF/DATA` is a shared
two-register transaction. The existing core generally serializes protocol
operations with `io_lock`, but the host driver should protect this pair as
well if any other IPU client can touch it.

## 4. Initialization comparison

| Area | Amazon | Mainline | Assessment for first light |
| --- | --- | --- | --- |
| Reset | 100 ms asserted low, 400 ms after release. | Board callback owns reset; AM300 implementation differs. | **Kindle-specific sequence required** in host `init()`. |
| Identity | Reads registers 0 and 2; accepts S1D13521 B00/B01/B02 and ISIS. | Reads the same registers; expects `0x0047`/`0x0100`, warns otherwise. | **Effectively equivalent for S1D13521**, but log and gate on product before initialization. Revision mismatch may be benign. |
| System run | Normal S1D13521 path sends `INIT_SYS_RUN`; ISIS may upload command/waveform images. | Sends `INIT_SYS_RUN` and sleeps one second. | **Equivalent for S1D13521**. Do not use the ISIS path. |
| ISIS product `0x004d` | Programs PLL standby (`0x0004,0x5949,0x0040`), installs a compiled command set, uploads the selected waveform to SDRAM at `0x000afc80`, selects SDRAM waveform device, and uses image buffer `0x000efc80` (`broadsheet.c:4025–4045`). | Has none of these operations and warns but continues after seeing `0x004d`. | **Incompatible as-is.** Host communication can be proven, but display initialization needs explicit ISIS support or a confirmed non-destructive persistent-command alternative. |
| PLL/command firmware | Normal Broadsheet relies on persistent controller state; special bootstrap/ISIS paths do more. | No PLL upload. | **Probably compatible for ordinary S1D13521 boot**, verify identity and ready transitions. |
| SDRAM | Amazon writes refresh register `0x0106 = 0x0203` during system init and performs a register test. | Does not write `0x0106`; explicitly sets image buffer address registers `0x0310/0x0312` to `800*600*2`. | **Unclear**. Add the known refresh write as a small Kindle quirk if reset-state testing shows it is necessary. Do not copy Amazon's diagnostic writes into routine boot. Validate mainline's image-buffer address against Kindle firmware state. |
| Display configuration | Selects from tables using controller clock, panel size, and waveform output rate. | Selects a fixed table index. | **Kindle profile required**; see below. |
| Waveform discovery | Reads panel/waveform metadata, then tells S1D13521 to use persistent waveform address `0x0886`. | Issues `RD_WFM_INFO` for address `0x0886`. | **Effectively equivalent** for a valid persistent waveform. |
| Gate-driver clear | Performs display-driver clear during panel bring-up. | Sends `INIT_DSPE_TMG`, gate-driver clear, and waits. | **Probably compatible** after correct timings/power. |
| Rotation | Selects portrait orientation through controller command. | Leaves its fixed 800×600 landscape orientation. | **Different but acceptable** for first light. |
| Pixel transfer | Amazon PIO path writes 16-bit words through IPU. | Mainline converts framebuffer bytes into its existing packed 4bpp transfer stream and uses board writes. | **Protocol-compatible in principle**; retain mainline behaviour. |
| Temperature | Amazon reads/manages PMIC temperature and controller update modes. | Uses the existing driver's simpler update command path. | **Unclear on image quality**, not a bus bring-up blocker if ambient testing is conservative. |
| Panel rails/VCOM | Separate Papyrus driver, panel-specific VCOM, power-good wait. | No regulator or VCOM acquisition. | **Kindle-specific external dependency required before a refresh**. Never guess VCOM. |

### The 800×600 table is not an exact match

Mainline's panel index 0 is labelled “standard 6 inch”
(`broadsheetfb.c:52–64`), but its timing words do not match Amazon's 6-inch
tables (`broadsheet_def.h:173–218`). For the source-selected Luigi 24 MHz clock
and Amazon's default 50 Hz profile:

| Field | Mainline index 0 | Amazon 6-inch, 24 MHz / 50 Hz |
| --- | ---: | ---: |
| Width × height | 800 × 600 | 800 × 600 |
| Frame sync / begin / end | 4 / 4 / 10 | 4 / 4 / 10 |
| Line sync / begin / end | 10 / 4 / 100 | 10 / 10 / 56 |
| Pixel-clock divisor | 6 | 6 |

Amazon's 85 Hz profile differs again (line sync/begin/end 8/8/71 and divisor
3). Amazon derives size and rate from persistent panel/waveform data and
defaults to 6-inch/50 Hz (`broadsheet.c:4105–4139`). The inspected stock unit
described in `docs/kindle-3-hardware-enablement.md` reports panel
`ED060SC7C1`, waveform `V220_C052_60_WJB701_D`, and 85 Hz, so its correct table
is the 24/85 one—not the default used in the comparison above. Source alone
does not establish that every Kindle 3 panel uses that rate. The first
downstream patch may hard-code 24/85 for this known unit after rechecking its
metadata; a plausible upstream design needs actual profile selection rather
than calling mainline index 0 “close enough.”

## 5. Waveform handling and safety

For the ordinary product-`0x0047` S1D13521 path, Amazon does not upload a
waveform at every boot. It uses controller-attached serial flash with:

* command data at `0x00000`;
* waveform data at `0x00886`;
* panel data at `0x30000`.

Those constants are in `broadsheet_def.h:319–327`. The S1D13521 panel path
passes `0x00886` directly to waveform initialization
(`broadsheet.c:4355–4375`).

ISIS is materially different: `bs_get_isis_waveform()` selects a rootfs
override, the controller-flash waveform, or a built-in fallback; normal ISIS
initialization then copies that selected image into controller SDRAM and calls
`INIT_WAVEFORMDEV` for SDRAM (`broadsheet.c:3889–4045`). This is volatile SDRAM
loading, not flash programming, but it means the existing mainline persistent
`0x00886` assumption is not source-backed for an ISIS unit. The earlier live
survey found a valid C052 waveform in the unit's read-only controller flash,
which is a safe input to preserve and potentially load, not evidence that ISIS
can execute it in place.

Amazon also contains explicit serial-flash erase and programming functions
around `broadsheet.c:5002`; they are not needed and must not be ported for
first light.

Mainline similarly reads waveform information from `0x00886` during display
initialization. It programs flash only when root writes the
`loadstore_waveform` sysfs attribute (`broadsheetfb.c:733–778`); probe does not
invoke that path. For safe bring-up:

1. do not write that attribute;
2. do not request `broadsheet.wbf`;
3. do not issue serial-flash erase/program commands;
4. first perform only reset and identity reads; query waveform information only
   through the source-backed path for the identified product;
5. preserve the stock flash and panel-data backups documented in the existing
   hardware-enablement report.

## 6. Modern Device Tree representation

### Correct model

WEIM is present at `memory-controller@b8002000` in modern `imx35.dtsi`, and its
driver/binding support external chip-select windows. It is not involved here.
Putting the S1D13521 under `weim` or assigning it a fabricated physical address
would misdescribe the board.

The clean hardware topology is an i.MX35 asynchronous-display host with one
controller attached to DISP0:

```text
i.MX35 IPU asynchronous display host (registers + clock + bus pinctrl)
└── DISP0 endpoint: Epson S1D13521
    ├── reset GPIO
    ├── ready GPIO
    └── optional HIRQ

separate I2C bus
└── E-Ink/Papyrus PMIC
    ├── wake/enable GPIO
    ├── interrupt
    ├── power-good GPIO
    └── panel rail regulators / VCOM
```

An eventual generic binding could look structurally like this. Property names
marked `TBD` are intentionally not proposed as stable ABI:

```dts
ipu_adc: display-bus@53fc0000 {
        compatible = "fsl,imx35-ipu-adc";       /* new binding */
        reg = <0x53fc0000 0x1000>;
        clocks = <&clks 55>;                    /* IPU gate; use a clock define */
        clock-names = "ipu";

        pinctrl-names = "default";
        pinctrl-0 = <&pinctrl_ipu_adc>;

        #address-cells = <1>;
        #size-cells = <0>;
        status = "okay";

        broadsheet: display-controller@0 {
                compatible = "epson,s1d13521"; /* new binding */
                reg = <0>;                      /* logical DISP0 port */

                reset-gpios = <&gpio2 17 GPIO_ACTIVE_LOW>;
                ready-gpios = <&gpio3 24 GPIO_ACTIVE_HIGH>;

                /* Optional only after HIRQ semantics are verified. */
                /* interrupt-parent = <&gpio3>; */
                /* interrupts = <25 IRQ_TYPE_EDGE_RISING>; */

                /* TBD: a real panel/profile description, not callback names. */
        };
};

pinctrl_ipu_adc: ipu-adcgrp {
        fsl,pins = <
                MX35_PAD_LD0__IPU_DISPB_DAT_0    0x...
                /* LD1 through LD14 */
                MX35_PAD_LD15__IPU_DISPB_DAT_15 0x...
                MX35_PAD_LD20__IPU_DISPB_CS0    0x...
                MX35_PAD_LD21__IPU_DISPB_PAR_RS 0x...
                MX35_PAD_LD22__IPU_DISPB_WR     0x...
                MX35_PAD_LD23__IPU_DISPB_RD     0x...
        >;
};
```

The ellipses are deliberate: pad-control electrical values must be translated
from the known-working old IOMUX setup or measured, not guessed. Reset and
ready GPIO pads also need GPIO pinctrl entries.

For the smallest reversible Paper Linux experiment, a flat downstream node
may instead match `"amazon,kindle3-broadsheet"` and carry the IPU register,
clock, pinctrl, reset, and ready resources. A board-glue driver can then create
the existing `broadsheetfb` platform device with `struct broadsheet_board`
callbacks. This is less pure than the host/child hierarchy but avoids designing
two bindings before first light. Its `reg` must be documented as the **i.MX35
host register block**, never as an S1D13521 memory range.

Controller protocol details and callback names do not belong in DT. Bus width
and interface mode are properties of the host/controller connection and may be
fixed by the compatible for this first implementation. Panel size/rate and
timings describe the attached panel/controller setup; a temporary hard-coded
Kindle profile is acceptable downstream. A separate panel node is unnecessary
to retain the current driver's behaviour, though it may be appropriate later.

The PMIC belongs on its actual I2C bus as a separate regulator provider. The
Broadsheet/controller node can consume named supplies once the rail topology is
known. Do not add a speculative `tps65185` compatible, voltage constraints, or
GPIO polarity until the exact PMIC and board revision have been confirmed.
The current Paper Linux DTS does not claim these LD pads and leaves FEC
disabled, so no existing enabled node was found competing for the display bus
or the two FEC pads Amazon reuses for Papyrus.

No existing Linux 7.0 DT binding for S1D13521/Broadsheet was found. The
TPS65185 regulator binding is useful precedent for keeping panel power separate,
not proof that it describes the Kindle part. `regmap` could be used internally
for the IPU register block, but it does not turn the command-stream S1D13521
protocol into a conventional register-map device.

## 7. Minimal first-light implementation plan

Each phase should be independently reversible and should stop on any identity,
ready, PMIC, or timing failure.

1. **Preserve evidence first.** Keep the stock controller-flash and panel-data
   backups. Record the unit's panel ID, VCOM, waveform size/rate/version, board
   revision, and stock boot identity log.
2. **Add a PIO-only i.MX35 host shim.** Map `0x53fc0000/0x1000`, enable the IPU
   gate, apply the LCD bus pinctrl state, and port only the DISP0 System-80
   initialization and low-level command/data/read operations needed by
   Amazon's PIO path. Do not port ADC DMA, EOF IRQ, automatic refresh, or other
   IPU functions.
3. **Prove reset and ready without panel power changes.** Acquire reset and
   ready with GPIO descriptors, reproduce 100 ms low + 400 ms release, and
   verify that ready becomes high within five seconds.
4. **Perform a read-only bus test.** Issue `RD_REG` for product `0x0002` and
   revision `0x0000`. Record both. Stop on all-zero/all-one or unstable reads.
   A `0x0047` result enters the small S1D13521 path below. A `0x004d` result
   proves the host interface but must stop before `broadsheet_init()`.
5. **For product `0x0047`, instantiate the existing core.** Have the DT-matched
   glue provide `struct broadsheet_board`, with MMIO callbacks that poll ready
   internally, and create/probe `broadsheetfb`. Keep its existing framebuffer
   format, deferred-I/O interval, and update commands.
6. **Use the known Kindle panel timing.** Add a dedicated 800×600 table entry
   based on the unit's confirmed 24/50 or 24/85 persistent metadata. Do not use
   generic mainline panel index 0 solely because its geometry matches.
7. **Make power safe.** Confirm the exact Papyrus-compatible PMIC, I2C bus,
   board-revision GPIO polarity, panel VCOM, and power-good behaviour. Reuse a
   proper mainline regulator driver if it truly matches. Until then, a small
   downstream helper reproducing only the known register sequence is acceptable;
   it must never guess VCOM or rail voltages.
8. **Run S1D13521 initialization without flash writes.** For product `0x0047`,
   send system-run, the confirmed panel display/timing values, and read
   waveform information at `0x00886`. Compare ready transitions and readbacks
   with stock. Add Amazon's SDRAM refresh write only if evidence shows the
   reset state requires it.
9. **Request one ordinary mainline update.** Allow the existing probe's initial
   image load/update, or make one normal framebuffer write and wait for existing
   deferred I/O. Start with the full-screen path already used by the driver.
   Do not touch `loadstore_waveform`.
10. **Only then test repeated fbdev writes.** Confirm the current automatic
    update semantics and suspend/removal cleanup; orientation and update-quality
    improvements remain later work.

If step 4 returns `0x004d`, the smallest safe follow-on remains internal to the
driver and need not change fbdev semantics, but it is no longer merely board
glue. It must reproduce the source-backed ISIS PLL/command-set setup, load the
verified existing waveform into SDRAM without writing flash, select the ISIS
image-buffer address, enable the ISIS auto-waveform/pixel-invert display bits,
and account for Amazon's other `BS_ISIS()` protocol differences. That work
should be a separately reviewable patch series. Until it exists, the honest
first-light endpoint on such a unit is successful identity communication, not
a physical refresh.

Useful staged diagnostics are: clock rate and calculated timing fields; reset
and ready levels; every command timeout; product/revision; read-only waveform
metadata; selected panel profile; PMIC power-good; and the last command before
a failure. Avoid verbose logging of every framebuffer word.

## 8. Likely code changes

### Small downstream first-light patch

| File | Change | Classification |
| --- | --- | --- |
| new `drivers/video/fbdev/broadsheetfb-imx35.c` | DT-matched Kindle/i.MX35 glue: resources, minimal IPU ADC PIO host setup, reset/ready, MMIO callbacks, and child `broadsheetfb` platform device. | Acceptable temporary Paper Linux glue. Much of the host code may later become generic. |
| `drivers/video/fbdev/Kconfig` and `Makefile` | Select/build the glue with i.MX35 and `FB_BROADSHEET`. | Ordinary integration. |
| `drivers/video/fbdev/broadsheetfb.c` | Add the exact Kindle panel table entry; optionally make probe diagnostics reject a non-S1D13521 product before sending initialization. Do not change deferred I/O or fbdev operations. | Panel hard-code is downstream; safer identification is plausibly upstreamable. |
| `drivers/video/fbdev/broadsheetfb.c` plus a firmware/command-data source, only if product is `0x004d` | Add a controller-variant initialization path which reproduces Amazon's volatile ISIS command/waveform loading and pixel configuration while leaving fbdev/deferred I/O intact. | Necessary Kindle variant support, plausibly upstreamable only with documented firmware handling; not part of the first host-glue patch. |
| `board/amazon/kindle3/mainline/linux-dts/nxp/imx/imx35-kindle3.dts` | Add the temporary host/controller node and pinctrl/GPIO/clock resources. | Downstream board description. |
| Paper Linux kernel config | Enable `FB_BROADSHEET` and the new glue, plus required framebuffer, GPIO, pinctrl, and clock support. | Board configuration. |
| PMIC driver/DT, exact files TBD | Use a verified existing regulator driver or reproduce only the minimum known Papyrus sequence and expose power-good. | Temporary glue until chip and rail binding are certain. |

The glue/child approach avoids changing `include/video/broadsheetfb.h`. The
child's platform data can retain the deliberate board-callback abstraction,
and its board owner keeps the glue module pinned. The parent owns mapped
resources and tears the child down before releasing them.

### Plausible later upstream shape

* a binding and narrow driver for the i.MX35 IPU asynchronous-display host;
* an `epson,s1d13521` binding and firmware-node instantiation path;
* descriptor-based reset/ready and optional IRQ acquisition;
* separation of fixed host-bus operations from controller/panel profile data;
* removal of the assumption in `broadsheetfb_probe()`
  (`broadsheetfb.c:1003–1115`) that all instances arrive with legacy platform
  data;
* a real selection mechanism for supported panel timing profiles.

The existing code already has the useful separation:

```text
struct broadsheet_board physical operations
        ↓
broadsheet command/register helpers
        ↓
display initialization, transfer, and update commands
        ↓
fbdev and deferred I/O
```

No broad refactor is necessary for first light. A tiny change may be useful to
make board construction firmware-node aware, but the protocol, update, and
fbdev layers should remain intact.

### Explicit scope classification

**Likely reasonable upstream work:** DT bindings for the real host and
S1D13521, firmware-node instantiation, modern clock/GPIO/regulator acquisition,
the PIO host-interface implementation, and small core changes that remove the
AM300-only platform-data assumption.

**Acceptable temporary Paper Linux work:** a flat Kindle-compatible glue node,
a hard-coded panel profile after checking the unit's metadata, read-only
diagnostics, the known SDRAM-refresh quirk if testing proves it necessary, and
a narrowly reproduced PMIC sequence after its identity/VCOM are established.

**Do not do yet:** alter fbdev's userspace ABI, add refresh ioctls, change
deferred-I/O scheduling, redesign partial updates, add automatic-refresh policy,
introduce a generic E-Ink ABI/framework, or convert the driver to DRM.

## 9. Open questions and risks

| Question/risk | What source proves | Safe resolution |
| --- | --- | --- |
| Actual fitted controller/revision | Software supports S1D13521 `0x0047` B00/B01/B02 and ISIS `0x004d`; prior live evidence strongly indicates ISIS on the inspected unit. | Read registers 2 and 0 after reset. Proceed through current mainline init only with `0x0047`; branch to explicit ISIS work for `0x004d`. |
| Exact panel rate/profile | Geometry is 800×600; Amazon has distinct 50/85 Hz timing tables and derives the choice from persistent metadata. | Record stock metadata and select the exact table. |
| Broadsheet reference clock | Amazon's Kindle table selection uses 24 MHz. Its physical source/control is not identified in the traced display code. | Check schematic/board measurements or stock clock state; do not confuse it with IPU HSP. |
| Modern IPU host support | Linux 7.0 retains the clock but has no i.MX35 ADC/SYS80 implementation. | Port only the small PIO subset from the released Freescale driver. |
| IPU register sharing | The 4 KiB block contains more than low-level access; modern DT has no owner. | Give one driver exclusive ownership and touch only source-traced registers/bits. Preserve unrelated bits. |
| Mainline image-buffer address | Mainline programs `0x0310/0x0312`; Amazon's 6-inch path does not obviously set the same value. | Compare stock register state/data-sheet meaning before relying on it; make a small quirk only if needed. |
| SDRAM refresh register `0x0106` | Amazon writes `0x0203`; mainline does not. | Read after reset/compare stock; add the known value if required. |
| PMIC exact type and bus | Old “Papyrus” code probes `0x48`; GPIO wiring and revision-dependent enable polarity are known. | Identify the IC and bus physically/from stock DT-less board state, read revision safely, then choose a binding. |
| VCOM and rails | VCOM is panel-specific and Amazon reads it from panel data. | Reuse the backed-up value and validated PMIC sequence; never infer a voltage. |
| HIRQ | GPIO3_25 is named, but normal Amazon Broadsheet setup supplies no handler. | Omit it initially and poll ready exactly as Amazon does. |
| Ready polling in mainline MMIO path | Core MMIO helpers do not consistently call `wait_for_rdy()`. | Poll inside every Kindle MMIO callback. |
| Waveform persistence | The inspected stock unit has a valid controller-flash waveform. S1D13521 executes the persistent image; ISIS source copies a selected image to SDRAM. | Read metadata only, never write/erase flash, and use the product-specific source-backed path. |
| Pad electrical configuration | Functional muxes are unambiguous; modern pad-control values have not been proven here. | Translate old pad settings carefully and, if necessary, compare signal integrity on hardware. |
| Physical rotation | Amazon uses portrait rotation; mainline is landscape. | Accept a rotated first-light image; defer orientation work. |

## Conclusion

The shortest credible host path is not a WEIM child driver. It is a small PIO-only
i.MX35 IPU asynchronous-display shim feeding the existing
`struct broadsheet_board` MMIO callbacks, plus the exact Kindle reset/ready
GPIOs and verified panel-power state. For product `0x0047`, the protocol and
fbdev parts of `broadsheetfb` are already close to Amazon's normal S1D13521
path, including read-only use of the persistent waveform at `0x00886` and
ordinary automatic updates. The concrete incompatibilities are the missing
modern IPU host implementation, callback-level ready polling, the non-matching
panel timing table, and external Papyrus/VCOM sequencing.

For product `0x004d`—which prior live evidence suggests is fitted to the
inspected Kindle—the same host shim is correct, but current mainline controller
initialization is not. A safe patch must add Amazon's volatile ISIS setup before
attempting a refresh. This does not require changing the framebuffer ABI or
deferred-update behaviour, and it must not write waveform flash, but it is a
larger minimum than the S1D13521-only hookup.

That remains a bounded first-light project: prove reset, read the product,
follow the matching `0x0047` or `0x004d` internal initialization, select the
known panel timing, and let the unchanged mainline framebuffer perform one
full update. Everything involving new update APIs, deferred-I/O policy, or DRM
remains explicitly out of scope.
