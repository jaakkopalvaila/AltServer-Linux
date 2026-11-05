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

## Fix 4 — October 2026: -80045 and two-factor codes

Since 2026-10-13 Apple checks that `X-Mme-Client-Info` matches the platform the anisette data was
provisioned as. Measured against a local `dadoum/anisette-v3-server` with a made-up account:

| client info sent | Apple's answer |
| --- | --- |
| the anisette server's own Mac string | `-80045` "This action cannot be completed at this time" |
| Android (`<Pixel 7> <Android;14;...>`) | HTTP 503 |
| Windows iTunes (`<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>`) | normal account check |

Release `ng-2026-10-20` therefore defaulted to the Windows AuthKit identity. **Since 2026-10-21
that no longer works; see Fix 5.** The client info now defaults to the anisette server's own value.
`ALTSERVER_CLIENT_INFO` overrides it: `windows` selects the Windows identity, and any other value is
sent as is. Whatever is chosen still goes through the `com.apple.dt.Xcode` sanitizer. AltStore on the
device receives the same client info with the anisette data, so this also governs AltStore's own
sign-in. `ALTSERVER_GSA_USER_AGENT` overrides the GrandSlam User-Agent.

The same change made Apple push the verification code to trusted devices on its own and answer the
`complete` step with `hsc 409` and no `au`. AltSign treated that as "no 2FA needed" and failed at
`apptokens` with `-22411`, without ever showing a code prompt. The fork ports
rileytestut/AltSign#59: a 409 without `au` goes down the trusted-device path without requesting a
second code, and anisette data is refreshed before the code is validated and before the second
sign-in.

**AltStore still shows -22411 without a code prompt?** That bug is inside the AltStore app. Sign in
once from AltServer-Linux on the command line and enter the code there; Apple then trusts the
anisette identity, and AltStore, which borrows it from AltServer, signs in without a code.

## Fix 5 — 21 October 2026: -80046, Mac anisette required

Around 2026-10-21 Apple tightened the check again. Windows and Android identities now pass the
password step but fail after the verification code, at `apptokens`, with `-80046` "This action
cannot be completed at this time" (AltStore#1840, SideStore#1686, xtool#378). Only anisette data
provisioned as a **Mac**, sent with its own Mac client info, gets through. xtool, iLoader and impactor
fixed it the same way.

What that means for AltServer-Linux:

- `dadoum/anisette-v3-server` provisions an Android device. It cannot produce Mac anisette, so a
  local anisette-v3 container no longer works for sign-in.
- Point `ALTSERVER_ANISETTE_SERVER` at a server that provides Mac anisette, and keep the default
  client info, which passes the server's own value through. AltServer-Linux uses the plain
  `GET /` protocol, which `https://mac-ani.jkcoxson.com` answers with Mac anisette data and
  `<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>`.
- That server is run by a third party. It never receives your Apple ID or password, which go only
  to Apple. Its plain `GET /` endpoint gives every client the same shared Mac identity, though, and
  Apple can treat sign-ins from a shared identity with more suspicion.

Verified on 2026-10-27: with this anisette, AltStore Classic signed in to a new free Apple ID
through AltServer-Linux and installed an app on iOS 27.

**Since 2026-11-05 Apple answers `mac-ani.jkcoxson.com`'s identity with `-80043`**, even for a made-up
account. `https://new.ani.sidestore.io` provides Mac anisette with the same client info and is now the
default anisette server, spoken to with the v3 protocol (Fix 8).

**Protect your Apple account.** Repeated failed sign-ins make Apple lock the account. While these
errors were being worked out, the maintainer's account was locked twice in four days. Stop after one
or two failures and find the cause first. After a lock or a password reset Apple can answer
`-36607` "Unable to sign you in to your Apple Account. Try again later" for days; do not retry
during that period. If `iforgot.apple.com` says the account cannot be unlocked online, unlock it in
Settings on an Apple device that is signed in with it. Use a separate free Apple ID for sideloading,
so a lock never hits your main account.

