#!/bin/sh
set -eu

TARGET_DIR="$1"
INITTAB="${TARGET_DIR}/etc/inittab"

# Buildroot's generic getty and board/raspberrypi/post-build.sh can both
# create tty1 gettys. On CarBerry the kernel console is tty1, so two gettys
# race for the same keyboard input. Normalize the final image to exactly one
# tty1 login after the Raspberry Pi post-build hook has run.
if [ -f "$INITTAB" ]; then
    sed -i '/^tty1::respawn:\/sbin\/getty /d' "$INITTAB"
    printf '%s\n' \
        'tty1::respawn:/sbin/getty -L tty1 0 vt100 # CarBerry HDMI console' \
        >> "$INITTAB"
fi
