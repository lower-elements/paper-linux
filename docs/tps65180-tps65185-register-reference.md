# TPS65180 and TPS65185 register reference

This is an implementation-oriented comparison of the TI TPS65180 r1p2 and
TPS65185 e-paper display power-management ICs. It is intended to support the
Linux regulator driver rather than replace the datasheets.

Sources:

- Paper Resources `ti-tps6518x-datasheet-g`, *TPS65180, TPS65181,
  TPS65180B, TPS65181B*, revision G, especially physical pages 18--32.
- Paper Resources `ti-tps65185-datasheet-g`, *TPS65185, TPS651851*,
  revision G, especially physical pages 31--48.

Unless stated otherwise, bit 7 is the most significant bit. `R` means
read-only, `R/W` means readable and writable, and `self-clear` means hardware
returns the bit to zero after accepting the request. Software should preserve
reserved or unused writable bits unless the datasheet explicitly requires a
value.

The TPS65180 values below describe the non-B TPS65180 and, where revision
matters, the r1p2 device identified by `REVID = 0x60`. TPS65181-only behavior
is called out but is not part of the initial Kindle 3 target.

## Address-level comparison

| Address | TPS65180 | TPS65185 | Compatibility |
|---:|---|---|---|
| `0x00` | `TMST_VALUE` | `TMST_VALUE` | Compatible |
| `0x01` | `ENABLE` | `ENABLE` | Same fields; different reset value and external control pins |
| `0x02` | `VP_ADJUST` | `VADJ` | Incompatible voltage semantics |
| `0x03` | `VN_ADJUST` | `VCOM1` | Completely different |
| `0x04` | `VCOM_ADJUST` | `VCOM2` | Incompatible VCOM format and control bits |
| `0x05` | `INT_ENABLE1` | `INT_EN1` | Related, but bits 7, 3, 1, and 0 differ |
| `0x06` | `INT_ENABLE2` | `INT_EN2` | Mostly compatible; bit 2 differs |
| `0x07` | `INT_STATUS1` | `INT1` | Related, but bits 7, 3, 1, and 0 differ |
| `0x08` | `INT_STATUS2` | `INT2` | Mostly compatible; bit 2 differs |
| `0x09` | `PWR_SEQ0` | `UPSEQ0` | Same power-up assignment encoding |
| `0x0a` | `PWR_SEQ1` | `UPSEQ1` | Incompatible delay encoding |
| `0x0b` | `PWR_SEQ2` | `DWNSEQ0` | Completely different |
| `0x0c` | `TMST_CONFIG` | `DWNSEQ1` | Completely different |
| `0x0d` | `TMST_OS` | `TMST1` | Completely different |
| `0x0e` | `TMST_HYST` | `TMST2` | Incompatible threshold format |
| `0x0f` | `PG_STATUS` | `PG` | Compatible |
| `0x10` | `REVID` | `REVID` | Same address, different ID encoding |
| `0x11` | TPS65181 `FIX_READ_POINTER` only | Not present | Not present on TPS65180 or TPS65185 |

## Common and nearly common registers

### `0x00`: `TMST_VALUE` -- thermistor ADC result

Both chips expose the temperature as an eight-bit signed Celsius value:

- `0xf6`: -10 degrees C or colder
- `0xf7` through `0xff`: -9 through -1 degrees C
- `0x00` through `0x55`: 0 through 85 degrees C
- `0x55`: also represents temperatures hotter than 85 degrees C

The register is read-only and has no defined reset value. Linux can interpret
it as `(s8)value` and convert degrees C to millidegrees C for hwmon.

### `0x01`: `ENABLE` -- rail enables and state transitions

The bit positions are shared:

| Bits | Name | Access | Meaning when set |
|---:|---|---|---|
| 7 | `ACTIVE` | R/W, self-clear | Request STANDBY-to-ACTIVE transition using the configured power-up sequence |
| 6 | `STANDBY` | R/W, self-clear | Request ACTIVE-to-STANDBY transition using the configured power-down sequence; takes priority over `ACTIVE` |
| 5 | `V3P3_SW_EN` / `V3P3_EN` | R/W | Enable the VIN3P3-to-V3P3 switch |
| 4 | `VCOM_EN` | R/W | Enable the VCOM buffer |
| 3 | `VDDH_EN` | R/W | Enable the VDDH charge pump |
| 2 | `VPOS_EN` | R/W | Enable the positive LDO; VPOS cannot be enabled before VNEG |
| 1 | `VEE_EN` | R/W | Enable the VEE charge pump |
| 0 | `VNEG_EN` | R/W | Enable the negative LDO; disabling VNEG also disables VPOS |

