# Kindle 3 audio hardware and safe bring-up

This note records only evidence from the exact Amazon GPL snapshot, its
provenance-separated comparison sources, upstream Linux, manufacturer
documentation, and observations reproduced on the Wi-Fi-only Kindle 3 under
test. It deliberately separates established wiring from proposed mainline
policy. The first mainline configuration is restricted to headphones; the
internal speakers remain outside its audio graph.

## Established hardware topology

The codec is a Wolfson WM8960 at I2C address `0x1a`. Lab126's
`sound/soc/imx/mx35luigi_wm8960.c` supplies that address to the codec driver.
That old driver probes all registered I2C adapters, so the source alone does
not identify which controller owns the codec. Live stock sysfs on the
Wi-Fi-only test unit identifies it as `1-001a`, whose adapter is the second
i.MX I2C controller. This corresponds to `i2c2` at `0x43f98000` in the modern
Device Tree. The machine driver connects i.MX35 SSI1 FIFO 0, internal AUDMUX
port 1, to external AUDMUX port 4. Its DAI format is I2S with normal bit-clock
and frame polarity; the WM8960 supplies both clocks and the i.MX35 SSI is the
slave.

The four external AUDMUX signals use `STXD4`, `SRXD4`, `SCK4`, and `STXFS4`.
`arch/arm/mach-mx35/mx35_luigi_gpio.c` configures all four pads with Schmitt
input hysteresis, pull/keeper enabled, a 100 kohm pull-up, and the i.MX35 1.8 V
driver characteristic. The initial Device Tree translates those settings
directly rather than copying values from an unrelated i.MX35 board.

The codec receives a 24 MHz master clock from the i.MX35 CLKO pin. Lab126's
`arch/arm/mach-mx35/devices.c` selects the 24 MHz CKIH crystal as CLKO's parent
and enables the output. Its codec machine driver then derives the sample-rate
clock with the WM8960 PLL. Upstream Linux 7.0.11 models CKIH but does not expose
the i.MX35 CLKO gate, so the Kindle patch adds that gate as clock index 83. It
selects CKIH and divide-by-one exactly as the old NXP/Lab126 clock code did;
the output remains gated until the WM8960 requests it.

The stock board code maps the headphone jack to the codec's OUT1 pair and the
speaker/line output to OUT2. Current upstream names these pins `HP_L`, `HP_R`,
`SPK_LP`, `SPK_LN`, `SPK_RP`, and `SPK_RN` in
`sound/soc/codecs/wm8960.c`.

## Why speaker enablement is deferred

The WM8960 contains both headphone output stages and stereo class-D speaker
drivers. This is not merely a low-level line output feeding an entirely
independent amplifier. Lab126's codec powers the speaker blocks, enables the
class-D stage, and writes `0x1ff` to both OUT2 volume registers. Its own comment
describes those values as enabling the speaker outputs at their configured
levels. It also programs class-D control registers explicitly.

There is a second control boundary. The separate MC9SDZ60 device exposes a
stock regulator named `SPKR`; `drivers/regulator/mc9sdz60/reg-mc9sdz60.c`
implements it by changing bit 0 of the MCU GPIO-control register. The GPL
source establishes that this is speaker-related, but does not by itself prove
the complete analogue circuit, safe ordering, or gain policy. Mainline must
not manipulate this bit until stock behaviour and the physical path have been
corroborated.

The stock codec also enables WM8960 hardware jack switching with JD2 and
HPSWEN, while separately reporting the headset-detect GPIO on the i.MX35
`CSI_PIXCLK` pad. Enabling that automatic switching before the speaker path is
understood could activate a speaker route without software making an obvious
speaker-selection request.

For those reasons, the first mainline Device Tree:

- routes only `HP_L` and `HP_R` to a `Headphone Jack`;
- supplies no `SPK_*` DAPM route;
- does not describe the MC9SDZ60 `SPKR` control;
- omits `wlf,hp-cfg`, so codec-internal automatic jack switching stays off;
- omits headset-detect GPIO handling because the now-confirmed active-low
  signal is valid only after the stock driver has powered and configured the
  codec GPIO; the desired mainline policy remains unresolved; and
