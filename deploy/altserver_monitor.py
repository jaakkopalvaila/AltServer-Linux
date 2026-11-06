#!/usr/bin/env python3
"""Health monitor for an AltServer-Linux-NG host. Python 3 standard library only.

The process, mDNS and device checks, and the reasoning behind them, are adapted from
Ben-Diehlci/altserver-linux, web/status_checks.py (https://github.com/Ben-Diehlci/altserver-linux),
AGPL-3.0 like this repository. Differences from that file:
  * netmuxd is reached over TCP (USBMUXD_SOCKET_ADDRESS, 127.0.0.1:27015 in deploy/), not a UNIX socket,
    and is checked on its own, so a dead netmuxd isn't mistaken for an absent phone;
  * the anisette check is for anisette v3: the saved identity file and the server's static
    /v3/client_info. It never fetches the server root, never provisions and never contacts Apple;
  * the clock check trusts local NTP, because the v3 client stamps X-Apple-I-Client-Time locally;
  * a new check counts the days until the sideloaded apps' provisioning profiles expire;
  * results go to a JSON-lines history and, on a change of state, to an ntfy topic.

AltServer can't report its own failures: a lost mDNS advertisement or an expired profile shows up a
week later as an app that won't open. These checks make that visible. Run by altserver-monitor.timer
every 30 minutes (deploy/). The exit code follows the Nagios convention: 0 OK, 1 WARNING, 2 CRITICAL,
3 UNKNOWN. Nothing here prints or sends the phone's UDID.
"""

import argparse
import datetime
import email.utils
import json
import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

OK, WARN, FAIL, UNKNOWN = "OK", "WARNING", "CRITICAL", "UNKNOWN"

# Nagios plugin exit codes.
EXIT_CODES = {OK: 0, WARN: 1, FAIL: 2, UNKNOWN: 3}

# Worst first: a run's overall state is the worst of its checks.
SEVERITY = [FAIL, WARN, UNKNOWN, OK]

DEFAULT_MUX = "127.0.0.1:27015"
DEFAULT_ANISETTE = "https://new.ani.sidestore.io"

# A profile expiring sooner than this is a warning: the next refresh has to happen now.
EXPIRY_WARNING_DAYS = 2

# Checks whose failure means "the phone isn't here" rather than "the server is broken". Taking the
# phone out of the house must not make the whole run critical, so these count as warnings at most
# in the overall state (the check itself still says CRITICAL).
DEVICE_DEPENDENT = ("iPhone advertising", "iPhone reachability")

HISTORY_MAX_LINES = 2000  # about six weeks at one run every 30 minutes

# Old 40-hex UDIDs and the 8-16 form newer devices use.
UDID_PATTERN = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b|\b[0-9A-Fa-f]{40}\b")


def scrub(text):
    """The text with any UDID replaced, so it can go to the history file and to ntfy."""
    return UDID_PATTERN.sub("<UDID>", text or "")


def result(name, state, summary, detail="", fix=""):
    return {"name": name, "state": state, "summary": scrub(summary), "detail": scrub(detail), "fix": fix}