Important differences:

- TPS65180 resets to `0x1f`: all five panel-rail enable bits are set, while
  `ACTIVE`, `STANDBY`, and the 3.3 V switch are clear.
- TPS65185 resets to `0x00`.
- On TPS65180, the individual enable bits are ANDed with the external PWRx
  inputs. Setting an enable bit alone does not override an inactive PWRx input.
- TPS65185 instead exposes a single PWRUP control input.

### `0x06`: interrupt-enable group 2

| Bit | TPS65180 name | TPS65185 name | Access | Meaning |
|---:|---|---|---|---|
| 7 | `VB_UV_EN` | `VBUVEN` | R/W | Positive boost/DCDC1 undervoltage interrupt enable |
| 6 | `VDDH_UV_EN` | `VDDHUVEN` | R/W | VDDH undervoltage interrupt enable |
| 5 | `VN_UV_EN` | `VNUV_EN` | R/W | Inverting buck-boost/DCDC2 undervoltage interrupt enable |
| 4 | `VPOS_UV_EN` | `VPOSUVEN` | R/W | VPOS undervoltage interrupt enable |
| 3 | `VEE_UV_EN` | `VEEUVEN` | R/W | VEE undervoltage interrupt enable |
| 2 | Unused | `VCOMFEN` | R on TPS65180, R/W on TPS65185 | TPS65185 VCOM-fault interrupt enable |
| 1 | `VNEG_UV_EN` | `VNEGUVEN` | R/W | VNEG undervoltage interrupt enable |
| 0 | `EOC_EN` | `EOCEN` | R/W | Thermistor ADC end-of-conversion interrupt enable |

TPS65180 resets to `0xfb`; its unused bit 2 reads zero. TPS65185 resets to
`0xff`.

### `0x08`: interrupt-status group 2

This is the status counterpart of register `0x06`:

| Bit | TPS65180 name | TPS65185 name | Meaning when set |
|---:|---|---|---|
| 7 | `VB_UV` | `VB_UV` | Positive boost/DCDC1 undervoltage |
| 6 | `VDDH_UV` | `VDDH_UV` | VDDH undervoltage |
| 5 | `VN_UV` | `VN_UV` | Inverting buck-boost/DCDC2 undervoltage |
| 4 | `VPOS_UV` | `VPOS_UV` | VPOS undervoltage |
| 3 | `VEE_UV` | `VEE_UV` | VEE undervoltage |
| 2 | Unused | `VCOMF` | TPS65185 VCOM outside its normal operating range |
| 1 | `VNEG_UV` | `VNEG_UV` | VNEG undervoltage |
| 0 | `EOC` | `EOC` | Thermistor ADC conversion complete |

All bits are read-only status bits.

### `0x0f`: `PG_STATUS` / `PG` -- rail power-good status

The layout is shared and read-only:

| Bit | Name | Meaning when set |
|---:|---|---|
| 7 | `VB_PG` | Positive boost/DCDC1 is in regulation |
| 6 | `VDDH_PG` | VDDH charge pump is in regulation |
| 5 | `VN_PG` | Inverting buck-boost/DCDC2 is in regulation |
| 4 | `VPOS_PG` | VPOS LDO is in regulation |
| 3 | `VEE_PG` | VEE charge pump is in regulation |
| 2 | Unused | Reads zero |
| 1 | `VNEG_PG` | VNEG LDO is in regulation |
| 0 | Unused | Reads zero |

Both chips reset this register to zero. On TPS65185, the open-drain PG output
is released when VDDH, VPOS, VEE, and VNEG are all good. The Kindle's observed
`0xfa` means all six defined status bits are set.

### `0x10`: `REVID` -- silicon identification

The complete register is read-only.

TPS65180-family IDs:

