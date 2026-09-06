# AM3

> **Project origins**
>
> This project is a continuation and reimplementation of the original Peugeot
> multimedia/CAN integration developed by
> [@mikebutrimov](https://github.com/mikebutrimov).
>
> The original system used an Arduino for vehicle-side CAN/control logic and an
> Android application for the user interface. That implementation was the
> starting point for this project and for the practical Peugeot integration work
> that followed.
>
> Original project:
> [automobile-machine-2](https://github.com/mikebutrimov/automobile-machine-2)
>
> Many thanks to @mikebutrimov for the original implementation and for starting
> this project in the first place.

Embedded software for a CarBerry-equipped Raspberry Pi used in a Peugeot
automotive CAN/audio integration project.

The current implementation reimplements the system on a standalone
Raspberry Pi + CarBerry platform running a small Buildroot Linux image.

## Target hardware

- Raspberry Pi Model B Rev 2
- Raspberry Pi revision: `000e`
- CPU: ARM1176JZF-S / ARMv6
- CarBerry HW: `1.00`
- CarBerry firmware: `1.19`

## Build environment

Current production/development baseline:

- Buildroot: `2025.02.16`
- Linux kernel: `6.6.28`
- Buildroot internal toolchain
- GCC: `13.4.0`
- Binutils: `2.43.1`
- glibc
- BusyBox init
- Python: `3.12.13`

## CAN architecture

Permanent channel assignment:

- **CH1** = Peugeot CAN, `125 kbit/s`
- **CH2** = MiniDSP Harmony CAN, `250 kbit/s`

Do not swap the channels.

### Production RX filters

CH1:

- `0x21F` — Peugeot steering-wheel controls
- `0x3E5` — factory display/HU buttons

CH2:

- `0x201` — MiniDSP Harmony → remote/status
- `0x202` — remote/controller → MiniDSP Harmony

### Production TX whitelist

CH1:

- `0x0A4`
- `0x165`
- `0x325`
- `0x3A5`
- `0x3E5`

CH2:

- `0x202`

The bridge also provides a diagnostic mode in which normal production RX
filtering can temporarily be replaced by catch-all CAN sniffing.

## CarBerry communication

The current bridge communicates with the CarBerry PIC **directly over
`/dev/ttyAMA0` at 115200 8N1**.

Runtime CAN transmission is pipelined and does not wait synchronously for an
`OK` response after every individual CAN TX command.

Bench testing with CH1↔CH2 loopback confirmed sustained pipelined operation at
the UART wire-rate with 500 consecutive CAN TX commands, preserving order and
without observed loss or duplication.

Only one process may own `/dev/ttyAMA0`.

The old `carberry_d` daemon is therefore **not started during normal operation**.

## Legacy CarBerry daemon

The repository still preserves the official `carberry_d 1.5` source and the
working vendor copy for diagnostics and historical reference.

The upstream archive is stored under `upstream/`.

The vendor `carberry.c` copy has CRLF line endings normalized to LF.
Peugeot-specific changes are kept separately where applicable.

A manual diagnostic launcher is retained as:

`/etc/init.d/carberry-diagnostic`

It must not be run at the same time as the direct-UART Peugeot bridge.

## Peugeot bridge

The main bridge is:

`/opt/carberry/peugeot_bridge.py`

It provides:

- direct CarBerry UART ownership;
- Peugeot CH1 CAN receive/decode;
- MiniDSP Harmony CH2 receive/decode;
- pipelined CAN transmission;
- production CAN RX filters;
- software TX whitelist;
- steering-wheel control decoding;
- factory display-button decoding and emulation;
- MiniDSP Harmony state tracking and control;
- GPIO joystick support;
- reserved GPIO control interface for the Bluetooth module;
- Unix-domain control/status API;
- diagnostic CAN sniffing mode.

The local command-line client is:

`peugeotctl`

The API socket is:

`/run/peugeot-bridge.sock`

## GPIO

Current joystick assignment:

| Function | BCM GPIO | Physical pin |
|---|---:|---:|
| UP | 10 | 19 |
| DOWN | 9 | 21 |
| LEFT | 11 | 23 |
| RIGHT | 8 | 24 |
| OK | 7 | 26 |
| MENU | 25 | 22 |
| DARK | 4 | 7 |

Reserved Bluetooth-control GPIO:

| Function | BCM GPIO | Physical pin |
|---|---:|---:|
| Play/Pause | 22 | 15 |
| Next | 23 | 16 |
| Previous | 24 | 18 |

GPIO input handling uses the Linux GPIO character-device interface through
`/dev/gpiochip0`.

Direct BCM2835 access through `/dev/mem` is deliberately not used.

Bluetooth GPIO output driving is currently disabled until the electrical button
interface of the QCC5181 module is verified.

## Autoboot

Normal boot starts:

- `S00carberry-boot-led`
- `S35peugeot-bridge`
- `S41carberry-network`

`S35peugeot-bridge` starts a supervisor which runs the direct-UART bridge and
restarts it if necessary.

The legacy `carberry_d` daemon is intentionally absent from automatic startup.

## Current verified state

Latest hardware-verified bridge baseline:

- verified commit: `ee42ee0`
- tag: `dev04-alpha6-autoboot`
- bridge version: `dev04-direct-uart-alpha6-autoboot`

Verified Buildroot disk image SHA256:

`00e09b0d3172fa663d7233323d08e2977fe0ca91afbd28040725ddea7400c242`

The disk image itself is intentionally not stored in ordinary Git history.

The alpha6 image has been verified on the target Raspberry Pi + CarBerry
hardware after a cold boot:

- bridge supervisor starts automatically;
- legacy `carberry_d` does not start;
- the bridge is the only owner of `/dev/ttyAMA0`;
- CarBerry UART setup completes successfully;
- CH1 opens at 125 kbit/s;
- CH2 opens at 250 kbit/s;
- production RX filters are applied;
- `/dev/gpiochip0` initializes successfully;
- the local API and `peugeotctl status` are operational.

## Current development status

Implemented but still awaiting final vehicle/hardware validation:

- steering-wheel volume control of MiniDSP Harmony Master volume;
- steering-wheel roller control of Harmony Sub volume;
- Harmony Master/Sub page-state tracking;
- physical joystick board;
- QCC5181 Play/Pause / Next / Previous GPIO interface.

Automatic Harmony control remains disabled by default until the remaining
in-vehicle Master/Sub selector test is completed.

## Storage roadmap

Development currently uses a writable ext4 root filesystem for persistent CAN
captures and rapid iteration.

Possible future production direction:

- immutable SquashFS root;
- no normal writes to the SD card;
- runtime state/logging in RAM;
- optional diagnostic logging to a specially labelled USB flash drive.

## Security note

The development Buildroot image currently uses the default root password:

`carberry`

This credential is part of the public build configuration and must therefore
be considered public. Do not expose SSH access from an untrusted network
without changing the credentials or authentication configuration.

## Repository notes

This repository contains source code, Buildroot configuration, overlays,
patches and development checkpoints.

Generated Buildroot output directories and SD-card images are intentionally not
stored in normal Git history.
