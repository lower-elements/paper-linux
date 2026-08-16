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
buttons, plus bare I2C controllers. Power-management, display, Wi-Fi and other
active peripherals remain disabled until their board wiring and bindings have
been verified.

Files under `common` must describe hardware or behavior shared by both
kernel lines.