| Value | Device |
|---:|---|
| `0x50` | TPS65180 r1p1 |
| `0x60` | TPS65180 r1p2 |
| `0x70` | TPS65180B, also described as TPS65180 r1p3 |
| `0x80` | TPS65180B, also described as TPS65180 r1p4 |
| `0x51`, `0x61`, `0x71`, `0x81` | Corresponding TPS65181/81B revisions |

The TPS65180 register-map overview prints `0x41`, which conflicts with the
explicit revision table and does not identify any production revision listed
there. It may be a stale overview value rather than a simple typographical
error. Driver identification should use the explicit REVID table and the value
read from hardware. The Kindle 3 device reads `0x60`, agreeing with the table's
TPS65180 r1p2 entry.

TPS65185-family IDs:

| Value | Device |
|---:|---|
| `0x45` | TPS65185 r1p0 |
| `0x55` | TPS65185 r1p1 |
| `0x65` | TPS65185 r1p2 |
| `0x66` | TPS651851 r1p0 |

For TPS65185, bits 7:6 are the major revision, bits 5:4 are the minor
revision, and bits 3:0 identify the device version.

## TPS65180-specific register definitions

### `0x02`: `VP_ADJUST` -- VPOS tracking and VDDH trim

Reset value: `0x23`.

| Bits | Name | Access | Encoding |
|---:|---|---|---|
| 7 | Unused | R | Preserve/read as zero |
| 6:4 | `VDDH_SET` | R/W | `000` +10%, `001` +5%, `010` nominal, `011` -5%, `100` -10%, `101`--`111` reserved |
| 3 | Unused | R | Preserve/read as zero |
| 2:0 | `VPOS_SET` | R/W | VPOS relative to `abs(VNEG)`: `000` -0.75 V, `001` -0.50 V, `010` -0.25 V, `011` equal, `100` +0.25 V, `101` +0.50 V, `110` +0.75 V, `111` reserved |

TI requires `VPOS_SET = 011` for proper VPOS/VNEG tracking. A symmetric
`vposneg` Linux regulator must leave this field at `011` and select the common
voltage through `VN_ADJUST.VNEG_SET` instead.

### `0x03`: `VN_ADJUST` -- VNEG selection, VEE trim, and VCOM source

TPS65180 reset value: `0xa3`.

| Bits | Name | Access | Encoding |
|---:|---|---|---|
| 7 | `VCOM_ADJ` | R/W | `0`: external `VCOM_XADJ` pin; `1`: I2C `VCOM_ADJUST` register |
| 6:4 | `VEE_SET` | R/W | `000` -10%, `001` -5%, `010` nominal, `011` +5%, `100` +10%, `101`--`111` reserved |
| 3 | Unused | R | Preserve/read as zero |
| 2:0 | `VNEG_SET` | R/W | `000` -15.75 V, `001` -15.50 V, `010` -15.25 V, `011` -15.00 V, `100` -14.75 V, `101` -14.50 V, `110` -14.25 V, `111` reserved |

`VCOM_ADJ` resets to one on TPS65180/TPS65180B and zero on
TPS65181/TPS65181B.

### `0x04`: `VCOM_ADJUST` -- eight-bit VCOM magnitude

Reset value: `0x74`, nominally 1.25 V of negative VCOM magnitude.

All eight bits are the R/W `VCOM_SET` code:

- `0x00`: 0 V
- `0x01`: approximately 11 mV
- `0xff`: 2.75 V
- theoretical step: 2.75 V / 255, approximately 10.78 mV
- guaranteed VCOM range: -0.3 V through -2.5 V

The physical output is negative. A Linux regulator implementation may expose
its positive magnitude, matching the existing TPS65185 driver convention.

### `0x05`: `INT_ENABLE1` -- thermal interrupt enables

Reset value: `0x74`.

| Bit | Name | Access | Meaning when set |
|---:|---|---|---|
| 7 | Unused | R | No function |
| 6 | `TSD_EN` | R/W | Thermal-shutdown interrupt enabled |
| 5 | `HOT_EN` | R/W | Thermal-shutdown early-warning interrupt enabled |
| 4 | `TMST_HOT_EN` | R/W | Thermistor-hot interrupt enabled |
| 3 | `TMST_COOL_EN` | R/W | Thermistor cool/escape interrupt enabled |
| 2 | `UVLO_EN` | R/W | VIN undervoltage interrupt enabled |
| 1:0 | Unused | R | No function |

