"""Unit tests for deploy/altserver_monitor.py with fake command output. Run from the repository root:

    python3 -m unittest discover -s deploy/tests
"""

import datetime
import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import altserver_monitor as monitor  # noqa: E402

UDID = "00008110-001A2B3C4D5E6F70"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.timezone.utc)


class FakeRun:
    """A stand-in for monitor.run: answers by the command's first two words, records every call."""

    def __init__(self, answers, profiles=None):
        self.answers = answers
        self.profiles = profiles or {}
        self.calls = []

    def __call__(self, cmd, timeout=15, env=None, unset=()):
        self.calls.append((cmd, env or {}, tuple(unset)))
        if cmd[0] == "ideviceprovision" and "copy" in cmd and (cmd[0], cmd[1]) in self.answers:
            for name, data in self.profiles.items():
                with open(os.path.join(cmd[-1], name), "wb") as fh:
                    fh.write(data)
        return self.answers.get((cmd[0], cmd[1]), (None, "%s is not installed" % cmd[0]))


def avahi_row(service, host, iface="enp3s0", name="AltServer"):
    return "=;%s;IPv4;%s;%s;local;%s.local;192.168.1.10;54321;" % (iface, name, service, host)


def mobileprovision(name, expiration):
    """A .mobileprovision lookalike: binary noise around the XML plist, as in the CMS-signed file."""
    plist = plistlib.dumps({"AppIDName": name, "ExpirationDate": expiration.replace(tzinfo=None),
                            "ProvisionedDevices": [UDID]})
    return b"\x30\x82\x10\x00junk" + plist + b"\x00\xa0trailer"


class ProcessTests(unittest.TestCase):
    def test_running_binary_is_found(self):
        fake = FakeRun({("pgrep", "-a"): (0, "1234 /home/u/AltServer/AltServer-x86_64\n")})
        self.assertEqual(monitor.check_altserver_process(fake)["state"], monitor.OK)

    def test_monitor_and_ipa_arguments_are_not_altserver(self):
        out = ("99 /usr/bin/python3 /home/u/AltServer/altserver_monitor.py\n"
               "100 vim /home/u/AltServer/AltServer.py\n"
               "101 /home/u/AltServer/AltServer.ipa\n")
        fake = FakeRun({("pgrep", "-a"): (0, out)})
        self.assertEqual(monitor.check_altserver_process(fake)["state"], monitor.FAIL)

    def test_one_off_install_is_not_the_daemon(self):
        out = "200 /home/u/AltServer/AltServer-x86_64 -u <udid> -a a@example.com -p x Apollo.ipa\n"
        fake = FakeRun({("pgrep", "-a"): (0, out)})
        self.assertEqual(monitor.check_altserver_process(fake)["state"], monitor.FAIL)
        fake = FakeRun({("pgrep", "-a"): (0, out + "201 /home/u/AltServer/AltServer-x86_64\n")})
        self.assertEqual(monitor.check_altserver_process(fake)["state"], monitor.OK)

    def test_no_process(self):
        fake = FakeRun({("pgrep", "-a"): (1, "")})
        self.assertEqual(monitor.check_altserver_process(fake)["state"], monitor.FAIL)

    def test_pgrep_missing_is_unknown(self):
        self.assertEqual(monitor.check_altserver_process(FakeRun({}))["state"], monitor.UNKNOWN)


