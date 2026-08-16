# Linux 2.6.26 compatibility patches

This layer adapts a modern musl/BusyBox userspace to Linux 2.6.26-era vendor
kernel headers, syscalls, input events, device discovery, and wireless
extensions. It is selected by the Kindle 3 vendor-kernel configuration and
must not be selected by mainline configurations.