### `0x07`: `INT_STATUS1` -- thermal interrupt status

| Bit | Name | Meaning when set |
|---:|---|---|
| 7 | Unused | No function |
| 6 | `TSD` | Chip is in thermal shutdown |
| 5 | `HOT` | Chip is approaching thermal shutdown |
| 4 | `TMST_HOT` | Thermistor is at or above the hot threshold |
| 3 | `TMST_COOL` | Thermistor hot condition has escaped according to the cool threshold |
| 2 | `UVLO` | VIN is below the undervoltage-lockout threshold |
| 1:0 | Unused | No function |

All bits are read-only status bits.

### `0x09`: `PWR_SEQ0` -- rail-to-strobe assignment

Reset value: `0xe4`.

| Bits | Name | Meaning |
|---:|---|---|
| 7:6 | `VDDH_SEQ` | Strobe selecting VDDH |
| 5:4 | `VPOS_SEQ` | Strobe selecting VPOS |
| 3:2 | `VEE_SEQ` | Strobe selecting VEE |
| 1:0 | `VNEG_SEQ` | Strobe selecting VNEG |

Each two-bit value maps `00` through `11` to STROBE1 through STROBE4. The
power-down order is the reverse of the configured power-up order.

### `0x0a`: `PWR_SEQ1` -- first two sequence delays

Reset value: `0x22`.

| Bits | Name | Meaning |
|---:|---|---|
| 7:4 | `DLY1` | STROBE1-to-STROBE2 on power-up, and STROBE2-to-STROBE1 on power-down |
| 3:0 | `DLY0` | WAKEUP-high-to-STROBE1 on power-up, and WAKEUP-low-to-STROBE4 on power-down |

Each four-bit value directly encodes 0 through 15 ms.

### `0x0b`: `PWR_SEQ2` -- final two sequence delays

Reset value: `0x22`.

| Bits | Name | Meaning |
|---:|---|---|
| 7:4 | `DLY3` | STROBE3-to-STROBE4 on power-up, and STROBE4-to-STROBE3 on power-down |
| 3:0 | `DLY2` | STROBE2-to-STROBE3 on power-up, and STROBE3-to-STROBE2 on power-down |

Each four-bit value directly encodes 0 through 15 ms.

### `0x0c`: `TMST_CONFIG` -- thermistor conversion and fault filtering

Reset value: `0x20`.

| Bits | Name | Access | Meaning |
|---:|---|---|---|
| 7 | `READ_THERM` | R/W, self-clear | Writing one starts a temperature conversion |
| 6 | Unused | R | No function |
| 5 | `CONV_END` | R | One when conversion has finished |
| 4:3 | `FAULT_QUE` | R/W | Required consecutive hot samples: `00` 1, `01` 2, `10` 4, `11` 6 |
| 2 | `FAULT_QUE_CLR` | R/W | Writing one clears the fault counter |
| 1:0 | Unused | R | No function |

This register is at `0x0c`, not the TPS65185 location `0x0d`.

### `0x0d`: `TMST_OS` -- hot threshold

Reset value: `0x32`, or 50 degrees C. All eight bits contain a signed Celsius
threshold. Valid values are -10 through 85 degrees C; other encodings are
reserved.

### `0x0e`: `TMST_HYST` -- cool/escape threshold

Reset value: `0x2d`, or 45 degrees C. All eight bits contain a signed Celsius
threshold with the same -10 through 85 degrees C valid range.

The register heading calls this the cool threshold and the register diagram
labels its field `TMST_COOL_SET`; only the subsequent field-detail row calls it
`TMST_HOT_SET`. Amazon's Papyrus header independently describes register
`0x0e` as the thermistor cool-temperature setting. The detail-row label is
therefore a confirmed copy-and-paste error.

### `0x11`: `FIX_READ_POINTER` -- TPS65181 only

This register does not exist on TPS65180. On TPS65181/TPS65181B, bit 0 fixes
the I2C read pointer at `0x00` when set; bits 7:1 are unused. It exists to let a
display controller repeatedly read temperature without first writing an
address. It must not be included in a TPS65180 r1p2 regmap range.

## TPS65185-specific register definitions

### `0x02`: `VADJ` -- symmetric VPOS/VNEG selection

Reset value: `0x23`.

