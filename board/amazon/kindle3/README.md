# Amazon Kindle 3

The Kindle 3 board has separate configurations for its original vendor
kernel and for mainline development:

- `vendor-2.6.26` contains the established chroot/rootfs integration for the
  Lab126 kernel.
- `mainline` is the home for the Device Tree, kernel configuration, RAM-boot
  assets, and patches developed for upstream Linux.

The mainline configuration carries an initial bring-up Device Tree. It
inherits the upstream i.MX35 SoC description and enables the Kindle 3 RAM,
debug UART, eMMC, USB peripheral controller, keypad, five-way and volume
buttons, I2C controllers, and initial MC13892 PMIC support over SPI. The PMIC
RTC, ADC and power-slider input are available; complete regulator policy,
display, Wi-Fi and other active peripherals remain disabled until their board
wiring and sequencing have been verified.

See [power-management.md](power-management.md) for the evidence gathered from
the stock Wi-Fi-only hardware, Lab126 source, component documentation and
board teardowns. It distinguishes confirmed regulator assignments from
unverified physical connections that must not yet be encoded in the Device
Tree.

Files under `common` must describe hardware or behavior shared by both
kernel lines.
