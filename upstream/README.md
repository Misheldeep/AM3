# Upstream sources

## Buildroot

Target Buildroot release: `2025.02.16`.

The SHA256 of the archived upstream tarball is stored in:

`buildroot-2025.02.16.tar.xz.sha256`

The tarball itself is kept outside ordinary Git history and is intended to be
stored as a disaster-recovery/release artifact.

The Peugeot package is integrated into the original Buildroot tree using:

`buildroot/patches/0001-add-peugeot-carberry-package.patch`

## CarBerry daemon

`carberry_d_1.5.tar` is the original CarBerry daemon 1.5 archive.

Verified archive SHA1:

`4dfffaa11cb650771c3b5799a5c1e864646527dd`

Original upstream `carberry.c` SHA256:

`75d95d379eb29d9a76ed83a39bb985432fb97434e6004d1e7f77559ea6c08bfc`

The working copy under:

`buildroot/package/peugeot-carberry/vendor/carberry.c`

has its line endings normalized from upstream CRLF to LF.

Peugeot-specific source modifications are kept separately as patches.

Current daemon patch:

`0001-carberryd-bind-loopback-only.patch`

It changes the TCP listener from all interfaces to loopback only:
`127.0.0.1:7070`.