| Bits | Name | Access | Meaning |
|---:|---|---|---|
| 7:4 | Unused | R/W | Preserve existing/reset values |
| 3 | Unused | R | Reads zero |
| 2:0 | `VSET` | R/W | `000`--`010` invalid, `011` +/-15.00 V, `100` +/-14.75 V, `101` +/-14.50 V, `110` +/-14.25 V, `111` reserved |

Unlike TPS65180, one field directly selects both VPOS and VNEG.

### `0x03`: `VCOM1` -- low eight VCOM bits

Reset value: `0x7d`. Bits 7:0 contain `VCOM[7:0]` and are R/W.

### `0x04`: `VCOM2` -- high VCOM bit and VCOM control

Reset value: `0x04`.

| Bits | Name | Access | Meaning |
|---:|---|---|---|
| 7 | `ACQ` | R/W, self-clear | Start kick-back-voltage acquisition |
| 6 | `PROG` | R/W, self-clear | Commit `VCOM[8:0]` to nonvolatile memory, then enter STANDBY |
| 5 | `HiZ` | R/W | Disconnect VCOM amplifier from its pin for measurement |
| 4:3 | `AVG` | R/W | Acquisition averaging: `00` 1 sample, `01` 2, `10` 4, `11` 8 |
| 2:1 | Unused | R/W | Preserve; bit 2 resets to one and bit 1 to zero |
| 0 | `VCOM[8]` | R/W | Most significant VCOM code bit |

Together, `VCOM2[0]` and `VCOM1[7:0]` form a nine-bit code. Each count is
-10 mV, giving 0 through -5.11 V.

### `0x05`: `INT_EN1` -- thermal and VCOM-operation interrupt enables

Reset value: `0x7f`.

| Bit | Name | Access | Meaning when enabled |
|---:|---|---|---|
| 7 | `DTX_EN` | R | Panel-temperature-change interrupt; reads disabled on TPS65185 |
| 6 | `TSD_EN` | R/W | Thermal-shutdown interrupt drives nINT |
| 5 | `HOT_EN` | R/W | Thermal early-warning interrupt drives nINT |
| 4 | `TMST_HOT_EN` | R/W | Thermistor-hot interrupt drives nINT |
| 3 | `TMST_COLD_EN` | R/W | Thermistor-cold interrupt drives nINT |
| 2 | `UVLO_EN` | R/W | VIN undervoltage interrupt drives nINT |
| 1 | `ACQC_EN` | R | VCOM acquisition-complete interrupt; reads enabled |
| 0 | `PRGC_EN` | R | VCOM programming-complete interrupt; reads enabled |

The datasheet presents bits 7, 1, and 0 as read-only fixed capabilities, not
normal software-controlled enables.

### `0x07`: `INT1` -- thermal and VCOM-operation status

| Bit | Name | Meaning when set |
|---:|---|---|
| 7 | `DTX` | Panel temperature has changed by the configured threshold |
| 6 | `TSD` | Chip is in thermal shutdown |
| 5 | `HOT` | Chip is approaching thermal shutdown |
| 4 | `TMST_HOT` | Thermistor is at or above the hot threshold |
| 3 | `TMST_COLD` | Thermistor is at or below the cold threshold |
| 2 | `UVLO` | VIN is below its undervoltage threshold |
| 1 | `ACQC` | VCOM acquisition completed |
| 0 | `PRGC` | VCOM nonvolatile programming completed |

All bits are read-only status bits.

### `0x09`: `UPSEQ0` -- power-up rail assignment

Reset value: `0xe4`.

| Bits | Name | Reset | Meaning |
|---:|---|---:|---|
| 7:6 | `VDDH_UP` | 3 | VDDH powers up on selected STROBE1--4 |
| 5:4 | `VPOS_UP` | 2 | VPOS powers up on selected STROBE1--4 |
| 3:2 | `VEE_UP` | 1 | VEE powers up on selected STROBE1--4 |
| 1:0 | `VNEG_UP` | 0 | VNEG powers up on selected STROBE1--4 |

Each value `0` through `3` selects STROBE1 through STROBE4.

### `0x0a`: `UPSEQ1` -- power-up delays

Reset value: `0x55`.