class AdvertisementTests(unittest.TestCase):
    def test_published_by_this_host(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (0, avahi_row("_altserver._tcp", "koti") + "\n")})
        self.assertEqual(monitor.check_altserver_advertisement(fake, hostname="koti")["state"], monitor.OK)

    def test_published_by_another_host_is_critical(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (0, avahi_row("_altserver._tcp", "other") + "\n")})
        check = monitor.check_altserver_advertisement(fake, hostname="koti")
        self.assertEqual(check["state"], monitor.FAIL)
        self.assertIn("not by this host", check["summary"])

    def test_host_name_renamed_by_avahi_is_still_this_host(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (0, avahi_row("_altserver._tcp", "koti-2") + "\n")})
        self.assertEqual(monitor.check_altserver_advertisement(fake, hostname="koti")["state"], monitor.OK)
        fake = FakeRun({("avahi-browse", "-rpt"): (0, avahi_row("_altserver._tcp", "koti-tv") + "\n")})
        self.assertEqual(monitor.check_altserver_advertisement(fake, hostname="koti")["state"], monitor.FAIL)

    def test_not_published(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (0, "+;enp3s0;IPv4;x;_altserver._tcp;local\n")})
        self.assertEqual(monitor.check_altserver_advertisement(fake, hostname="koti")["state"], monitor.FAIL)

    def test_avahi_failure_is_unknown_not_critical(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (1, "Failed to create client object: Daemon not running")})
        self.assertEqual(monitor.check_altserver_advertisement(fake, hostname="koti")["state"], monitor.UNKNOWN)

    def test_phone_advertising(self):
        row = avahi_row("_apple-mobdev2._tcp", "iPhone", name="aa:bb:cc:dd:ee:ff@fe80::1")
        fake = FakeRun({("avahi-browse", "-rpt"): (0, row + "\n")})
        self.assertEqual(monitor.check_phone_advertisement(fake)["state"], monitor.OK)

    def test_phone_not_advertising(self):
        fake = FakeRun({("avahi-browse", "-rpt"): (0, "")})
        self.assertEqual(monitor.check_phone_advertisement(fake)["state"], monitor.FAIL)


class NetmuxdTests(unittest.TestCase):
    def test_listening(self):
        self.assertEqual(monitor.check_netmuxd("127.0.0.1:27015", connect=lambda address: None)["state"], monitor.OK)

    def test_refused(self):
        def refuse(address):
            raise ConnectionRefusedError("Connection refused")
        self.assertEqual(monitor.check_netmuxd("127.0.0.1:27015", connect=refuse)["state"], monitor.FAIL)


class DeviceTests(unittest.TestCase):
    def test_wifi_with_valid_pairing_and_no_udid_in_output(self):
        fake = FakeRun({
            ("idevice_id", "-n"): (0, "%s (Network)\n" % UDID),
            ("idevice_id", "-l"): (0, ""),
            ("idevicepair", "-n"): (0, "SUCCESS: Validated pairing with device %s\n" % UDID),
        })
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.OK)
        self.assertIn("netmuxd", check["summary"])
        self.assertNotIn(UDID, json.dumps(check))
        # The Wi-Fi probe goes to netmuxd; the USB probe to usbmuxd's default socket.
        wifi_call = next(call for call in fake.calls if call[0] == ["idevice_id", "-n"])
        usb_call = next(call for call in fake.calls if call[0] == ["idevice_id", "-l"])
        self.assertEqual(wifi_call[1]["USBMUXD_SOCKET_ADDRESS"], "127.0.0.1:27015")
        self.assertIn("USBMUXD_SOCKET_ADDRESS", usb_call[2])

    def test_listed_but_not_answering_is_transport_not_pairing(self):
        fake = FakeRun({
            ("idevice_id", "-n"): (0, "%s\n" % UDID),
            ("idevice_id", "-l"): (0, ""),
            ("idevicepair", "-n"): (255, "ERROR: Could not connect to lockdownd, error code -8\n"),
        })
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.FAIL)
        self.assertIn("didn't answer", check["summary"])

    def test_locked_phone_is_a_warning(self):
        fake = FakeRun({
            ("idevice_id", "-n"): (0, "%s\n" % UDID),
            ("idevice_id", "-l"): (0, ""),
            ("idevicepair", "-n"): (255, "ERROR: Could not validate with device %s because a passcode is set." % UDID),
        })
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.WARN)
        self.assertNotIn(UDID, check["detail"])

    def test_usb_only(self):
        fake = FakeRun({("idevice_id", "-n"): (0, ""), ("idevice_id", "-l"): (0, "%s\n" % UDID)})
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.WARN)
        self.assertIn("USB only", check["summary"])

    def test_validates_the_listed_device(self):
        fake = FakeRun({
            ("idevice_id", "-n"): (0, "%s (Network)\n" % UDID),
            ("idevice_id", "-l"): (0, ""),
            ("idevicepair", "-n"): (0, "SUCCESS"),
        })
        monitor.check_device(fake, "127.0.0.1:27015")
        pair_call = next(call for call in fake.calls if call[0][0] == "idevicepair")
        self.assertEqual(pair_call[0], ["idevicepair", "-n", "-u", UDID, "validate"])

    def test_idevice_id_error_is_not_a_device(self):
        fake = FakeRun({("idevice_id", "-n"): (1, "ERROR: Could not connect to usbmuxd: Connection timed out\n"),
                        ("idevice_id", "-l"): (0, "")})
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.UNKNOWN)
        self.assertFalse(any(call[0][0] == "idevicepair" for call in fake.calls))

    def test_monitor_udid_not_listed(self):
        fake = FakeRun({("idevice_id", "-n"): (0, "%s\n" % UDID), ("idevice_id", "-l"): (0, "")})
        os.environ["ALTSERVER_MONITOR_UDID"] = "00008110-0000000000000001"
        try:
            check = monitor.check_device(fake, "127.0.0.1:27015")
        finally:
            del os.environ["ALTSERVER_MONITOR_UDID"]
        self.assertEqual(check["state"], monitor.FAIL)

    def test_idevicepair_timeout_is_not_an_install_problem(self):
        fake = FakeRun({("idevice_id", "-n"): (0, "%s\n" % UDID), ("idevice_id", "-l"): (0, "")})
        fake.answers[("idevicepair", "-n")] = (None, "idevicepair timed out after 15s")
        check = monitor.check_device(fake, "127.0.0.1:27015")
        self.assertEqual(check["state"], monitor.UNKNOWN)
        self.assertNotIn("apt install", check["fix"])

    def test_dead_netmuxd_is_unknown(self):
        fake = FakeRun({("idevice_id", "-n"): (255, "ERROR: Unable to retrieve device list!\n"),
                        ("idevice_id", "-l"): (255, "ERROR: Unable to retrieve device list!\n")})
        self.assertEqual(monitor.check_device(fake, "127.0.0.1:27015")["state"], monitor.UNKNOWN)

    def test_no_device_anywhere(self):
        fake = FakeRun({("idevice_id", "-n"): (0, ""), ("idevice_id", "-l"): (0, "")})
        self.assertEqual(monitor.check_device(fake, "127.0.0.1:27015")["state"], monitor.FAIL)

    def test_tools_missing(self):
        self.assertEqual(monitor.check_device(FakeRun({}), "127.0.0.1:27015")["state"], monitor.UNKNOWN)


