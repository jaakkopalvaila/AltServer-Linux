# Deploying AltServer-Linux-NG

From a fresh Ubuntu host to apps that refresh over Wi-Fi without a cable. The example files are in
[`deploy/`](../deploy). This guide uses no Docker.

```
iPhone ──Wi-Fi (mDNS _apple-mobdev2)──> netmuxd (root, 127.0.0.1:27015) <── AltServer (your user)
   │                                                                         │      │
   └── AltStore ──Wi-Fi (mDNS _altserver._tcp, TCP)──────────────────────────┘      ├── anisette v3 server
                                                                                    └── Apple
```

- **netmuxd** finds the phone on the network and offers it to AltServer as if it were plugged in.
- **AltServer** signs and installs apps, and answers AltStore when it refreshes.
- **The anisette v3 server** provisions one Mac identity for this host. The identity is kept in a
  file, so Apple sees the same device at every sign-in (README-NG, Fix 8).

**Use a separate Apple ID for sideloading.** Repeated failed sign-ins make Apple lock the account, and
unlocking can take days (see Troubleshooting). A lock should never hit your main account.

## 1. Prerequisites

- Ubuntu 24.04 or newer (tested on 26.04), x86_64 or arm64, on the same network as the phone. Wired
  Ethernet or Wi-Fi both work; mDNS must reach the phone.
- An iPhone with a passcode, unlocked when you pair it, and a USB cable for the one-time pairing.
- Packages:

  ```bash
  sudo apt install avahi-daemon avahi-utils libavahi-compat-libdnssd1 usbmuxd libimobiledevice-utils python3
  ```

  - `avahi-daemon` publishes AltServer and finds the phone.
  - `libavahi-compat-libdnssd1` is the Bonjour library AltServer loads at run time.
  - `usbmuxd` and `libimobiledevice-utils` are for the USB pairing and for checks.
  - `python3` runs the monitor.
- If `ufw` is active, allow the local network in: mDNS (UDP 5353) and AltServer's TCP port, which it
  picks at random at each start. For example: `sudo ufw allow from 192.168.1.0/24`.

## 2. Binaries