| Bits | Name | Interval |
|---:|---|---|
| 7:6 | `UDLY4` | STROBE3 to STROBE4 |
| 5:4 | `UDLY3` | STROBE2 to STROBE3 |
| 3:2 | `UDLY2` | STROBE1 to STROBE2 |
| 1:0 | `UDLY1` | `VN_PG` high to STROBE1 |

Each field encodes `00` 3 ms, `01` 6 ms, `10` 9 ms, or `11` 12 ms.

### `0x0b`: `DWNSEQ0` -- power-down rail assignment

Reset value: `0x1e`.

| Bits | Name | Reset | Meaning |
|---:|---|---:|---|
| 7:6 | `VDDH_DWN` | 0 | VDDH powers down on selected STROBE1--4 |
| 5:4 | `VPOS_DWN` | 1 | VPOS powers down on selected STROBE1--4 |
| 3:2 | `VEE_DWN` | 3 | VEE powers down on selected STROBE1--4 |
| 1:0 | `VNEG_DWN` | 2 | VNEG powers down on selected STROBE1--4 |

Each value `0` through `3` selects STROBE1 through STROBE4. Unlike TPS65180,
TPS65185 has an independently programmable power-down order.

### `0x0c`: `DWNSEQ1` -- power-down delays

Reset value: `0xe0`.

| Bits | Name | Meaning |
|---:|---|---|
| 7:6 | `DDLY4` | STROBE3 to STROBE4; `00` 6 ms, `01` 12 ms, `10` 24 ms, `11` 48 ms |
| 5:4 | `DDLY3` | STROBE2 to STROBE3; same encoding |
| 3:2 | `DDLY2` | STROBE1 to STROBE2; same encoding |
| 1 | `DDLY1` | WAKEUP low to STROBE1; `0` 3 ms, `1` 6 ms |
| 0 | `DFCTR` | `0`: 1x; `1`: 16x multiplier for `DDLY2`--`DDLY4` |

### `0x0d`: `TMST1` -- thermistor conversion and delta threshold

Reset value: `0x20`.

| Bits | Name | Access | Meaning |
|---:|---|---|---|
| 7 | `READ_THERM` | R/W, self-clear | Writing one starts temperature acquisition |
| 6 | Unused | R/W | Preserve existing value |
| 5 | `CONV_END` | R | One when conversion has finished |
| 4:2 | Unused | R/W | Preserve existing values |
| 1:0 | `DT` | R/W | Temperature-change threshold: `00` 2 C, `01` 3 C, `10` 4 C, `11` 5 C |

### `0x0e`: `TMST2` -- compact cold and hot thresholds

Reset value: `0x78`.

| Bits | Name | Encoding |
|---:|---|---|
| 7:4 | `TMST_COLD` | `0` through `15` represent -7 through +8 degrees C |
| 3:0 | `TMST_HOT` | `0` through `15` represent 42 through 57 degrees C |

The register diagram labels bits 7:4 `TMST_COLD`; their encoding is -7 through
+8 degrees C, and the field description says a cold interrupt is generated at
or below the selected threshold. Only the field-name cell in the subsequent
detail table says `READ_THERM`. `READ_THERM` is already the conversion-start
bit at `TMST1[7]`, so it cannot also name this four-bit threshold. This is
internally conclusive, although no second implementation source was found that
names the TPS65185 field.

## Driver-relevant consequences

The following behavior can be shared between variants:

- eight-bit register addresses and values;
- thermistor result at `0x00`;
- ENABLE bit positions and ACTIVE/STANDBY requests;
- interrupt-enable group 2 except bit 2;
- interrupt-status group 2 except bit 2;
- power-good layout at `0x0f`;
- REVID location at `0x10`.

The following must be selected by chip data or variant-specific operations:

- thermistor-control register: TPS65180 `0x0c`, TPS65185 `0x0d`;
- volatile-register map;
- VPOS/VNEG selector and number of supported voltages;
- VCOM register format and voltage conversion;
- power-sequence register definitions;
- interrupt group 1 definitions;
- interrupt group 2 bit 2;
- accepted REVID values;
- external power-control model: four PWRx inputs versus one PWRUP input.

In particular, a driver must never apply the TPS65185 meanings of registers
`0x03`, `0x04`, or `0x0c` to TPS65180. Those writes would respectively alter
TPS65180 rail voltage/trim configuration, VCOM, or thermistor state rather
than the intended TPS65185 function.