class ProfileTests(unittest.TestCase):
    def profiles_check(self, profiles, rc=0):
        fake = FakeRun({("idevice_id", "-n"): (0, "%s (Network)\n" % UDID),
                        ("ideviceprovision", "-n"): (rc, "")}, profiles)
        return monitor.check_profiles(fake, "127.0.0.1:27015", now=NOW), fake

    def provision_call(self, fake):
        return next(call for call in fake.calls if call[0][0] == "ideviceprovision")

    def test_parse_profile(self):
        name, expiration = monitor.parse_profile(mobileprovision("Apollo", NOW + datetime.timedelta(days=5)))
        self.assertEqual(name, "Apollo")
        self.assertEqual(expiration, NOW + datetime.timedelta(days=5))

    def test_not_a_profile(self):
        self.assertIsNone(monitor.parse_profile(b"\x00\x01 no plist here"))

    def test_all_fresh(self):
        check, fake = self.profiles_check({
            "a.mobileprovision": mobileprovision("AltStore", NOW + datetime.timedelta(days=6)),
            "b.mobileprovision": mobileprovision("Apollo", NOW + datetime.timedelta(days=4, hours=12)),
        })
        self.assertEqual(check["state"], monitor.OK)
        self.assertIn("4.5 days (Apollo)", check["summary"])
        self.assertNotIn(UDID, json.dumps(check))
        self.assertEqual(self.provision_call(fake)[0][:5], ["ideviceprovision", "-n", "-u", UDID, "copy"])

    def test_under_two_days_warns(self):
        check, _ = self.profiles_check({"a.mobileprovision": mobileprovision("Apollo", NOW + datetime.timedelta(hours=30))})
        self.assertEqual(check["state"], monitor.WARN)

    def test_expired_is_critical(self):
        check, _ = self.profiles_check({
            "a.mobileprovision": mobileprovision("Apollo", NOW - datetime.timedelta(hours=1)),
            "b.mobileprovision": mobileprovision("AltStore", NOW + datetime.timedelta(days=6)),
        })
        self.assertEqual(check["state"], monitor.FAIL)
        self.assertIn("Apollo", check["summary"])

    def test_unreachable_phone_is_unknown(self):
        check, _ = self.profiles_check({}, rc=255)
        self.assertEqual(check["state"], monitor.UNKNOWN)

    def test_phone_not_listed_is_unknown(self):
        fake = FakeRun({("idevice_id", "-n"): (0, ""), ("ideviceprovision", "-n"): (0, "")})
        self.assertEqual(monitor.check_profiles(fake, "127.0.0.1:27015", now=NOW)["state"], monitor.UNKNOWN)

    def test_temporary_directory_is_removed(self):
        _, fake = self.profiles_check({"a.mobileprovision": mobileprovision("Apollo", NOW + datetime.timedelta(days=6))})
        self.assertFalse(os.path.exists(self.provision_call(fake)[0][-1]))


class AnisetteTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.environ = {"HOME": self.home}

    def write_identity(self, mode, contents=b"{}"):
        path = monitor.identity_path("https://new.ani.sidestore.io", self.environ)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as fh:
            fh.write(contents)
        os.chmod(path, mode)
        return path

    def test_identity_path_matches_the_cpp_naming(self):
        base = os.path.join(self.home, ".config", "AltServer", "anisette-v3")
        self.assertEqual(monitor.identity_path("https://new.ani.sidestore.io", self.environ),
                         os.path.join(base, "new.ani.sidestore.io.json"))
        self.assertEqual(monitor.identity_path("http://localhost:6969", self.environ),
                         os.path.join(base, "localhost_6969.json"))
        # cpprest counts any port written in the URL, even the scheme's default.
        self.assertEqual(monitor.identity_path("https://new.ani.sidestore.io:443", self.environ),
                         os.path.join(base, "new.ani.sidestore.io_443.json"))
        self.assertEqual(monitor.identity_path("https://example.com/anisette/v3", self.environ),
                         os.path.join(base, "example.com_anisette_v3.json"))
        self.assertEqual(monitor.identity_path("https://example.com", {"XDG_CONFIG_HOME": "/cfg"}),
                         "/cfg/AltServer/anisette-v3/example.com.json")
        # AltServer trims trailing slashes before naming the file.
        self.assertEqual(monitor.identity_path("https://example.com/anisette/", self.environ),
                         os.path.join(base, "example.com_anisette.json"))

    def test_identity_mode_600(self):
        self.write_identity(0o600)
        check = monitor.check_anisette_identity("https://new.ani.sidestore.io", self.environ)
        self.assertEqual(check["state"], monitor.OK)

    def test_identity_readable_by_others(self):
        self.write_identity(0o644)
        check = monitor.check_anisette_identity("https://new.ani.sidestore.io", self.environ)
        self.assertEqual(check["state"], monitor.FAIL)

    def test_identity_missing(self):
        check = monitor.check_anisette_identity("https://new.ani.sidestore.io", self.environ)
        self.assertEqual(check["state"], monitor.WARN)

    def test_v1_has_no_identity(self):
        environ = dict(self.environ, ALTSERVER_ANISETTE_PROTOCOL="v1")
        self.assertEqual(monitor.check_anisette_identity("https://new.ani.sidestore.io", environ)["state"], monitor.WARN)

    def test_server_only_fetches_client_info(self):
        urls = []

        def fetch(url):
            urls.append(url)
            body = json.dumps({"client_info": "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
                               "user_agent": "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0"}).encode()
            return 200, {"Date": "Fri, 06 Nov 2026 12:00:05 GMT"}, body

        check, date = monitor.check_anisette_server("https://new.ani.sidestore.io/", fetch)
        self.assertEqual(check["state"], monitor.OK)
        self.assertEqual(urls, ["https://new.ani.sidestore.io/v3/client_info"])
        self.assertEqual(date, "Fri, 06 Nov 2026 12:00:05 GMT")

    def test_server_not_a_mac(self):
        def fetch(url):
            return 200, {}, json.dumps({"client_info": "<Pixel 7> <Android;14>", "user_agent": "x"}).encode()
        self.assertEqual(monitor.check_anisette_server("https://x", fetch)[0]["state"], monitor.WARN)

    def test_fetch_refuses_other_schemes(self):
        with self.assertRaises(ValueError):
            monitor.fetch("file:///etc/passwd")

    def test_server_without_v3(self):
        self.assertEqual(monitor.check_anisette_server("https://x", lambda url: (404, {}, b""))[0]["state"], monitor.FAIL)

    def test_server_unreachable(self):
        def fetch(url):
            raise OSError("Name or service not known")
        self.assertEqual(monitor.check_anisette_server("https://x", fetch)[0]["state"], monitor.FAIL)