- does not assign guessed MC13892 supply rails to the codec. In particular,
  the live stock PMIC snapshot showed VAUDIO disabled and the vendor audio path
  never requested it.

With no board speaker route, upstream DAPM has no complete path through its
speaker-output widgets and therefore should leave the speaker PGAs and class-D
blocks off. This conclusion comes from the upstream WM8960 DAPM graph and must
still be checked from the running mainline system before any playback test.

## Initial headphone test policy

The initial target is codec discovery, card registration, mixer inspection,
and then quiet headphone playback. Before playing anything, capture `dmesg`,
`/proc/asound`, `aplay -l`, `amixer contents`, the ASoC DAPM state under
debugfs, and the i.MX clock summary. Confirm that no `SPK_*`, speaker-output,
or class-D widget is powered.

Use a disposable pair of headphones and begin with both DAC and headphone
playback switches muted and volumes at their minimum. Unmute only the
headphone path, increase it in small steps, and use a low-amplitude test file.
Do not use `speaker-test` for the first run: its name is generic ALSA
terminology and does not guarantee that only the physical headphone route is
selected.

WM8960 registers are controlled over a write-only two-wire interface, so a
normal I2C register dump cannot reconstruct their state. The vendor driver's
software register cache or its existing debug interface is the appropriate
source on the stock kernel.

## Current mainline status

Linux 7.0.11 successfully binds the upstream WM8960 driver to I2C client
`1-001a` and registers the `Kindle 3 WM8960` ALSA card. Both playback and
capture PCM devices are present, `aplay` enumerates the playback device, and
the expected upstream mixer controls are available through `amixer`. SSI1 is
enabled and its clock is active; the 24 MHz CLKO supplied to the codec is
correctly gated while the idle card has no active stream.

This verifies discovery and the digital audio-card topology, but not audible
playback, signal integrity, analogue gain, or long-running stability. The
headphone path therefore remains bring-up quality, and the internal-speaker
path remains intentionally absent pending the separate safety work described
above.

## Reproduced stock baseline

The following observations were collected read-only from the running stock
2.6.26-rt-lab126 system on 17 August 2026, before headphones were inserted or
audio was played:

- `/sys/bus/i2c/devices/1-001a/name` reports `WM8960`, proving the codec is on
  the second i.MX I2C adapter rather than the first.
- `/proc/asound/cards` reports `mx35luigi (WM8960)`, with one full-duplex PCM
  exposed for playback and capture.
- the vendor `wm8960_power_status` attribute reports `0`, consistent with the
  codec's explicit powered-off idle sequence.
- the vendor codec cache reports `0x000` for `POWER1`, `POWER2`, and `POWER3`.
  `CLASSD1` is `0x037`, whose speaker-enable bits 6 and 7 are both clear.
  The OUT1 headphone registers are `0x000`/`0x010`, and the OUT2 speaker
  registers are likewise `0x000`/`0x010`. These are the exact values written
  by the vendor driver's powered-off path, not values read back from the
  write-only codec.
- the vendor `headset_status` attribute reports `1`. Its implementation
  returns the raw `CSI_PIXCLK` GPIO level, and the vendor headset-event logic
  treats this high level as detached.
- the stock mixer exposes separate headphone and speaker volume controls.
  This does not weaken the conservative mainline policy: the speaker route,
  class-D path, and separate MC9SDZ60 `SPKR` control remain omitted.

A second read-only observation pass and a 15-second, 48 kHz stereo test tone
at -50 dBFS were then performed with low-volume powered speakers connected to
the headphone jack:

- inserting the plug while the codec was off left `headset_status` at `1` and
  `wm8960_power_status` at `0`. The codec GPIO is configured as a jack-detect
  output during stream setup, so its idle level does not report an inserted
  plug.
- while the PCM was running, `headset_status` changed to `0` and
  `wm8960_power_status` changed to `1`. This establishes active-low insertion
  once the codec is operating.
