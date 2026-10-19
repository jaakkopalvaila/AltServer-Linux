# AltServer-Linux-NG

A fork of [NyaMisty/AltServer-Linux](https://github.com/NyaMisty/AltServer-Linux) that installs and
signs apps for **iOS 26.4 / 27** devices over Wi-Fi. Upstream has been unmaintained since 2023
(only automated keepalive commits) and its v0.0.5 binary fails on modern iOS for two independent
reasons, both fixed here.

Layout:

- This repository, branch `ng`: the AltServer-Linux tree.
- Submodule `upstream_repo` = [jaakkopalvaila/AltServer-Windows](https://github.com/jaakkopalvaila/AltServer-Windows),
  branch `ng-fixes`: vendored AltSign + ldid with the signing and sign-in fixes.
- Submodule `libraries/libimobiledevice` = [jaakkopalvaila/libimobiledevice](https://github.com/jaakkopalvaila/libimobiledevice),
  branch `ng-fixes`: netmuxd address-format fix.
- All other submodules track their upstream repositories unchanged.

Clone with `git clone --recursive -b ng https://github.com/jaakkopalvaila/AltServer-Linux`.

## Fix 1 — code signature rejected by iOS 26.4+

### Symptom

`AltStore` installs successfully but the app flashes and closes. `idevicesyslog` shows:

```
kernel  AMFI: cmsBlobVerifyWithAgilityHash failed ... Unrecoverable CT signature issue
launchd Bad executable (85)
```

### Root cause

Signing is done by a copy of `ldid` vendored inside AltServer-Windows, pinned to a 2022 commit.
That version writes the CMS *hash agility* signed attribute (OID `1.2.840.113635.100.9.2`) with the
SHA-256 code directory hash **truncated to 20 bytes**. Apple requires the **full 32 bytes**, so
CoreTrust cannot match the attribute against the alternate code directory and rejects the signature.

Measured on a test binary signed with each version:

| | old ldid (2022) | new ldid |
| --- | --- | --- |
| entries in the `9.2` attribute | 1 | 2 |
| SHA-1 entry | absent | 20 bytes, matches primary code directory |
| SHA-256 entry | **20 bytes, truncated** | **32 bytes, exact match** |
| designated requirement | empty | generated |

The empty designated requirement was a second defect flagged in upstream issue #131.

### Fix

Upstream Riley Testut already fixed this: AltServer for Windows 1.7.4 (2026-03-24) shipped
*"Fixed apps crashing on launch on iOS 26.4"* via commit `62a7a2b`
*"[ldid] Updates ldid to match AltStore + AltServer macOS' version"*. That commit lives on the
branches `26.4_fix` / `1.7.4` / `develop` of rileytestut/AltServer-Windows, which the Linux fork
never picked up.

This fork takes `ldid/ldid.cpp` and `ldid/ldid.hpp` from commit `2e6783d` and adapts the caller:

- `AltSign/Signer.cpp` – the new `ldid::Sign` takes a single `ldid::Progress` object instead of two
  `Functor` callbacks.
- `AltSign/Signer.cpp` – the new `DiskFolder` asserts on a missing trailing path separator, so the
  bundle path gets a `"/"`. Upstream appends a Windows backslash here; the Linux source rewriter
  does not translate backslashes, so the forward slash is required.

The new ldid compiles against the existing LibreSSL toolchain with no errors, so the OpenSSL 3
upgrade that accompanied it on Windows is not needed.

## Fix 2 — Apple blocks the sign-in

### Symptom

Since early September 2026, `gsa.apple.com` answers **HTTP 503** with an HTML page to every
request whose `X-Mme-Client-Info` header contains the substring `com.apple.dt.Xcode`. This broke
AltServer, AltStore and SideStore sign-in simultaneously.

### Fix

Three changes, mirroring altstoreio/AltStore#1790 and rileytestut/AltSign #51 / #52:

- `src/AnisetteDataManager.cpp` – replace every `com.apple.dt.Xcode` with `com.apple.akd` in the
  client info string returned by the anisette server, before the `AnisetteData` object is built.
  All four call sites that send the header read it from that object, so one substitution covers
  them all. This also means an out-of-date anisette server no longer matters.
- `AltSign/AppleAPI+Authentication.cpp` – GrandSlam `User-Agent` becomes
  `AuthKit/1 (Macintosh; OS X 26.5.2) (com.apple.dt.Xcode/26.0)`. The block applies only to the
  client info header, so the Xcode token is still allowed here.
- `AltSign/AppleAPI.cpp` – `gsaClient()` returns a brand new `http_client` on every call rather
  than a cached member, so no GSA request travels over a reused keep-alive connection. The bundled
  cpprestsdk has no `http_client_config::set_keep_alive()`, so a fresh connection pool per request
  is how connection reuse is avoided.

## Fix 3 — netmuxd address format

netmuxd >= 0.3 reports the device `NetworkAddress` in Linux sockaddr layout (a little-endian
`sa_family_t` first: `02 00` = AF_INET, `0a 00` = AF_INET6). The bundled 2022 libimobiledevice
assumed the BSD layout (byte 0 = `sa_len`, byte 1 = family) and failed with
`Unsupported address family 0x00`.

`libraries/libimobiledevice/src/idevice.c` now detects both layouts, so `USBMUXD_SOCKET_ADDRESS`
can point straight at netmuxd. A BSD-format proxy in front of netmuxd keeps working as well.

## Fix 4: Remote AltServer setup (`PairingFileRequest`)

### Symptom

In AltStore Classic 2.3, **Set up Remote AltServer…** fails with "unable to pair" and AltServer
logs `Failed to handle request:AltServer does not support this request.`

### Cause

AltStore Classic 2.3 asks AltServer for a RemotePairing (RPPairing) file, the iOS 17+ pairing
record with a `private_key`. AltStore keeps it in its keychain and uses it to install and refresh
apps on the device itself over LocalDevVPN. AltServer for macOS creates it in
`DevicePairingManager.swift` with idevice's `tunnel_pair_usb()`; AltServer-Linux had no handler for
the request.

### Fix

- `upstream_repo/AltServer/ClientConnection.cpp` handles `PairingFileRequest` and answers with a
  `PairingFileResponse` carrying the file. `upstream_repo/AltServer/DevicePairingManager.h` declares
  the pairing interface; the `DevicePairingManager.cpp` next to it is the Windows version, which
  throws `UnknownRequest`, and the Linux build replaces it with `src/DevicePairingManager.cpp`.
- `src/DevicePairingManager.cpp` is a C++ port of `DevicePairingManager.swift`: it looks up the
  device by UDID in usbmuxd, calls `tunnel_pair_usb()` (the device shows a Trust prompt for
  "AltServer on <hostname>") and serializes the pairing file. It reads `USBMUXD_SOCKET_ADDRESS` by
  libusbmuxd's rules (`UNIX:<path>`, `<host>:<port>`, otherwise `/var/run/usbmuxd`), so the device
  lookup and the pairing connection use the same usbmuxd as the rest of AltServer-Linux. It pairs
  only over the device's USB connection, not a network entry for the same UDID. The pairing
  runs on its own thread with an 8 MiB stack: `tunnel_pair_usb()` polls idevice's pairing future on
  the calling thread, and that overflows the 128 KiB stack musl gives cpprestsdk's pool threads.
- `libraries/idevice` is [idevice](https://github.com/jkcoxson/idevice) v0.1.68, built with the
  features `usbmuxd,tunnel_tcp_stack,ring` (`makefiles/idevice-build/idevice-features.txt`).
  idevice's static library also exports libplist-compatible `plist_*` functions and the Rust standard
  library, which would clash with the vendored libplist. `makefiles/idevice-build/idevice.mak`
  therefore prelinks it into one object (`ld -r`) and keeps only the functions in
  `idevice-api.txt` global (`objcopy --keep-global-symbols`).

## Prebuilt binaries

Static binaries for x86_64, aarch64, armv7 and i586 are attached to the
[Releases](https://github.com/jaakkopalvaila/AltServer-Linux/releases) page. Every push to `ng` also
produces them as GitHub Actions artifacts, which expire after 90 days.

## Building

Builds run in the prebuilt upstream Alpine image. On an Apple Silicon Mac, Rosetta runs the amd64
container at native speed and a clean build takes about 30 seconds.

The builder images have no Rust toolchain, so idevice (Fix 4) is built first with
`makefiles/idevice-build/build-idevice-cross.sh`, which runs cargo-zigbuild in the
`ghcr.io/rust-cross/cargo-zigbuild` image and prints the path of the static library:

```bash
git clone --recursive -b ng https://github.com/jaakkopalvaila/AltServer-Linux
cd AltServer-Linux
lib=$(makefiles/idevice-build/build-idevice-cross.sh x86_64-unknown-linux-musl)
mkdir -p build
docker run --rm --platform linux/amd64 -v "$PWD:/workdir" -w /workdir \
  ghcr.io/nyamisty/altserver_builder_alpine_amd64:latest \
  bash -c "cd build && make -f ../Makefile -j8 IDEVICE_FFI_LIB=/workdir/${lib#$PWD/}"
```

The result is a statically linked `build/AltServer-x86_64`. Other architectures use the
`altserver_builder_alpine_{aarch64,armv7,i386}` images with the Rust targets
`aarch64-unknown-linux-musl`, `armv7-unknown-linux-musleabihf` and `i686-unknown-linux-musl`.
Without `IDEVICE_FFI_LIB`, the Makefile runs `cargo build` itself, which needs cargo in the build environment
(tested with Rust 1.93 and 1.98).

Sanity-check that the fixes are present:

```bash
strings build/AltServer-x86_64 | grep -c "missing path separator"   # Fix 1, new ldid      -> 1
strings build/AltServer-x86_64 | grep -c "Sanitized client info"    # Fix 2, sign-in        -> 1
grep -c idevice_sockaddr_len libraries/libimobiledevice/src/idevice.c  # Fix 3, netmuxd  -> >= 1
strings build/AltServer-x86_64 | grep -c "Generated pairing file"   # Fix 4, pairing file   -> 1
```

## Running

```bash
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 \
ALTSERVER_ANISETTE_SERVER=http://127.0.0.1:6969 \
./AltServer-x86_64 -d -u <UDID> -a <apple-id> -p <password> AltStore.ipa
```

`-d` enables debug output. Wi-Fi installs need netmuxd listening on that address (see Fix 3).

To check sign-in without real credentials, run it with a throwaway account. HTTP 503 means Apple is
still blocking; a GSA error such as `-20101` or `-20209` means the request reached the service.