AltServer, from the [Releases](https://github.com/jaakkopalvaila/AltServer-Linux/releases) page
(`AltServer-x86_64` or `AltServer-aarch64`):

```bash
mkdir -p ~/AltServer && cd ~/AltServer
curl -LO https://github.com/jaakkopalvaila/AltServer-Linux/releases/download/ng-2026-11-05/AltServer-x86_64
chmod +x AltServer-x86_64
```

netmuxd, from [jkcoxson/netmuxd](https://github.com/jkcoxson/netmuxd/releases) (the build for your CPU),
installed as `/usr/local/bin/netmuxd`:

```bash
sudo install -m 755 netmuxd-<your-architecture> /usr/local/bin/netmuxd
```

Get the example files: `git clone -b ng https://github.com/jaakkopalvaila/AltServer-Linux` or download
the `deploy/` directory.

## 3. Journal limits

A pre-NG build once busy-looped and wrote 68 GB of logs before the disk filled. Cap the journal before
anything else:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/journald-altserver.conf /etc/systemd/journald.conf.d/altserver.conf
sudo systemctl restart systemd-journald
```

The file caps the journal at 1 GB and rate-limits floods. `altserver.service` also rate-limits its own
output (`LogRateLimitIntervalSec`, `LogRateLimitBurst`). If rsyslog also writes `/var/log/syslog`, give
it a size cap in `/etc/logrotate.d/rsyslog` (`maxsize 500M`).

## 4. netmuxd as a system service

```bash
sudo cp deploy/netmuxd.service /etc/systemd/system/
sudo systemctl enable --now netmuxd
```

The flags are explained in [`deploy/netmuxd.service`](../deploy/netmuxd.service). The short version:

- **`--disable-unix`**: netmuxd's default socket is `/var/run/usbmuxd`, which Ubuntu's usbmuxd (USB,
  started by udev when a cable is plugged in) also uses. With both on one path, each breaks the other.
  So netmuxd listens only on TCP `127.0.0.1:27015`, and usbmuxd keeps the socket for USB pairing.
- **`--plist-storage /var/lib/lockdown`**: netmuxd uses the pairing records usbmuxd writes there.

## 5. Pair the phone once over USB, then use Wi-Fi

1. Connect the phone with the cable, unlock it, and pair through Ubuntu's usbmuxd:

   ```bash
   idevicepair pair          # tap Trust on the phone and enter the passcode, then run it again if asked
   idevicepair validate      # SUCCESS: Validated pairing with device <UDID>
   idevice_id -l             # prints the UDID; you need it in step 6
   ```

   The pairing record is saved in `/var/lib/lockdown/<UDID>.plist`.

2. The phone must offer itself over Wi-Fi. Check it:

   ```bash
   avahi-browse -rt _apple-mobdev2._tcp      # a record from the phone
   ```

   If nothing appears, turn on Wi-Fi connections once while the cable is connected. On a Mac, use
   Finder: "Show this iPhone when on Wi-Fi". On Linux, use
   `pymobiledevice3 lockdown wifi-connections --state on`.

3. Unplug the cable and restart netmuxd so it reads the new record. Then check that AltServer will
   find the phone through netmuxd:

   ```bash
   sudo systemctl restart netmuxd
   USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 idevice_id -n              # the UDID
   USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 idevicepair -n validate    # SUCCESS
   ```

## 6. First sign-in from the command line

Sign in once from the command line, with an app to install (for example AltStore Classic's `.ipa` from
altstore.io). This provisions the anisette identity and asks for the two-factor code once. Reading the
password with `read` keeps it out of the shell history and the process list:

```bash
cd ~/AltServer
read -rp "Apple ID: " AID; read -rsp "Password: " APW; echo
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 ./AltServer-x86_64 -u <UDID> -a "$AID" -p "$APW" AltStore.ipa
unset APW
```

- Two-factor code: AltServer chooses between the trusted-device prompt and SMS (README-NG, Fix 7). At
  `Enter two factor code`, type the code. For an SMS that doesn't arrive, type `resend` once.
- Success ends with `Installation Succeeded`. The log shows no password, code or token (README-NG,
  "Log output").

**The identity file** is `~/.config/AltServer/anisette-v3/new.ani.sidestore.io.json` (mode 600):

- **Back it up** somewhere private, for example in an encrypted backup. It works like a password for
  this "device".
- **Deleting or losing it** means a new device to Apple: the next sign-in asks for a two-factor code
  again.
- Don't share it, and don't put it in a repository.

## 7. AltServer as a user service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/altserver.service ~/.config/systemd/user/      # edit ExecStart for AltServer-aarch64
sudo loginctl enable-linger "$USER"                      # run without a login session, start at boot
systemctl --user daemon-reload
systemctl --user enable --now altserver
journalctl --user -u altserver -f                        # Ctrl-C to stop following
avahi-browse -rt _altserver._tcp                         # this host publishes AltServer
```

- **Linger**: a user service normally stops when you log out. Linger keeps it running and starts it at
  boot.
- **Environment** (`deploy/altserver.service`):
  - `USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015` is netmuxd.
  - `ALTSERVER_ANISETTE_SERVER` is the v3 anisette server; new.ani is also the default.
  - Leave `ALTSERVER_CLIENT_INFO` unset: Apple requires the anisette server's own client info (Fix 4).
- **Start order**: the service waits only for netmuxd to accept connections. The anisette server is
  remote and is contacted when needed.
- **Log rate limit**: at most 500 lines per 30 s from this service.

## 8. AltStore

With the phone on the same Wi-Fi, AltStore finds this AltServer by mDNS. Sign in to AltStore with the
same Apple ID. AltStore borrows anisette data from AltServer, so it gets the same identity, and Apple
doesn't ask for a code again. Then refresh with **My Apps → Refresh All**. Free Apple ID apps last 7
days. AltStore refreshes in the background when it can reach this server, and the monitor (step 9)
warns 2 days before a profile expires.

AltStore's own SMS two-factor flow has a bug (altstoreio/AltStore#1844): it can fail with no code
prompt. Signing in from the command line first (step 6) avoids it, because Apple then already trusts
the identity AltStore borrows.

## 9. The monitor

[`deploy/altserver_monitor.py`](../deploy/altserver_monitor.py) checks the host every 30 minutes. It
uses only the Python standard library and is adapted from
[Ben-Diehlci/altserver-linux](https://github.com/Ben-Diehlci/altserver-linux)'s `web/status_checks.py`
(AGPL-3.0). It checks:

| Check | What it tells you |
| --- | --- |
| AltServer process | The daemon is running. |
| AltServer advertisement | `_altserver._tcp` is published by this host, browsed from outside with avahi. |
| netmuxd | netmuxd accepts connections. |
| iPhone advertising | The phone offers `_apple-mobdev2._tcp`, so netmuxd can find it. |
| iPhone reachability | The phone is reachable through netmuxd (Wi-Fi) with a valid pairing, or only over USB. |
| Provisioning profiles | Days until the sideloaded apps expire: warning under 2 days, critical when expired. |
| Anisette server | The v3 server answers `/v3/client_info` with Mac client info. Only that static endpoint is used: no provisioning, nothing sent to Apple. |
| Anisette identity | The identity file exists with mode 600. |
| Clock | NTP is synchronised and the clock agrees with the anisette server's. |

- **Exit code** (Nagios): 0 OK, 1 warning, 2 critical, 3 unknown. A phone that's away from home counts
  as a warning, not critical.
- **History**: every run is appended to `~/.local/state/AltServer/monitor-history.jsonl` (JSON lines,
  mode 600, the last 2000 runs).
- **Notifications**: when a check's state changes (including the profile warning starting), the monitor
  posts to an [ntfy](https://ntfy.sh) topic. The phone checks (advertising, reachability, profiles)
  notify only once their new state has held for 4 runs (1.5 hours), so a sleeping phone or an evening
  away isn't news. Messages never contain the phone's UDID. The topic URL is a secret, so it lives in a
  file only you can read, never in the repository:

  ```bash
  install -D -m 600 /dev/null ~/.config/AltServer/ntfy-url
  echo "https://ntfy.sh/<your-secret-topic>" > ~/.config/AltServer/ntfy-url
  ```

With more than one iPhone on the network, set `ALTSERVER_MONITOR_UDID` in
`altserver-monitor.service` to the phone to check; otherwise the first one netmuxd lists is used.

Install and test:

```bash
cp deploy/altserver_monitor.py ~/AltServer/
cp deploy/altserver-monitor.service deploy/altserver-monitor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
python3 ~/AltServer/altserver_monitor.py --no-history --no-notify    # one run, nothing recorded
systemctl --user enable --now altserver-monitor.timer
systemctl --user list-timers altserver-monitor.timer
```

Unit tests, from the repository root: `python3 -m unittest discover -s deploy/tests`.

## Upgrading an older setup

If you followed an older guide:
- **The address proxy for netmuxd** (`muxaddr-proxy`, port 27016): not needed. The NG build reads
  netmuxd's addresses itself (README-NG, Fix 3). Disable it.
- **A local anisette container on port 6969** (`dadoum/anisette-v3-server`): its Android identity is
  refused since 2026-10-21 (-80046). Remove it, along with any `ExecStartPre` wait for it.
- **An older health script**: replace it with the monitor (step 9).

## Troubleshooting

| Symptom | Meaning and fix |
| --- | --- |
| `-80043` | Apple refuses the anisette server's identity (mac-ani.jkcoxson.com since 2026-11-05). Use another server, for example the default new.ani.sidestore.io. |
| `-80045` | The client info doesn't match the anisette provisioning. Leave `ALTSERVER_CLIENT_INFO` unset (README-NG, Fix 4). |
| `-80046` after the code | The anisette data isn't from a Mac. Use a Mac anisette server (Fix 5); the monitor's Anisette server check shows the client info. |
| `-36607` "Try again later" | Often after an account lock or password reset, and it can last days. **Don't retry during that period.** Also check the clock (monitor). |
| `-20209` / account locked | Apple locked the account after failed sign-ins. Unlock it at iforgot.apple.com, or in Settings on an Apple device signed in with it. |
| "still requires two-factor authentication" | The code went the wrong way. Set `ALTSERVER_2FA=sms` (or `trusteddevice`) for the command-line sign-in (Fix 7). |
| Phone not found | Run the monitor. The netmuxd, iPhone advertising and iPhone reachability checks show which link is broken. |
| Apps expired | Refresh in AltStore or from the command line (step 6). The monitor warns 2 days ahead. |

**Protect the account.** Stop after one or two failed sign-ins and find the cause before trying again.
Use a separate Apple ID for sideloading.