- stock set both OUT1 headphone registers to `0x16d`; ALSA reported headphone
  volume 109 of 127. The deliberately attenuated source, rather than a low
  stock analogue gain, provided the safety margin for this test.
- even with a plug detected, stock set both OUT2 registers to `0x1ff` and
  `CLASSD1` to `0x0f7`, including its speaker-enable bits. The vendor driver
  relies on the WM8960 hardware jack-switching configuration and an additional
  MC9SDZ60 speaker control instead of leaving the codec's speaker path off.
  This makes the mainline headphone-only graph materially more conservative
  than the stock sequencing.
- the -50 dBFS tone was audible through the externally powered speakers
  connected to the headphone jack, while no sound was audible from the
  Kindle's internal speakers. This physical result is consistent with the
  stock hardware jack switching and/or the separate MC9SDZ60 speaker boundary
  preventing the programmed class-D path from reaching the internal speakers;
  it does not distinguish which boundary was responsible.
- ten seconds after the PCM completed, the vendor delayed-work path returned
  `wm8960_power_status` to `0`. The detected GPIO remained low until that
  power-off sequence completed.

The powered speakers also provided a repeatable qualitative power-state cue:
they produced substantial interference while the codec/output path was off,
and that interference disappeared when playback powered the codec. This is
useful during bring-up because it agrees with the vendor power-status
transition and delayed shutoff. It is not a voltage measurement and does not
by itself identify whether the noise comes from a floating/high-impedance
codec output, board coupling, grounding, or the external amplifier; it must
not be used to infer a regulator connection or safe power sequence.

## Evidence index

All stock paths below are relative to the exact external archive worktree
`../kernel-worktrees/amazon-kindle3-3.4.3`:

- `sound/soc/imx/mx35luigi_wm8960.c`: address, I2S master/slave roles, SSI and
  AUDMUX ports, 24 MHz PLL input, and board audio routes.
- `sound/soc/codecs/wm8960.c`: stock power sequence, headphone and speaker
  gains, class-D programming, hardware jack switching, and codec debug state.
- `arch/arm/mach-mx35/mx35_luigi_gpio.c`: AUDMUX and headset-detect pin mux and
  pad settings.
- `arch/arm/mach-mx35/devices.c` and `arch/arm/mach-mx35/clock.c`: CLKO parent,
  rate, divider, and enable policy.
- `drivers/regulator/mc9sdz60/reg-mc9sdz60.c`: the separate `SPKR` control and
  its MCU GPIO register bit.

The provenance matrix classifies the Luigi machine driver and the archive's
WM8960 codec file as `amazon-only` among the compared source states. In
contrast, the MC9SDZ60 core and `SPKR` regulator implementation are
byte-identical to the known Freescale 2009-03-17 BSP snapshot. The i.MX35
clock/device files are modified or from a later unresolved source. These are
source-presence classifications, not proof of authorship; see
`../kernel-worktrees/meta-provenance` for the trust model and lineage report.

Upstream Linux paths inspected at version 7.0.11:

- `sound/soc/codecs/wm8960.c`: independent headphone and class-D DAPM paths,
  reset defaults, mixer controls, and PLL programming.
- `sound/soc/fsl/fsl-asoc-card.c`: generic i.MX/WM8960 card setup and AUDMUX
  support.
- `drivers/clk/imx/clk-imx35.c`: existing i.MX35 clock tree and the absence of
  CLKO before the board patch.
- `Documentation/devicetree/bindings/sound/wlf,wm8960.yaml` and
  `fsl-asoc-card.yaml`: codec and machine-card Device Tree interfaces.

Manufacturer and upstream references:

- [Cirrus Logic WM8960 product page](https://www.cirrus.com/products/wm8960)
- [Cirrus Logic class-D audio design guidance](https://statics.cirrus.com/pubs/whitePaper/WP_Class_D_Audio_Design_Tips_indd.pdf)
- [upstream WM8960 Device Tree binding](https://www.kernel.org/doc/Documentation/devicetree/bindings/sound/wlf,wm8960.yaml)
- [NXP i.MX35 reference manual](https://www.nxp.com/docs/en/reference-manual/IMX35RM.pdf)
