# dev03 architecture (work in progress)

Status: source implementation prepared, not yet built or tested on the real CarBerry HAT.
Known-good released state remains dev02 until hardware verification.

## Process boundaries

- `carberry_d` remains the low-level CarBerry/PIC transport daemon on `127.0.0.1:7070`.
- `peugeot_bridge` is the only application process that talks to `carberry_d`.
- `peugeot_bridge` owns CAN RX/TX, protocol state, scheduling and a local high-level API.
- `peugeot-test`, `peugeotctl` and `peugeot-sniffer` are API clients. They never open `carberry_d` directly.

Local API socket:

`/run/peugeot-bridge.sock`

Transport: newline-delimited UTF-8 JSON over Unix-domain stream socket.

## Capture ownership

The bridge does not create numbered CAN capture files.

When no subscriber is present it processes CAN internally and does not copy the full bus stream anywhere.

`peugeot-sniffer` explicitly subscribes to CAN events and owns:

- capture numbering;
- capture file creation;
- filesystem selection;
- fsync policy;
- future USB diagnostic-media policy.

A stalled subscriber must not stall the bridge. Per-client bridge output is bounded; events may be dropped for a slow subscriber and a `dropped` event is reported.

No CAN pre-trigger/ring buffer is planned.

## Scheduler

The bridge is passive by default. CAN TX starts only after API state/commands request it.

Current protocol mechanisms:

- DISPLAY `0x3E5`: one-shot button frames; optional periodic ZERO every 500 ms.
- HU `0x165`: current-box header `C0 <source> 20 00`, period 100 ms while enabled.
- Playback `0x325`: play/pause status, period 500 ms while active.
- Track position `0x3A5`: period 1000 ms while playing.
- Track metadata `0x0A4`: AM2-compatible 20-byte artist + 20-byte title sequence.

Production position is clamped to duration by default. Diagnostic API can explicitly allow overflow.

## Terminology

- STALK: physical steering-column remote, input from vehicle (e.g. `0x21F`).
- DISPLAY: MENU/EXIT/OK/DARK/arrows sent as `0x3E5`.
- HU: source/head-unit state and track information (`0x165`, `0x325`, `0x3A5`, `0x0A4`).

## Test client

`peugeot-test` is separate from the bridge.

Manual keys:

- arrows: DISPLAY arrows;
- Enter/Space: manual OK only;
- Esc: EXIT;
- M: MENU;
- D: DARK;
- `[` / `]`: decrement/increment HU source byte.

Automatic scenarios never send OK.

- `5`: CP1251 Cyrillic display test, then duration=5 s / current-position overflow to about 10 s with bridge clamp intentionally disabled.
- `6`: scan `0x165 C0 C0 20 00` through `0x165 C0 CF 20 00`; each step is self-labelled via track metadata.

## Still intentionally deferred

- automatic USB discovery/mount/label/filesystem validation for diagnostic mode;
- STALK-to-Harmony production actions;
- Android/AVRCP metadata transport;
- final choice of display charset/transliteration policy;
- final named HU source profiles after the C0..CF visual scan.

## Local console and Ethernet

- The local login getty is explicitly bound to `tty1`. This avoids creating
  both a generic `/dev/console` getty and the Raspberry Pi post-build HDMI
  `tty1` getty when the kernel console itself is `tty1`.
- `eth0` always receives the fixed service address `192.168.200.1/24` when
  available.
- A BusyBox `udhcpc` client runs asynchronously and may add a second IPv4
  address plus a default route from DHCP. DHCP must never block boot.
- DHCP lease handling never flushes the fixed service address. If the DHCP
  gateway itself is `192.168.200.1`, the fixed service address is temporarily
  suppressed to avoid an address/gateway collision and is restored when the
  lease is removed.


## Console
The final CarBerry post-build hook removes duplicate tty1 gettys and appends exactly one `tty1` login entry after the Raspberry Pi post-build hook.