class ClockTests(unittest.TestCase):
    def synced(self, value="yes"):
        return FakeRun({("timedatectl", "show"): (0, value + "\n")})

    def test_in_sync(self):
        check = monitor.check_clock("Fri, 06 Nov 2026 12:00:05 GMT", self.synced(), now=NOW)
        self.assertEqual(check["state"], monitor.OK)

    def test_far_from_server_clock(self):
        check = monitor.check_clock("Fri, 06 Nov 2026 12:02:00 GMT", self.synced(), now=NOW)
        self.assertEqual(check["state"], monitor.FAIL)

    def test_not_synchronised(self):
        self.assertEqual(monitor.check_clock(None, self.synced("no"), now=NOW)["state"], monitor.WARN)

    def test_nothing_to_go_on(self):
        self.assertEqual(monitor.check_clock(None, FakeRun({}), now=NOW)["state"], monitor.UNKNOWN)


class RunTests(unittest.TestCase):
    def check(self, name, state):
        return monitor.result(name, state, "summary")

    def test_overall_and_exit_codes(self):
        self.assertEqual(monitor.overall_state([self.check("A", monitor.OK)]), monitor.OK)
        self.assertEqual(monitor.overall_state([self.check("A", monitor.OK), self.check("B", monitor.UNKNOWN)]),
                         monitor.UNKNOWN)
        self.assertEqual(monitor.overall_state([self.check("A", monitor.WARN), self.check("B", monitor.FAIL)]),
                         monitor.FAIL)
        self.assertEqual(monitor.EXIT_CODES, {monitor.OK: 0, monitor.WARN: 1, monitor.FAIL: 2, monitor.UNKNOWN: 3})

    def test_phone_away_is_not_critical(self):
        checks = [self.check("iPhone reachability", monitor.FAIL), self.check("AltServer process", monitor.OK)]
        self.assertEqual(monitor.overall_state(checks), monitor.WARN)

    def test_history_round_trip_trim_and_mode(self):
        path = os.path.join(tempfile.mkdtemp(), "state", "history.jsonl")
        for i in range(5):
            monitor.append_history(path, {"time": str(i), "overall": monitor.OK,
                                          "checks": {"A": {"state": monitor.OK if i < 4 else monitor.WARN}}},
                                   max_lines=3)
        with open(path) as fh:
            self.assertEqual(len(fh.read().splitlines()), 3)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(monitor.recent_states(path, 2), [{"A": monitor.OK}, {"A": monitor.WARN}])
        self.assertEqual(os.listdir(os.path.dirname(path)), ["history.jsonl"])

    def test_changes_only(self):
        checks = [self.check("A", monitor.OK), self.check("B", monitor.WARN), self.check("C", monitor.FAIL)]
        changed = monitor.changes([{"A": monitor.OK, "B": monitor.WARN, "C": monitor.OK}], checks)
        self.assertEqual([(check["name"], before, after) for check, before, after in changed],
                         [("C", monitor.OK, monitor.FAIL)])
        # A first run reports only what isn't OK.
        self.assertEqual([check["name"] for check, _, _ in monitor.changes([], checks)], ["B", "C"])

    def test_phone_checks_notify_once_the_state_holds(self):
        name = "iPhone reachability"
        away = [self.check(name, monitor.FAIL)]
        home = {name: monitor.OK}
        gone = {name: monitor.FAIL}
        # One run, two runs, three runs away: quiet. Four in a row: one notification.
        self.assertEqual(monitor.changes([home, home, home], away), [])
        self.assertEqual(monitor.changes([home, home, gone], away), [])
        self.assertEqual(monitor.changes([home, gone, gone], away), [])
        changed = monitor.changes([home, gone, gone, gone], away)
        self.assertEqual([(before, after) for _, before, after in changed], [(monitor.OK, monitor.FAIL)])
        # The fifth run away is not news.
        self.assertEqual(monitor.changes([gone, gone, gone, gone], away), [])
        # A nap of one run never notifies, in either direction.
        back = [self.check(name, monitor.OK)]
        self.assertEqual(monitor.changes([home, home, gone], back), [])
        # A new install with the phone away: no earlier state is known, so no "OK -> CRITICAL".
        self.assertEqual(monitor.changes([gone, gone, gone], away), [])

    def test_undelivered_change_is_sent_again(self):
        checks = [self.check("A", monitor.FAIL)]
        self.assertEqual(monitor.changes([{"A": monitor.FAIL}], checks), [])
        changed = monitor.changes([{"A": monitor.FAIL}], checks, unsent={"A": monitor.OK})
        self.assertEqual([(before, after) for _, before, after in changed], [(monitor.OK, monitor.FAIL)])
        # Recovered before delivery: nothing left to say.
        self.assertEqual(monitor.changes([{"A": monitor.FAIL}], [self.check("A", monitor.OK)],
                                         unsent={"A": monitor.OK}), [])

    def test_failed_send_is_recorded_and_retried(self):
        directory = tempfile.mkdtemp()
        history = os.path.join(directory, "history.jsonl")
        ntfy = os.path.join(directory, "ntfy-url")
        with open(ntfy, "w") as fh:
            fh.write("https://ntfy.sh/t\n")
        os.chmod(ntfy, 0o600)
        checks = [self.check("AltServer process", monitor.FAIL)]
        sent = []

        def unreachable(*args):
            raise OSError("Network is unreachable")

        env = {"ALTSERVER_MONITOR_HISTORY": history, "ALTSERVER_MONITOR_NTFY_FILE": ntfy}
        with mock.patch.dict(os.environ, env), mock.patch.object(monitor, "run_checks", return_value=checks), \
                mock.patch("sys.stdout"):
            with mock.patch.object(monitor, "send_ntfy", side_effect=unreachable):
                monitor.main([])
            self.assertEqual(monitor.unsent_changes(history), {"AltServer process": monitor.OK})
            with mock.patch.object(monitor, "send_ntfy", side_effect=lambda *a: sent.append(a)):
                monitor.main([])
            self.assertEqual(len(sent), 1)
            self.assertEqual(monitor.unsent_changes(history), {})

    def test_notification_has_no_udid(self):
        check = monitor.result("iPhone reachability", monitor.FAIL, "Lost %s" % UDID)
        title, body, priority = monitor.notification([(check, monitor.OK, monitor.FAIL)], hostname="koti")
        self.assertNotIn(UDID, title + body)
        self.assertEqual(priority, "high")
        self.assertIn("OK -> CRITICAL", body)

    def test_ntfy_url_file_must_be_private(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "ntfy-url")
        self.assertIsNone(monitor.read_ntfy_url(path)[0])
        with open(path, "w") as fh:
            fh.write("https://ntfy.sh/secret-topic\n")
        os.chmod(path, 0o644)
        url, why_not = monitor.read_ntfy_url(path)
        self.assertIsNone(url)
        self.assertIn("chmod 600", why_not)
        os.chmod(path, 0o600)
        self.assertEqual(monitor.read_ntfy_url(path), ("https://ntfy.sh/secret-topic", ""))

    def test_send_ntfy(self):
        sent = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            sent.append(request)
            return Response()

        monitor.send_ntfy("https://ntfy.sh/t", "Title", "Body", "high", urlopen=urlopen)
        self.assertEqual(sent[0].get_method(), "POST")
        self.assertEqual(sent[0].data, b"Body")
        self.assertEqual(sent[0].get_header("Priority"), "high")


if __name__ == "__main__":
    unittest.main()
