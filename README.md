# Peugeot CarBerry

Embedded software for a CarBerry-equipped Raspberry Pi used in a Peugeot automotive CAN/audio project.

## Target hardware

- Raspberry Pi Model B Rev 2
- Raspberry Pi revision: `000e`
- CPU: ARM1176JZF-S / ARMv6
- CarBerry HW: 1.00
- CarBerry firmware: 1.19

## Build environment

- Buildroot: `2025.02.16`
- Linux kernel: `6.6.28`
- Buildroot internal toolchain
- GCC: `13.4.0`
- Binutils: `2.43.1`
- glibc
- BusyBox init
- Python: `3.12.13`

## CAN wiring

Permanent channel assignment:

- CH1 = Peugeot CAN, 125 kbit/s
- CH2 = MiniDSP Harmony CAN, 250 kbit/s

Do not swap the channels.

## CarBerry daemon

Based on official `carberry_d 1.5`.

The official upstream archive is preserved under `upstream/`.

The working vendor copy of `carberry.c` has line endings normalized from CRLF to LF.
Peugeot-specific modifications are kept as separate patches.

Current daemon modification:

- TCP port 7070 binds to `127.0.0.1` only instead of all interfaces.

## Current development image

Current known-good source state: `dev02`, 2026-08-28.

Image SHA256:

`2ed58174c5dbae0d8c1870e1b2bf85488793748f5f61d73b1a875a6c02e19575`

The disk image itself is intentionally not stored in ordinary Git history.

## Storage roadmap

Development currently uses a writable ext4 root filesystem for persistent CAN captures and rapid iteration.

Future production direction, after protocol discovery and migration of the bridge from Python to C:

- immutable SquashFS root;
- no normal writes to the SD card;
- runtime state/logging in RAM;
- optional diagnostic logging to a specially labelled USB flash drive.