## Fix 7 — SMS codes for accounts without a trusted device

An Apple ID with no trusted Apple device gets its verification code by SMS. GrandSlam then answers
the password step with `au` = `secondaryAuth`. AltSign recognised only `trustedDeviceSecondaryAuth`, so
it treated such an account as having no two-factor authentication and the sign-in failed later.

`AltSign/AppleAPI+Authentication.cpp` now requests the code by SMS and validates it with the JSON
endpoints that rileytestut/AltSign#62 proposes and [isideload](https://github.com/nab138/isideload) uses:
`PUT /auth/verify/phone` with `{"phoneNumber":{"id":1},"mode":"sms"}`, then
`POST /auth/verify/phone/securitycode` with the code. The code goes to the account's first trusted
phone number (id 1). AltServer prints `Enter two factor code` and reads the code from standard input.
If Apple says a code was already sent and is still valid (HTTP 412, or `-22979` / `-22981`), no new
SMS is requested. A wrong code is `-21669`; any other error, also inside a 2xx answer, shows Apple's
message.

GrandSlam does not always report such an account as `secondaryAuth`. On 2026-11-05 an account without
a trusted device got `trustedDeviceSecondaryAuth`: the trusted-device code request made Apple send an
SMS, the trusted-device validation answered that code with HTTP 200, and the second sign-in still
required two-factor authentication.

So the method is chosen from the account's 2FA info. `ALTSERVER_2FA` selects it:

- `auto` (default): when a code is needed, AltServer reads the 2FA info (`GET /auth`, which sends
  nothing). With `noTrustedDevices` it takes the SMS path for the `trustedPhoneNumber`. Apple sends that
  SMS on its own, so AltServer requests no second one. `au` = `secondaryAuth` also takes the SMS path,
  requesting the code with `PUT`. Any other account keeps the trusted-device path. `pushMode` doesn't
  decide, because it is `sms` for the phone numbers of trusted-device accounts too.
- `sms`: always the SMS path, requesting the code with `PUT`.
- `trusteddevice`: the trusted-device path, as before Fix 7, except for `au` = `secondaryAuth`, which
  has no trusted device to send a code to.

At the SMS code prompt, `resend` requests one new SMS, for when the expected one doesn't arrive. The
log shows `au` and `hsc` of the sign-in response, the 2FA info (without the phone number), the chosen
method and which SMS code to enter.

Apple ties the code to the anisette identity it was validated with. `new.ani.sidestore.io` returns a new
identity (Device-Id, MD-LU, MD-M) on every request, so the anisette refreshes added in Fix 4 made the
second sign-in come from another device than the one that validated the code. A refresh is now used only
when it returns the same device; otherwise the sign-in keeps its original anisette data. The log names
the Device-Id (first 8 characters) at each step. With such a server every sign-in is still a new device
to Apple, so it asks for a code every time.

Verified on 2026-11-05: an account without trusted devices signed in with `ALTSERVER_2FA=sms` and
v1 anisette from `new.ani.sidestore.io`, and AltServer installed an app. The `auto` choice has not been
tried against Apple yet. If a sign-in fails, stop and read the log before trying again; see the
account-lock warning above.

## Fix 8 — anisette v3: one identity, a code only once

A v1 anisette server (`GET /`) hands out whatever identity it likes. `new.ani.sidestore.io` hands out a
new one on every request, so Apple sees a new device at every sign-in and asks for a code each time.
An anisette v3 server provisions one identity instead, and the identity's keychain identifier and
`adi.pb` stay on this computer. After the first sign-in with a code, Apple knows the device.

