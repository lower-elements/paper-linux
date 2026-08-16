# Amazon Kindle 3

The Kindle 3 board has separate configurations for its original vendor
kernel and for mainline development:

- `vendor-2.6.26` contains the established chroot/rootfs integration for the
  Lab126 kernel.
- `mainline` is the home for the Device Tree, kernel configuration, RAM-boot
  assets, and patches developed for upstream Linux.

Files under `common` must describe hardware or behavior shared by both
kernel lines.
