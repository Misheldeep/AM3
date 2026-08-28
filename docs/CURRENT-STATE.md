# Current state

Checkpoint: 2026-08-28
Development image: dev02

## Hardware target

- Raspberry Pi Model B Rev 2
- Revision: 000e
- CPU: ARM1176JZF-S / ARMv6
- CarBerry HW: 1.00
- CarBerry firmware: 1.19

## Build

- Buildroot: 2025.02.16
- Kernel: 6.6.28
- Internal Buildroot toolchain
- GCC: 13.4.0
- Binutils: 2.43.1
- glibc
- BusyBox init
- Python: 3.12.13

## CarBerry daemon

Based on official carberry_d 1.5.

Official archive SHA1:

4dfffaa11cb650771c3b5799a5c1e864646527dd

Original upstream carberry.c SHA256:

75d95d379eb29d9a76ed83a39bb985432fb97434e6004d1e7f77559ea6c08bfc

The working vendor copy has only CRLF -> LF normalization.

Peugeot-specific modification is a separate Buildroot patch:

0001-carberryd-bind-loopback-only.patch

Verified build result:
- vendor source: INADDR_ANY
- Buildroot work tree: htonl(INADDR_LOOPBACK)
- TCP listener therefore intended for 127.0.0.1:7070 only

## CAN

Permanent channel assignment:

- CH1 = Peugeot CAN, 125 kbit/s
- CH2 = MiniDSP Harmony CAN, 250 kbit/s

ALIGN RIGHT.
Passive receive-all masks/filters during sniffing.

## Bridge readiness

The bridge waits for an actual CarBerry/PIC command response rather than only
for TCP port 7070 to accept connections.

Readiness command:

CAN MODE

Response timeout:

6.0 seconds

This supersedes the earlier 2.0 second timeout. carberry_d itself can spend
about 5 seconds waiting for a UART response from an absent/unready PIC.

## Early status LED

S00carberry-boot-led directly opens /dev/ttyAMA0, sends:

GPLED LED2 SET\r

then drains, closes UART and exits before carberry_d starts.

Known:
- the helper performs the UART write and releases the UART;
- GPLED state is latched by the PIC after a successfully accepted command.

Not yet verified:
- successful acceptance of this very early RED SET command by the PIC during
  a cold boot of this dev02 image on a real CarBerry HAT.

Expected:
- PIC startup should be substantially faster than Raspberry Pi startup to S00,
  so this is expected to work, but remains an unverified hardware test.

## Networking (dev03 WIP)

- CarBerry: 192.168.200.1/24
- Service laptop: 192.168.200.2/24
- Fixed service address 192.168.200.1/24 is configured asynchronously
- A second IPv4 address is requested by DHCP when a server is available
- DHCP runs entirely in the background and must never delay boot
- The fixed service address remains present alongside the DHCP lease; if a DHCP
  gateway itself is 192.168.200.1, the service address is suppressed to avoid
  black-holing that gateway
- Dropbear SSH enabled

## dev02 image

SHA256:

2ed58174c5dbae0d8c1870e1b2bf85488793748f5f61d73b1a875a6c02e19575

A Windows-side copy was verified to have the same SHA256.

## Storage direction

Current development system remains writable ext4 because persistent CAN
captures and rapid iteration are useful during reverse engineering.

Future production direction, only after protocol logic is stable and the
bridge is migrated from Python to C:

- immutable SquashFS root;
- no normal SD-card writes;
- runtime state/logs in RAM;
- specially labelled USB flash drive may automatically enable diagnostic
  logging and CAN sniffing.

No CAN pre-trigger/ring buffer is currently planned.


## Console getty normalization (dev03 WIP)
The Raspberry Pi post-build hook and Buildroot generic getty can both create a tty1 getty. CarBerry now runs `board/peugeot/carberry/post-build.sh` after the Raspberry Pi hook and normalizes `/etc/inittab` to exactly one tty1 getty.