`src/AnisetteV3.cpp` speaks SideStore's anisette-v3-server protocol, as omnisette and Sideloader do:
`GET /v3/client_info`, provisioning over the `/v3/provisioning_session` websocket with Apple's
`midStartProvisioning` and `midFinishProvisioning` (relayed by this computer as the device), and
`POST /v3/get_headers` for each set of anisette data. The identity is kept in
`$XDG_CONFIG_HOME/AltServer/anisette-v3/<host>.json` (default `~/.config/...`), readable by its owner
only. A port or path in the server URL is added to the file name. Delete that file to start over with a
new identity. The v3 client's requests (anisette server and provisioning) honour `HTTPS_PROXY`,
`HTTP_PROXY` and `NO_PROXY`; the v1 client and the Apple sign-in itself do not use a proxy.

`ALTSERVER_ANISETTE_PROTOCOL` selects the protocol: `auto` (default) uses v3 when the server answers
`/v3/client_info` and v1 otherwise; `v3` and `v1` force one. A server that can't be reached is an
error, not a reason to fall back to v1. The log shows the protocol, whether the identity was
provisioned or loaded, and the first 8 characters of its device ID.

AltStore receives the same identity when it asks AltServer for anisette data.

Verified on 2026-11-05 against `new.ani.sidestore.io` with a made-up account: the first run provisioned
an identity and saved it (mode 600), the second run reused it without provisioning, and both times
Apple answered the password step with `-20970`, so the v3 anisette passed every check before it.

## Log output

The log, with or without `-d`, shows the steps, status codes, `au`/`hsc` and the first 8 characters
of the anisette device ID. It does not show passwords, verification codes, GSA tokens, the decrypted
sign-in response or anisette secrets (one-time password, machine ID, local user ID, `adi.pb`). `-d`
also turns on libimobiledevice's own debug output for the device connection.

## Prebuilt binaries

Static binaries for x86_64, aarch64, armv7 and i586 are attached to the
[Releases](https://github.com/jaakkopalvaila/AltServer-Linux/releases) page. Every push to `ng` also
produces them as GitHub Actions artifacts, which expire after 90 days.

## Building

Builds run in the prebuilt upstream Alpine image. On an Apple Silicon Mac, Rosetta runs the amd64
container at native speed and a clean build takes about 30 seconds.

```bash
git clone --recursive -b ng https://github.com/jaakkopalvaila/AltServer-Linux
cd AltServer-Linux
mkdir -p build
docker run --rm --platform linux/amd64 -v "$PWD:/workdir" -w /workdir \
  ghcr.io/nyamisty/altserver_builder_alpine_amd64:latest \
  bash -c 'cd build && make -f ../Makefile -j8'
```

The result is a statically linked `build/AltServer-x86_64`. Other architectures use the
`altserver_builder_alpine_{aarch64,armv7,i386}` images.

Sanity-check that the fixes are present:

```bash
strings build/AltServer-x86_64 | grep -c "missing path separator"   # Fix 1, new ldid      -> 1
strings build/AltServer-x86_64 | grep -c "Sanitized client info"    # Fix 2, sign-in        -> 1
grep -c idevice_sockaddr_len libraries/libimobiledevice/src/idevice.c  # Fix 3, netmuxd  -> >= 1
strings build/AltServer-x86_64 | grep -c "noTrustedDevices"          # Fix 7, 2FA choice     -> >= 1
strings build/AltServer-x86_64 | grep -c "/v3/provisioning_session"  # Fix 8, anisette v3    -> 1
```

## Running

```bash
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 \
./AltServer-x86_64 -d -u <UDID> -a <apple-id> -p <password> AltStore.ipa
```

`-d` enables debug output. Wi-Fi installs need netmuxd listening on that address (see Fix 3). The
default anisette server is `https://new.ani.sidestore.io` with the v3 protocol (Fix 8);
`ALTSERVER_ANISETTE_SERVER` selects another one, which must provide Mac anisette (Fix 5).

To check sign-in without real credentials, run it with a throwaway account. HTTP 503 means Apple's
edge is blocking, `-80045` means the client info does not match the anisette provisioning (Fix 4),
and an account error such as `-20970` "account information entered incorrectly" means the request
passed every check up to the password. A made-up account cannot reach the step where `-80046`
occurs, so this test cannot prove that a setup is free of it.