def run(cmd, timeout=15, env=None, unset=()):
    """Run a command and return (exit code, stdout+stderr), or (None, reason) if it couldn't run.

    env adds variables to this process's environment; unset removes some. Never raises.
    """
    if shutil.which(cmd[0]) is None:
        return None, "%s is not installed" % cmd[0]
    merged = dict(os.environ)
    merged.update(env or {})
    for name in unset:
        merged.pop(name, None)
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged)
        return process.returncode, (process.stdout or "") + (process.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "%s timed out after %ss" % (cmd[0], timeout)
    except OSError as exc:
        return None, "%s could not be run: %s" % (cmd[0], exc)


def fetch(url, timeout=10):
    """GET url and return (status, headers, body bytes). Raises OSError (URLError) if unreachable."""
    request = urllib.request.Request(url, headers={"User-Agent": "altserver-monitor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(65536)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), b""


# --- AltServer process -------------------------------------------------------------------------

# The binary's own name, as deployed: AltServer-x86_64, AltServer-aarch64.ng-2026-11, ... Matching
# argv[0] only keeps out this monitor, editors and anything with "AltServer" in an argument.
ALTSERVER_ARGV0 = re.compile(r"^\d+\s+(\S*/)?AltServer[^\s/]*(\s|$)")


def check_altserver_process(run=run):
    rc, out = run(["pgrep", "-a", "-f", "AltServer"])
    if rc is None:
        return result("AltServer process", UNKNOWN, "Could not run pgrep", out, "Install procps.")
    lines = [line for line in out.splitlines()
             if ALTSERVER_ARGV0.match(line) and not line.split()[1].endswith((".py", ".ipa"))]
    if lines:
        return result("AltServer process", OK, "Running", lines[0][:160])
    return result("AltServer process", FAIL, "Not running", "",
                  "systemctl --user status altserver; journalctl --user -u altserver -n 50")


# --- mDNS ----------------------------------------------------------------------------------------

def browse(service, run=run):
    """avahi-browse a service. Returns (state, rows, note), state "ok", "missing" or "failed".

    A browse that couldn't run is not a browse that found nothing; the two need different advice.
    """
    rc, out = run(["avahi-browse", "-rpt", service], timeout=15)
    if rc is None:
        return ("missing" if "not installed" in out else "failed"), [], out
    if rc != 0:
        return "failed", [], "avahi-browse exited %d: %s" % (rc, out.strip()[:200])
    rows = [line.split(";") for line in out.splitlines() if line.startswith("=")]
    return "ok", [row for row in rows if len(row) > 8], ""


def is_this_host(row, hostname=None):
    """Was this resolved avahi row published by this host? row[6] is the host name."""
    hostname = hostname or socket.gethostname()
    return row[6].split(".")[0].lower() == hostname.split(".")[0].lower()


def check_altserver_advertisement(run=run, hostname=None):
    """AltServer's _altserver._tcp, browsed from outside: AltServer can't see its own advert fail."""
    name = "AltServer advertisement"
    state, rows, note = browse("_altserver._tcp", run)
    if state == "missing":
        return result(name, UNKNOWN, "avahi-browse is not installed", note, "sudo apt install avahi-utils")
    if state == "failed":
        return result(name, UNKNOWN, "Could not ask avahi", note,
                      "This says nothing about the advertisement. Check that avahi-daemon is running.")
    ours = [row for row in rows if is_this_host(row, hostname)]
    if ours:
        return result(name, OK, "Published by this host", "on " + ", ".join(sorted({row[1] for row in ours})))
    if rows:
        return result(name, FAIL, "Published, but not by this host",
                      "seen from " + ", ".join(sorted({row[6] for row in rows})),
                      "Another AltServer on this network, or a stale record. AltStore may use that one.")
    return result(name, FAIL, "Not published", "avahi answered and nothing advertises _altserver._tcp",
                  "AltStore can't find this server. Restart altserver and check avahi-daemon and "
                  "libavahi-compat-libdnssd1.")


def check_phone_advertisement(run=run):
    """The phone's _apple-mobdev2._tcp: netmuxd finds the phone through it."""
    name = "iPhone advertising"
    state, rows, note = browse("_apple-mobdev2._tcp", run)
    if state == "missing":
        return result(name, UNKNOWN, "avahi-browse is not installed", note, "sudo apt install avahi-utils")
    if state == "failed":
        return result(name, UNKNOWN, "Could not ask avahi", note, "Check that avahi-daemon is running.")
    if not rows:
        return result(name, FAIL, "No iPhone advertises _apple-mobdev2._tcp", "",
                      "The phone must be awake, on this network, and paired with this host over USB once.")
    return result(name, OK, "Discoverable over mDNS",
                  "%d record(s) on %s" % (len(rows), ", ".join(sorted({row[1] for row in rows}))))


# --- netmuxd and the phone -----------------------------------------------------------------------

def mux_address():
    return os.environ.get("USBMUXD_SOCKET_ADDRESS") or DEFAULT_MUX


def check_netmuxd(address=None, connect=None):
    """Does netmuxd accept connections? A dead netmuxd otherwise looks exactly like an absent phone."""
    address = address or mux_address()
    connect = connect or _connect
    try:
        connect(address)
        return result("netmuxd", OK, "Listening on %s" % address)
    except OSError as exc:
        return result("netmuxd", FAIL, "Not answering on %s" % address, str(exc),
                      "sudo systemctl status netmuxd; it must run with --disable-unix -p 27015 (deploy/).")


def _connect(address):
    if address.startswith("UNIX:"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        target = address[5:]
    else:
        host, _, port = address.rpartition(":")
        sock = socket.socket(socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET, socket.SOCK_STREAM)
        target = (host.strip("[]"), int(port))
    sock.settimeout(3)
    try:
        sock.connect(target)
    finally:
        sock.close()


def devices_via(flag, run=run, env=None, unset=()):
    """UDIDs over one transport, or (None, note) if the mux couldn't be asked.

    -n lists network devices (netmuxd presents the phone only as a network device), -l USB devices.
    """
    rc, out = run(["idevice_id", flag], env=env, unset=unset)
    if rc is None:
        return None, out
    if "Unable to retrieve device list" in out:
        return None, "no mux answered"
    return [line.split()[0] for line in out.splitlines() if line.strip()], ""


# How idevicepair reports failures: pairing-stage failures are words without a number, connection-
# stage failures carry "error code N". Text first; "no device found" beats everything.
PAIR_PATTERNS = (
    ("passcode", ("because a passcode is set", "enter the passcode")),
    ("gone", ("no device found",)),
    ("unpaired", ("is not paired with this host",)),
    ("trust", ("accept the trust dialog",)),
    ("denied", ("denied the trust dialog",)),
    ("failed", ("pairing with device", "pairing failed")),
)

# lockdownd codes (libimobiledevice lockdown.h) that idevicepair prints for connection failures.
PAIR_CODES = {-8: "gone", -7: "gone", -19: "trust", -18: "denied", -2: "unpaired", -4: "unpaired", -5: "unpaired"}


def classify_pair_failure(out):
    low = (out or "").lower()
    for kind, needles in PAIR_PATTERNS:
        if any(needle in low for needle in needles):
            return kind
    match = re.search(r"error code\s+(-?\d+)", out or "")
    return PAIR_CODES.get(int(match.group(1)), "unknown") if match else "unknown"


PAIR_ADVICE = {
    "passcode": (WARN, "Found over Wi-Fi, but the phone is locked", "Unlock the phone."),
    "gone": (FAIL, "Listed by netmuxd, but the phone didn't answer",
             "A transport problem, not a pairing one: the phone is asleep, off this Wi-Fi or just left. "
             "Don't re-pair because of this."),
    "trust": (WARN, "Waiting for Trust on the phone", "Unlock the phone and tap Trust."),
    "denied": (FAIL, "The phone refused the pairing", "Re-pair over USB and tap Trust."),
    "unpaired": (FAIL, "Found over Wi-Fi, but the pairing record doesn't validate",
                 "Re-pair over USB (docs/DEPLOY.md, step 5) and restart netmuxd."),
    "failed": (FAIL, "Found over Wi-Fi, but the pairing record doesn't validate",
               "Re-pair over USB (docs/DEPLOY.md, step 5) and restart netmuxd."),
    "unknown": (FAIL, "Found over Wi-Fi, but idevicepair validate failed",
                "Unrecognised idevicepair output; it is quoted above."),
}


def check_device(run=run, address=None):
    """Is the phone reachable, and over which path: netmuxd (Wi-Fi, unattended refresh) or only USB?"""
    name = "iPhone reachability"
    mux = {"USBMUXD_SOCKET_ADDRESS": address or mux_address()}
    wireless, wireless_note = devices_via("-n", run, env=mux)
    # Ubuntu's usbmuxd, at its default socket. It exits when no cable is attached, which is normal.
    usb, usb_note = devices_via("-l", run, unset=("USBMUXD_SOCKET_ADDRESS",))

    if wireless:
        rc, out = run(["idevicepair", "-n", "validate"], env=mux)
        if rc == 0:
            return result(name, OK, "Reachable through netmuxd (Wi-Fi), pairing valid",
                          "also connected over USB" if usb else "")
        if rc is None:
            return result(name, UNKNOWN, "Listed by netmuxd; idevicepair couldn't run", out,
                          "sudo apt install libimobiledevice-utils")
        state, summary, fix = PAIR_ADVICE[classify_pair_failure(out)]
        return result(name, state, summary, out.strip()[:300], fix)

    if usb:
        return result(name, WARN, "Reachable over USB only: Wi-Fi refresh won't work",
                      "netmuxd: " + (wireless_note or "running, no device"),
                      "Check that the phone is on this Wi-Fi and advertising (iPhone advertising).")
    if wireless is None and "not installed" in wireless_note:
        return result(name, UNKNOWN, "idevice_id is not installed", wireless_note,
                      "sudo apt install libimobiledevice-utils")
    if wireless is None:
        return result(name, UNKNOWN, "Couldn't ask netmuxd for devices", wireless_note,
                      "This says nothing about the phone; see the netmuxd check.")
    return result(name, FAIL, "No iPhone through netmuxd or USB", "",
                  "The phone is asleep, away from this network, or was never paired with this host.")


# --- provisioning profiles -------------------------------------------------------------------

def parse_profile(data):
    """(name, expiration datetime in UTC) from a .mobileprovision, or None.

    The file is a CMS signature around an XML property list; the plist is stored in the clear.
    """
    start, end = data.find(b"<?xml"), data.find(b"</plist>")
    if start < 0 or end < 0:
        return None
    try:
        plist = plistlib.loads(data[start:end + len(b"</plist>")])
    except (plistlib.InvalidFileException, ValueError):
        return None
    expiration = plist.get("ExpirationDate")
    if not isinstance(expiration, datetime.datetime):
        return None
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=datetime.timezone.utc)
    return plist.get("AppIDName") or plist.get("Name") or "unnamed profile", expiration


def read_profiles(run=run, address=None):
    """[(name, expiration)] of the phone's provisioning profiles, or (None, note) if unreadable."""
    directory = tempfile.mkdtemp(prefix="altserver-monitor-")
    try:
        rc, out = run(["ideviceprovision", "-n", "copy", directory],
                      env={"USBMUXD_SOCKET_ADDRESS": address or mux_address()}, timeout=30)
        if rc is None:
            return None, out
        if rc != 0:
            return None, "ideviceprovision exited %d: %s" % (rc, out.strip()[:200])
        profiles = []
        for entry in sorted(os.listdir(directory)):
            with open(os.path.join(directory, entry), "rb") as fh:
                parsed = parse_profile(fh.read())
            if parsed:
                profiles.append(parsed)
        return profiles, ""
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def check_profiles(run=run, address=None, now=None):
    """Days until the sideloaded apps' provisioning profiles expire (7 days for a free Apple ID)."""
    name = "Provisioning profiles"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    profiles, note = read_profiles(run, address)
    if profiles is None:
        fix = "sudo apt install libimobiledevice-utils" if "not installed" in note else \
            "Needs the phone reachable through netmuxd; see iPhone reachability."
        return result(name, UNKNOWN, "Couldn't read the profiles", note, fix)
    if not profiles:
        return result(name, WARN, "No provisioning profiles on the phone", "",
                      "Nothing sideloaded is installed, or the profiles couldn't be read.")

    left = sorted((expiration - now).total_seconds() / 86400.0 for _, expiration in profiles)
    by_expiry = sorted(profiles, key=lambda profile: profile[1])
    detail = ", ".join("%s %.1f d" % (profile_name, (expiration - now).total_seconds() / 86400.0)
                       for profile_name, expiration in by_expiry)
    expired = [profile_name for profile_name, expiration in by_expiry if expiration <= now]
    if expired:
        return result(name, FAIL, "Expired: %s" % ", ".join(expired), detail,
                      "Refresh in AltStore (or from the command line) now; expired apps won't open.")
    soonest_name = by_expiry[0][0]
    if left[0] < EXPIRY_WARNING_DAYS:
        return result(name, WARN, "%s expires in %.1f days" % (soonest_name, left[0]), detail,
                      "Refresh in AltStore before it expires.")
    return result(name, OK, "%d profile(s), next expiry in %.1f days (%s)" % (len(profiles), left[0], soonest_name),
                  detail)


# --- anisette v3 and the clock -----------------------------------------------------------------

def anisette_server():
    return (os.environ.get("ALTSERVER_ANISETTE_SERVER") or DEFAULT_ANISETTE).rstrip("/")


def identity_path(server, environ=None):
    """Where AltServer keeps its v3 identity for server: the same name src/AnisetteV3.cpp builds."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(environ.get("HOME", ""), ".config")
    parts = urllib.parse.urlsplit(server.rstrip("/"))
    name = parts.hostname or ""
    default_port = {"https": 443, "http": 80}.get(parts.scheme)
    if parts.port is not None and parts.port != default_port:
        name += "_%d" % parts.port
    if parts.path not in ("", "/"):
        name += "".join(c if c.isalnum() or c in "-." else "_" for c in parts.path)
    return os.path.join(base, "AltServer", "anisette-v3", name + ".json")


def check_anisette_identity(server=None, environ=None):
    """The saved v3 identity: present and readable by its owner only. Losing it means 2FA again."""
    name = "Anisette identity"
    environ = os.environ if environ is None else environ
    server = server or anisette_server()
    if (environ.get("ALTSERVER_ANISETTE_PROTOCOL") or "auto") == "v1":
        return result(name, WARN, "ALTSERVER_ANISETTE_PROTOCOL=v1: no saved identity",
                      "", "With v1 Apple may see a new device at every sign-in and ask for a code each time.")
    path = identity_path(server, environ)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return result(name, WARN, "No identity file yet", path,
                      "AltServer creates it at the first sign-in. If it existed before, it was lost and "
                      "the next sign-in will need a two-factor code.")
    except OSError as exc:
        return result(name, UNKNOWN, "Can't read the identity file", "%s: %s" % (path, exc))
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return result(name, FAIL, "Identity file is readable by others (mode %o)" % mode, path,
                      "chmod 600 '%s'. It works like a password for this device." % path)
    if info.st_size == 0:
        return result(name, FAIL, "Identity file is empty", path, "Restore it from a backup.")
    return result(name, OK, "Saved, mode 600", path)


def check_anisette_server(server=None, fetch=fetch):
    """The v3 server's static /v3/client_info. Returns (result, the server's Date header or None).

    Only this endpoint: never the server root (a v1 request hands out a fresh identity), never
    provisioning, and nothing that reaches Apple.
    """
    name = "Anisette server"
    server = (server or anisette_server()).rstrip("/")
    url = server + "/v3/client_info"
    try:
        status, headers, body = fetch(url)
    except (OSError, ValueError) as exc:
        return result(name, FAIL, "Unreachable", "%s: %s" % (url, exc),
                      "Sign-in and AltStore's refresh need it. Check the network, or choose another "
                      "server with ALTSERVER_ANISETTE_SERVER."), None
    date = headers.get("Date") or headers.get("date")
    if status != 200:
        return result(name, FAIL, "%s answered HTTP %d" % (url, status)), date
    try:
        info = json.loads(body.decode("utf-8", "replace"))
        client_info = info["client_info"]
    except (ValueError, KeyError, TypeError):
        return result(name, FAIL, "/v3/client_info isn't valid v3 client info", body[:200].decode("utf-8", "replace"),
                      "The server doesn't speak anisette v3."), date
    if "Mac" not in client_info:
        return result(name, WARN, "The server's identity isn't a Mac", client_info,
                      "Apple accepts only Mac anisette since 2026-10-21 (-80046; README-NG, Fix 5)."), date
    return result(name, OK, "v3, Mac client info", client_info), date


def check_clock(server_date=None, run=run, now=None):
    """Is this host's clock right? The v3 client stamps X-Apple-I-Client-Time with it.

    Upstream compared against the v1 anisette server's timestamp, which v1 forwards to Apple. With v3
    the time comes from this host, so local NTP is what counts. The anisette server's HTTP Date header,
    when there is one, gives an independent reading to compare with.
    """
    name = "Clock"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    synchronized = None if rc is None else out.strip() == "yes"

    skew = None
    if server_date:
        try:
            skew = abs((email.utils.parsedate_to_datetime(server_date) - now).total_seconds())
        except (TypeError, ValueError):
            skew = None

    if skew is not None and skew > 30:
        return result(name, FAIL, "%d s away from the anisette server's clock" % skew,
                      "NTP synchronised: %s" % {True: "yes", False: "no", None: "unknown"}[synchronized],
                      "Apple rejects sign-ins with a wrong client time (often as -36607). Fix NTP: timedatectl.")
    if synchronized is False:
        return result(name, WARN, "Not NTP-synchronised", "" if skew is None else "%d s from the anisette server" % skew,
                      "sudo timedatectl set-ntp true")
    if synchronized is None and skew is None:
        return result(name, UNKNOWN, "Couldn't check the clock", out)
    detail = "NTP synchronised" if synchronized else "timedatectl unavailable"
    if skew is not None:
        detail += ", %d s from the anisette server" % skew
    return result(name, OK, "In sync", detail)


# --- the run, history and notifications -------------------------------------------------------

def run_checks():
    anisette, server_date = check_anisette_server()
    return [
        check_altserver_process(),
        check_altserver_advertisement(),
        check_netmuxd(),
        check_phone_advertisement(),
        check_device(),
        check_profiles(),
        anisette,
        check_anisette_identity(),
        check_clock(server_date),
    ]


def overall_state(checks):
    """The worst state, with the phone-presence checks counting as warnings at most."""
    states = []
    for check in checks:
        state = check["state"]
        if check["name"] in DEVICE_DEPENDENT and state == FAIL:
            state = WARN
        states.append(state)
    return next((state for state in SEVERITY if state in states), OK)


def state_home(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_STATE_HOME") or os.path.join(environ.get("HOME", ""), ".local", "state")
    return os.path.join(base, "AltServer")


def history_path(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("ALTSERVER_MONITOR_HISTORY") or os.path.join(state_home(environ), "monitor-history.jsonl")


def last_states(path):
    """{check name: state} from the history's last run, or {} if there is none."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return {}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            return {name: check["state"] for name, check in entry["checks"].items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return {}


def append_history(path, entry, max_lines=HISTORY_MAX_LINES):
    """Append one run as a JSON line, keep the last max_lines, file mode 600."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = (lines + [json.dumps(entry, sort_keys=True)])[-max_lines:]
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(temporary, path)


def changes(previous, checks):
    """[(check, previous state, new state)] for checks whose state changed. A new check counts as OK before."""
    return [(check, previous.get(check["name"], OK), check["state"])
            for check in checks if previous.get(check["name"], OK) != check["state"]]


def ntfy_url_path(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(environ.get("HOME", ""), ".config")
    return environ.get("ALTSERVER_MONITOR_NTFY_FILE") or os.path.join(base, "AltServer", "ntfy-url")


def read_ntfy_url(path):
    """The topic URL, or (None, why not). The topic name is a secret: the file must be mode 600."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None, "not configured (%s doesn't exist)" % path
    if stat.S_IMODE(info.st_mode) & 0o077:
        return None, "not sent: %s is readable by others, chmod 600 it" % path
    with open(path, encoding="utf-8") as fh:
        url = fh.read().strip()
    if not url.startswith(("https://", "http://")):
        return None, "not sent: %s doesn't contain an http(s) URL" % path
    return url, ""


def notification(changed, hostname=None):
    """(title, body, priority) for the changed checks. Nothing in it names the phone."""
    hostname = hostname or socket.gethostname()
    lines = ["%s: %s -> %s. %s" % (check["name"], before, after, check["summary"]) for check, before, after in changed]
    worst = next((state for state in SEVERITY if state in [after for _, _, after in changed]), OK)
    priority = {FAIL: "high", WARN: "default", UNKNOWN: "default", OK: "low"}[worst]
    return "AltServer on %s: %s" % (hostname, worst), scrub("\n".join(lines)), priority


def send_ntfy(url, title, body, priority, urlopen=urllib.request.urlopen):
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                     headers={"Title": title, "Priority": priority, "Tags": "iphone"})
    with urlopen(request, timeout=15) as response:
        return response.status


def main(argv=None):
    parser = argparse.ArgumentParser(description="AltServer-Linux health check (Nagios exit codes).")
    parser.add_argument("--no-history", action="store_true", help="don't read or write the history file")
    parser.add_argument("--no-notify", action="store_true", help="don't send ntfy notifications")
    args = parser.parse_args(argv)

    checks = run_checks()
    overall = overall_state(checks)
    for check in checks:
        line = "%-24s %-8s %s" % (check["name"], check["state"], check["summary"])
        if check["state"] != OK and check["fix"]:
            line += "  [%s]" % check["fix"]
        print(line)
    print("Overall: %s" % overall)

    if not args.no_history:
        path = history_path()
        previous = last_states(path)
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        append_history(path, {"time": now, "overall": overall,
                              "checks": {check["name"]: {"state": check["state"], "summary": check["summary"]}
                                         for check in checks}})
        changed = changes(previous, checks)
        if changed and not args.no_notify:
            url, why_not = read_ntfy_url(ntfy_url_path())
            if url:
                try:
                    send_ntfy(url, *notification(changed))
                    print("Notified: %d change(s)" % len(changed))
                except (OSError, ValueError) as exc:
                    print("ntfy failed: %s" % exc)
            else:
                print("ntfy %s" % why_not)

    return EXIT_CODES[overall]


if __name__ == "__main__":
    sys.exit(main())
