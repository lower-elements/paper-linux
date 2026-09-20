# Kindle 3 Papyrus behaviour in stock firmware

This note records the Papyrus/TPS65180 observations made while investigating
Kindle 3 display bring-up.  It is deliberately split between direct
observations, interpretations, and unknowns.  A register value seen in one
state must not be treated as a universal board configuration without the
state and provenance recorded here.

## Hardware identity and access

The inspected Wi-Fi Kindle 3 is a Shasta PVT1 unit running Amazon's
`2.6.26-rt-lab126` kernel and firmware 3.4.3.  Papyrus is the TI TPS65180
revision r1p2 at I2C address `0x48` on I2C bus 1.  Its `REVID` register reads
`0x60`.

On the stock image, the vendor `i2cutil` program can read it with commands of
the following form:

```text
/usr/bin/i2cutil -d /dev/i2c/1 -r 0x48 0x0f
```

These tests used reads only, except for invoking the normal stock `eips`
display-update command.  No Papyrus registers were written manually.

## Stock idle and update observations

The following snapshot was taken on stock firmware while the display was
idle, before an update:

| Register | Value | Meaning/relevance |
|---|---:|---|
| `ENABLE` (`0x01`) | `0x1f` | Stock value in both idle and active snapshots |
| `VP_ADJUST` (`0x02`) | `0x23` | Stock VP/VDDH setting |
| `VN_ADJUST` (`0x03`) | `0xa3` | Stock VN/VEE/VCOM-source setting |
| `VCOM_ADJUST` (`0x04`) | `0xc4` | Panel calibration, approximately −2.12 V |
| `INT_ENABLE1` (`0x05`) | `0x00` | Stock interrupt mask |
| `INT_ENABLE2` (`0x06`) | `0x01` | Stock interrupt mask |
| `INT_STATUS1` (`0x07`) | `0x00` | No status bits observed |
| `INT_STATUS2` (`0x08`) | `0x00` | No status bits observed |
| `PWR_SEQ0` (`0x09`) | `0xe4` | Rail-to-strobe assignment |
| `PWR_SEQ1` (`0x0a`) | `0x22` | First two delays |
| `PWR_SEQ2` (`0x0b`) | `0x22` | Final two delays |
| `TMST_CONFIG` (`0x0c`) | `0x20` | Thermistor configuration |
| `TMST_OS` (`0x0d`) | `0x32` | Thermistor hot threshold |
| `TMST_HYST` (`0x0e`) | `0x2d` | Thermistor hysteresis/cool threshold |
| `PG_STATUS` (`0x0f`) | `0x00` | Panel rails not power-good |
| `REVID` (`0x10`) | `0x60` | TPS65180 r1p2 observed identity |

An ordinary stock `eips` update was then started.  While it was active, the
configuration values above remained unchanged, and:

- `PG_STATUS` became `0xfa`, meaning all six defined power-good status bits
  were asserted;
- `INT_STATUS1` and `INT_STATUS2` were observed as zero; and
- `ENABLE` remained `0x1f`.

After the update, within roughly two seconds, `PG_STATUS` returned to `0x00`.
Thus stock firmware powers the panel rails for an update and then returns them
to the non-power-good state.  The two-second observation is consistent with
the documented e-ink power-timer delay of 2000 ms, although this test did not
attempt to measure the complete internal timing sequence.

## Power ordering and delays

`PWR_SEQ0 = 0xe4` decodes as:

1. VNEG on STROBE1;
2. VEE on STROBE2;
3. VPOS on STROBE3; and
4. VDDH on STROBE4.

Power-down uses the reverse order.  `PWR_SEQ1 = 0x22` and `PWR_SEQ2 = 0x22`
set each of the four delay fields to 2 ms:

- wakeup-to-STROBE1: 2 ms;
- STROBE1-to-STROBE2: 2 ms;
- STROBE2-to-STROBE3: 2 ms; and
- STROBE3-to-STROBE4: 2 ms.

These are not guessed values: they were read from the running stock Kindle
before and during a normal display update, and the update completed with all
power-good bits asserted and no observed fault status.

## VCOM and other voltage settings

The stock panel-specific VCOM value is `0xc4`, documented by the vendor
software/live observation as approximately −2.12 V.  The reset value observed
when Papyrus is uninitialised is `0x74`, nominally about −1.25 V.  The reset
value is inside the TPS65180's electrical adjustment range, but it is not the
Kindle panel's calibrated operating value and should not be used for normal
display operation.

The stock idle/update snapshots used `VP_ADJUST = 0x23` and
`VN_ADJUST = 0xa3`.  No evidence from these tests indicates that stock
firmware changes either value per update.

## Interrupt and fault behaviour

Stock firmware configured the interrupt-enable registers as:

```text
INT_ENABLE1 = 0x00
INT_ENABLE2 = 0x01
```

The exact consumer of the enabled bit has not been established here.  These
registers control interrupt reporting; they do not select panel-rail voltages
or sequencing.  During the captured update, both interrupt-status registers
read zero.  A more exhaustive fault test has not been performed, and status
reads should not be assumed to preserve latched events without checking the
device semantics.

## Mainline comparison

When the mainline TPS65180 driver was bound in probe-only mode, it performed
no writes and reported the reset/default state, including:

- `VCOM_ADJUST = 0x74`;
- `INT_ENABLE1 = 0x74`;
- `INT_ENABLE2 = 0xfb`;
- `PWR_SEQ0/1/2 = 0xe4/0x22/0x22`; and
- `PG_STATUS = 0x00`.

With Isis initialised but the PMIC still probe-only, `PG_STATUS` was observed
as `0xfa`, showing that Isis initialization requests panel power.  The stock
tests above establish that the same sequence values and calibrated VCOM are
used by Amazon during an actual update.

The important implementation consequence is that the TPS65180 driver must
not inherit TPS65185 register semantics or use the TPS65180 reset VCOM as the
Kindle operating value.  It should preserve the stock sequence
`e4/22/22`, apply the panel VCOM `c4` before active operation, and keep
regulator/refresh activation explicit rather than enabling rails merely as a
probe side effect.

## Still unknown

The observations above do not establish:

- which i.MX35 or Isis GPIO actually initiates the Papyrus active/standby
  transition;
- the exact time between each external power-control transition and the
  first/last EPDC command;
- whether all update types use exactly the same rail-on duration;
- the complete stock handling of thermistor warnings and fault recovery; or
- the behaviour of every Kindle 3 board variant.

Those questions should be investigated with read-only logging first.  The
confirmed stock values above are the safe baseline for the inspected Kindle
3 and should not be generalized to a different panel without a new
calibration and power-state observation.
