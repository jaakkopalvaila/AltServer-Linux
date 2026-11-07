# AltServer-Linux-NG

**AltServer for Linux. Keep sideloaded iOS apps signed and refreshed from a Linux machine, with no Mac
or Windows PC.**

## What this is

Apps installed with [AltStore](https://altstore.io) are signed with your Apple ID. With a free Apple ID
the signature expires after **7 days**, and the app stops opening unless it is signed again. Normally
AltServer on a Mac or a Windows PC does this, and that computer must be on and on the same Wi-Fi as
the phone.

This fork runs AltServer on Linux instead: a home server, a NAS, a Raspberry Pi or a virtual machine.
It is a single static binary. After a one-time setup, AltStore on the phone refreshes its apps over
Wi-Fi through this host, and the host can also install apps from the command line.

This is a fork of [NyaMisty/AltServer-Linux](https://github.com/NyaMisty/AltServer-Linux), which has
not been maintained since 2023. Its last binary cannot sign apps for current iOS or sign in to Apple.
This fork fixes that; see [What this fork changes](#what-this-fork-changes).

## What you need

| | |
| --- | --- |
| A Linux host | Ubuntu 24.04 or newer is documented; x86_64 or arm64 (armv7 and i586 binaries also build). On the same network as the phone |
| An iPhone or iPad | With AltStore, or install AltStore from this host |
| A USB cable | **Once**, to pair the phone with the host. After that everything runs over Wi-Fi |
| An Apple ID | **Use a separate one for sideloading**, so an account lock never hits your main account |
| [netmuxd](https://github.com/jkcoxson/netmuxd) | Makes the phone on Wi-Fi look like a plugged-in device to AltServer |

A free Apple ID allows 3 sideloaded apps at a time and 10 new app IDs per week, and its signatures last
7 days.

**Tested on:** Ubuntu 26.04 (x86_64) with an iPhone on iOS 27 and AltStore Classic, November 2026.

## What it does not do

- **Pair without a cable.** The first pairing needs USB.
- **Refresh a phone it cannot reach.** The phone must be on the same network when a refresh is due.
- **JIT.** AltJIT is not supported.
- **Work alongside another signer.** Do not use the same Apple ID with AltServer on a Mac or PC,
  Sideloadly or Xcode at the same time: each one revokes the other's certificate.

## Get started

1. Download `AltServer-x86_64` or `AltServer-aarch64` from the
   [Releases](https://github.com/jaakkopalvaila/AltServer-Linux/releases) page.
2. Follow **[docs/DEPLOY.md](docs/DEPLOY.md)**. It takes a fresh Ubuntu host to apps that refresh over
   Wi-Fi: packages, netmuxd, the one-time USB pairing, the first sign-in from the command line,
   AltServer as a systemd user service, AltStore, and troubleshooting.
3. Optional: turn on the [health monitor](#health-monitor). It tells you when something stops working,
   before an app expires.

A one-off install from the command line, with netmuxd running:

```bash
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 ./AltServer-x86_64 -u <UDID> -a <apple-id> -p <password> app.ipa
```

With the same `USBMUXD_SOCKET_ADDRESS` and no other arguments, AltServer runs as the daemon that AltStore
talks to; docs/DEPLOY.md runs it as a systemd user service. Two-factor codes are asked on the terminal.

**Protect your Apple account.** Repeated failed sign-ins make Apple lock the account, and unlocking can
take days. After one or two failures, stop and read the log before you try again.

## What this fork changes

| | |
| --- | --- |
| iOS 26.4+ / 27 signing | **Fixed.** Signed apps launch again (ldid from AltServer for Windows 1.7.4) |
| Apple sign-in, 2026 blocks | **Fixed.** Mac anisette with the v3 protocol by default, so Apple sees one stable device |
| Two-factor codes | Trusted-device and **SMS** codes, chosen automatically from the account's settings |
| netmuxd >= 0.3 | **Fixed.** Connects to netmuxd directly; no address-translation proxy |
| Logs | No passwords, codes, tokens or anisette secrets in the output |
| Deployment | A step-by-step guide, systemd unit files and a health monitor |

The details, with symptoms, root causes and how each fix was verified, are in
**[README-NG.md](README-NG.md)**.

## Health monitor

[`deploy/altserver_monitor.py`](deploy/altserver_monitor.py) runs every 30 minutes from a systemd timer.
It checks the AltServer process and its mDNS advertisement, netmuxd, whether the phone is reachable,
how many days the sideloaded apps have left, the anisette server and identity file, and the clock. It
keeps a history and can send a notification to an [ntfy](https://ntfy.sh) topic when a check changes
state. See [docs/DEPLOY.md, step 9](docs/DEPLOY.md#9-the-monitor).

## Configuration

| Environment variable | Default | What it does |
| --- | --- | --- |
| `USBMUXD_SOCKET_ADDRESS` | the system usbmuxd socket | netmuxd's address, for example `127.0.0.1:27015` |
| `ALTSERVER_ANISETTE_SERVER` | `https://new.ani.sidestore.io` | The anisette server. It must provide Mac anisette |
| `ALTSERVER_ANISETTE_PROTOCOL` | `auto` | `v3`, `v1` or `auto` (v3 when the server supports it) |
| `ALTSERVER_2FA` | `auto` | `auto`, `sms` or `trusteddevice` |
| `ALTSERVER_CLIENT_INFO` | the anisette server's own | Override the client info sent to Apple |

The anisette v3 identity is kept in `~/.config/AltServer/anisette-v3/`, readable by its owner only.
Delete that file to start over with a new identity.

## Building

Prebuilt static binaries for x86_64, aarch64, armv7 and i586 are on the Releases page, and every push
to `ng` builds them in GitHub Actions. To build them yourself, see
[README-NG.md, Building](README-NG.md#building). Clone with submodules:

```bash
git clone --recursive -b ng https://github.com/jaakkopalvaila/AltServer-Linux
```

## Credits

- [NyaMisty/AltServer-Linux](https://github.com/NyaMisty/AltServer-Linux): the original Linux port.
- [Riley Testut / AltStore](https://github.com/rileytestut/AltServer-Windows): AltServer, AltSign and
  the ldid signing fix.
- [jkcoxson/netmuxd](https://github.com/jkcoxson/netmuxd): Wi-Fi device access.
- [Dadoum/anisette-v3-server](https://github.com/Dadoum/anisette-v3-server) and
  [SideStore](https://github.com/SideStore/anisette-servers): the anisette v3 protocol and the public
  server this fork uses by default.
- [Ben-Diehlci/altserver-linux](https://github.com/Ben-Diehlci/altserver-linux): the monitor's process,
  mDNS and device checks are adapted from its `web/status_checks.py`.

## License

[AGPL-3.0](LICENSE), as upstream.
