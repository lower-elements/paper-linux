#!/bin/sh

set -eu
alias mount='mount -v'
alias umount='umount -v'

ROOT="${1:-$(dirname "$(realpath "$0")")}"

echo "Setting up bind mounts ..."
mount --rbind /dev "$ROOT/dev"
mount --rbind /proc "$ROOT/proc"
mount --rbind /sys "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/tmp"

exec chroot "$ROOT" /bin/sh
