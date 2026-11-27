"""
Contract tests for unifi2ipam. Both APIs are stubbed at the `requests` layer with small stateful
fakes, so the real code path (xmit, lookups, sync) runs and every request is recorded.

Run: python -m unittest discover -s tests -v
"""
import itertools
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unifi2ipam as u  # noqa: E402

ZONE = "home.8none1.com"
NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
NOW_S = "2026-11-27 12:00:00"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.url, self.text = status, body, "stub", str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


class FakeWorld:
    """UniFi (integration + classic) and phpIPAM in one object. `log` holds (METHOD, host, path)."""

    def __init__(self):
        self.log = []
        # UniFi
        self.clients = []        # integration API clients (connected)
        self.users = []          # classic rest/user records
        self.unifi_fail = None   # None | "clients-empty" | "clients-500" | "users-401" | "users-empty" | "sites-500" | "net"
        # phpIPAM
        self.rows = {}           # id -> row
        self.subnets = [{"id": "9", "subnet": "172.16.0.0", "mask": "24"}, {"id": "1", "subnet": "172.16.0.0", "mask": "16"}]
        self.ipam_fail = {}      # endpoint-substring -> "net" | 500 | 401 | "garbage"
        self.write_fail = set()  # methods that return success:false
        self._ids = itertools.count(1000)

    def add_row(self, ip, mac=None, hostname=None, note=None, owner=None, description=None, last_seen="1970-01-01 00:00:01"):
        rid = str(next(self._ids))
        self.rows[rid] = {"id": rid, "ip": ip, "mac": mac, "hostname": hostname, "note": note, "owner": owner,
                          "description": description, "lastSeen": last_seen, "subnetId": "9"}
        return rid

    def writes(self):
        return [e for e in self.log if e[0] != "GET"]

    def ipam_writes(self):
        return [e for e in self.writes() if e[1] == "ipam"]

    # -- request dispatch -------------------------------------------------
    def request(self, method):
        def call(url, headers=None, verify=None, timeout=None, params=None, json=None):
            host = "ipam" if "/api/app/" in url else "unifi"
            path = url.split("/api/app/")[1] if host == "ipam" else url
            self.log.append((method.upper(), host, path))
            if host == "ipam":
                return self._ipam(method, path, json)
            return self._unifi(method, path, params)
        return call

    def _unifi(self, method, url, params):
        f = self.unifi_fail
        if f == "net":
            raise requests.exceptions.ConnectionError("down")
        if url.endswith("/integration/v1/sites"):
            if f == "sites-500":
                return Resp(500, {"message": "boom"})
            return Resp(200, {"data": [{"id": "site-1", "name": "Default"}]})
        if url.endswith("/clients"):
            if f == "clients-500":
                return Resp(500, {"message": "boom"})
            if f == "clients-empty":
                return Resp(200, {"data": [], "count": 0, "totalCount": 0})
            return Resp(200, {"data": self.clients, "count": len(self.clients), "totalCount": len(self.clients)})
        if url.endswith("/rest/user"):
            if f == "users-401":
                return Resp(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []})
            if f == "users-empty":
                return Resp(200, {"meta": {"rc": "ok"}, "data": []})
            return Resp(200, {"meta": {"rc": "ok"}, "data": self.users})
        raise AssertionError(f"unexpected UniFi URL {url}")

    def _ipam(self, method, path, body):
        for frag, mode in self.ipam_fail.items():
            if frag in path:
                if mode == "net":
                    raise requests.exceptions.ConnectionError("down")
                if mode == "garbage":
                    return Resp(200, ValueError("not json"))
                return Resp(mode, {"code": mode, "success": False, "message": "stub error"})
        ok = lambda data=None: Resp(200, {"code": 200, "success": True, "data": data})
        nf = lambda: Resp(404, {"code": 404, "success": False, "message": "No results found"})
        rows = list(self.rows.values())
        if method == "get":
            if path == "subnets/":
                return ok(self.subnets)
            if path.startswith("subnets/overlapping/"):
                return ok(self.subnets)
            if path.startswith("addresses/search_mac/"):
                mac = path.split("/")[2]
                hit = [r for r in rows if (r["mac"] or "").lower() == mac.lower()]
                return ok(hit) if hit else nf()
            if path.startswith("addresses/search/"):
                ip = path.split("/")[2]
                hit = [r for r in rows if r["ip"] == ip]
                return ok(hit) if hit else nf()
            raise AssertionError(f"unexpected phpIPAM GET {path}")
        if method.upper() in {m.lower() for m in self.write_fail} or method in self.write_fail:
            return Resp(500, {"code": 500, "success": False, "message": "write refused"})
        if method == "post":
            assert path == "addresses", path
            if any(r["ip"] == body["ip"] for r in rows):
                return Resp(409, {"code": 409, "success": False, "message": "IP address already exists"})
            rid = self.add_row(body["ip"], body.get("mac"), body.get("hostname"), body.get("note"),
                               body.get("owner"), body.get("description"), body.get("lastSeen"))
            return Resp(201, {"code": 201, "success": True, "id": rid})
        rid = path.split("/")[1]
        if rid not in self.rows:
            return nf()
        if method == "patch":
            self.rows[rid].update(body)
            return ok()
        if method == "delete":
            del self.rows[rid]
            return ok()
        raise AssertionError(method)


def client(ip, mac, name):
    return {"id": "c-" + mac, "ipAddress": ip, "macAddress": mac, "name": name, "type": "WIRELESS"}


def user(mac, name=None, dns=None, enabled=True):
    d = {"mac": mac, "local_dns_record_enabled": enabled}
    if name:
        d["name"] = name
    if dns:
        d["local_dns_record"] = dns
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.w = FakeWorld()
        env = {"UNIFI_API_KEY": "k-unifi", "IPAM_API_KEY": "k-ipam", "UNIFI_URL": "https://unifi.test/proxy/network/integration/v1/",
               "IPAM_BASE_URL": "https://ipam.test", "IPAM_APP_ID": "app"}
        self.stack = [
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(u, "utcnow", lambda: NOW),
            mock.patch.object(requests, "get", self.w.request("get")),
            mock.patch.object(requests, "post", self.w.request("post")),
            mock.patch.object(requests, "patch", self.w.request("patch")),
            mock.patch.object(requests, "delete", self.w.request("delete")),
        ]
        for m in self.stack:
            m.start()
            self.addCleanup(m.stop)

    def run_cli(self, *extra, dry=False, include="172.16.0.0/24"):
        argv = ["--config", os.devnull, "--site-id", "site-1", "--include-subnet", include,
                "--dns-zone", ZONE, "--workers", "1", "--quiet"] + list(extra) + (["--dryrun"] if dry else [])
        try:
            return u.cli(argv)
        except SystemExit as e:
            return e.code

    def row_by_ip(self, ip):
        return next((r for r in self.w.rows.values() if r["ip"] == ip), None)


class TestNames(unittest.TestCase):
    cfg = u.Config("", "", "", "", True, dns_zone=ZONE)

    def test_local_record_strips_zone_and_keeps_subdomain(self):
        self.assertEqual(u.dns_name_from_local_record("pet-cam.iot.home.8none1.com", ZONE), "pet-cam.iot")
        self.assertEqual(u.dns_name_from_local_record("Unity.Home.8none1.com.", ZONE), "unity")

    def test_local_record_rejects_foreign_zone_apex_deep_and_bad_labels(self):
        for rec in ("x.example.com", "home.8none1.com", "a.b.c.home.8none1.com", "bad_name.home.8none1.com",
                    "-x.home.8none1.com", "sp ace.home.8none1.com", ("a" * 64) + ".home.8none1.com"):
            self.assertIsNone(u.dns_name_from_local_record(rec, ZONE), rec)

    def test_no_zone_means_no_local_record_names(self):
        self.assertIsNone(u.dns_name_from_local_record("x.home.8none1.com", ""))

    def test_alias_only_if_already_a_single_valid_label(self):
        self.assertEqual(u.dns_name_from_alias("k3s-server-01"), "k3s-server-01")
        self.assertEqual(u.dns_name_from_alias("Bambu-P1S"), "bambu-p1s")
        for bad in ("Living Room TV", "SonosZP 16:0c", "Adam's iPhone", "Theo’s iPad", "d4:ad:fc:99:d5:b2", "Unknown", "", "a.b", "x_y"):
            self.assertIsNone(u.dns_name_from_alias(bad), bad)

    def test_randomised_mac_detection(self):
        for mac in ("da:a1:19:00:00:01", "02:00:00:00:00:00", "FE:00:00:00:00:00"):
            self.assertTrue(u.is_randomised_mac(mac), mac)
        for mac in ("bc:24:11:aa:bb:cc", "00:11:22:33:44:55", "d4:ad:fc:99:d5:b2"):
            self.assertFalse(u.is_randomised_mac(mac), mac)

    def test_curated_precedence_and_reasons(self):
        mac = "d4:ad:fc:99:d5:b2"
        self.assertEqual(u.curated_name(self.cfg, mac, user(mac, "Pet Cam", "pet-cam.iot.home.8none1.com")), ("pet-cam.iot", "local DNS record"))
        self.assertEqual(u.curated_name(self.cfg, mac, user(mac, "pve01-mgmt"))[0], "pve01-mgmt")
        # disabled record counts as "has none"
        self.assertEqual(u.curated_name(self.cfg, mac, user(mac, "k3s-agent-01", "x.home.8none1.com", enabled=False))[0], "k3s-agent-01")
        # a record that is present but unusable does not fall back to the alias
        self.assertIsNone(u.curated_name(self.cfg, mac, user(mac, "good-alias", "x.example.com"))[0])
        self.assertIsNone(u.curated_name(self.cfg, mac, user(mac, "Living Room TV"))[0])
        self.assertIsNone(u.curated_name(self.cfg, mac, None)[0])
        # randomised MAC wins over everything
        rnd = "da:a1:19:00:00:01"
        self.assertEqual(u.curated_name(self.cfg, rnd, user(rnd, "ok-name", "ok.home.8none1.com")), (None, "randomised MAC"))


class TestSyncRules(Base):
    def setUp(self):
        super().setUp()
        w = self.w
        w.clients = [
            client("172.16.0.10", "d4:ad:fc:00:00:10", "Pet Cam"),
            client("172.16.0.11", "d4:ad:fc:00:00:11", "SonosZP 16:0c"),
            client("172.16.0.12", "da:a1:19:00:00:12", "iPhone 0b:f1"),
            client("172.16.0.13", "d4:ad:fc:00:00:13", "k3s-agent-01"),
            client("10.9.9.9", "d4:ad:fc:00:00:99", "Out Of Scope"),
        ]
        w.users = [
            user("d4:ad:fc:00:00:10", "Pet Cam", "pet-cam.iot.home.8none1.com"),
            user("d4:ad:fc:00:00:11", "SonosZP 16:0c"),
            user("da:a1:19:00:00:12", "Adam's iPhone", "phone.home.8none1.com"),
            user("d4:ad:fc:00:00:13", "k3s-agent-01"),
        ]

    # 1. names ------------------------------------------------------------
    def test_names_written_and_display_name_kept_in_description(self):
        self.assertEqual(self.run_cli(), 0)
        cam = self.row_by_ip("172.16.0.10")
        self.assertEqual(cam["hostname"], "pet-cam.iot")
        self.assertEqual(cam["description"], "Pet Cam")
        self.assertEqual(self.row_by_ip("172.16.0.13")["hostname"], "k3s-agent-01")

    def test_display_name_never_becomes_the_dns_name(self):
        self.run_cli()
        sonos = self.row_by_ip("172.16.0.11")
        self.assertFalse(sonos["hostname"])  # empty, not 'SonosZP 16:0c'
        self.assertEqual(sonos["description"], "SonosZP 16:0c")
        for r in self.w.rows.values():
            self.assertNotRegex(r["hostname"] or "", r"[A-Z :']")

    def test_long_display_name_is_truncated_for_the_description_column(self):
        self.w.clients[1]["name"] = "x" * 200
        self.run_cli()
        self.assertEqual(len(self.row_by_ip("172.16.0.11")["description"]), u.DESC_MAX)

    # 2. no-forward-name marker ------------------------------------------------
    def test_randomised_mac_gets_no_forward_name_even_with_a_record(self):
        self.run_cli()
        phone = self.row_by_ip("172.16.0.12")
        self.assertFalse(phone["hostname"])
        self.assertEqual(phone["description"], "iPhone 0b:f1")
        self.assertEqual(phone["owner"], u.OWNER)

    def test_duplicate_curated_name_goes_to_nobody(self):
        self.w.users[3] = user("d4:ad:fc:00:00:13", "pet-cam", None)
        self.w.users[0] = user("d4:ad:fc:00:00:10", "pet-cam")
        self.run_cli()
        self.assertFalse(self.row_by_ip("172.16.0.10")["hostname"])
        self.assertFalse(self.row_by_ip("172.16.0.13")["hostname"])

    # 3. renames ---------------------------------------------------------------
    def test_rename_updates_existing_row(self):
        self.run_cli()
        self.w.users[0]["local_dns_record"] = "pet-cam2.iot.home.8none1.com"
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.row_by_ip("172.16.0.10")["hostname"], "pet-cam2.iot")
        self.assertEqual(sum(1 for r in self.w.rows.values() if r["mac"] == "d4:ad:fc:00:00:10"), 1)

    def test_name_removed_clears_hostname(self):
        self.run_cli()
        self.w.users[0].pop("local_dns_record")
        self.w.users[0]["name"] = "Pet Cam"
        self.run_cli()
        self.assertFalse(self.row_by_ip("172.16.0.10")["hostname"])

    def test_adopts_and_normalises_a_v1_row(self):
        rid = self.w.add_row("172.16.0.11", "d4:ad:fc:00:00:11", "SonosZP 16:0c", "Created by UniFi2IPAM Updated by UniFi2IPAM")
        self.run_cli()
        row = self.w.rows[rid]
        self.assertFalse(row["hostname"])
        self.assertEqual((row["owner"], row["note"]), (u.OWNER, u.LEGACY_NOTE))
        self.assertEqual(len(self.w.rows), 4)

    # 4. lastSeen --------------------------------------------------------------
    def test_last_seen_set_on_create_and_on_every_run(self):
        self.run_cli()
        self.assertTrue(all(r["lastSeen"] == NOW_S for r in self.w.rows.values()))
        later = datetime(2026, 11, 27, 13, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(u, "utcnow", lambda: later):
            self.run_cli()
        self.assertTrue(all(r["lastSeen"] == "2026-11-27 13:00:00" for r in self.w.rows.values()))

    def test_absent_client_row_is_not_touched(self):
        gone = self.w.add_row("172.16.0.77", "d4:ad:fc:00:00:77", "gone", "Created by UniFi2IPAM", u.OWNER)
        before = dict(self.w.rows[gone])
        self.run_cli()
        self.assertEqual(self.w.rows[gone], before)

    # 5. ownership --------------------------------------------------------------
    def test_foreign_row_on_the_address_is_never_touched_or_deleted(self):
        # v1.0.1 deleted this row (create -> "already exists" -> delete holder -> retry)
        rid = self.w.add_row("172.16.0.10", None, "pet-cam.iot.home.8none1.com")
        before = dict(self.w.rows[rid])
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.w.rows[rid], before)
        self.assertNotIn("DELETE", [e[0] for e in self.w.log])
        self.assertEqual(sum(1 for r in self.w.rows.values() if r["ip"] == "172.16.0.10"), 1)

    def test_foreign_row_with_a_matching_mac_is_not_updated_either(self):
        rid = self.w.add_row("172.16.0.10", "d4:ad:fc:00:00:10", "hand-made", "my note")
        before = dict(self.w.rows[rid])
        self.run_cli()
        self.assertEqual(self.w.rows[rid], before)

    def test_foreign_row_with_our_mac_at_another_address_is_ignored_not_moved_or_deleted(self):
        rid = self.w.add_row("172.16.0.200", "d4:ad:fc:00:00:10", "hand-made")
        before = dict(self.w.rows[rid])
        self.run_cli()
        self.assertEqual(self.w.rows[rid], before)
        self.assertEqual(self.row_by_ip("172.16.0.10")["owner"], u.OWNER)

    def test_every_row_created_is_marked_and_only_owned_rows_are_ever_deleted(self):
        self.run_cli()
        self.assertTrue(all(u.is_owned(r) for r in self.w.rows.values()))

    def test_ip_change_moves_our_row_create_then_delete(self):
        self.run_cli()
        self.w.clients[3]["ipAddress"] = "172.16.0.50"
        self.assertEqual(self.run_cli(), 0)
        self.assertIsNone(self.row_by_ip("172.16.0.13"))
        self.assertEqual(self.row_by_ip("172.16.0.50")["hostname"], "k3s-agent-01")
        methods = [e[0] for e in self.w.ipam_writes()][-2:]
        self.assertEqual(methods, ["POST", "DELETE"])

    def test_move_keeps_old_row_if_create_fails(self):
        self.run_cli()
        self.w.clients[3]["ipAddress"] = "172.16.0.50"
        self.w.write_fail = {"post"}
        self.assertEqual(self.run_cli(), 1)
        self.assertIsNotNone(self.row_by_ip("172.16.0.13"))

    def test_our_stale_row_on_a_reused_address_is_taken_over(self):
        stale = self.w.add_row("172.16.0.13", "d4:ad:fc:ee:ee:ee", "old device", u.LEGACY_NOTE)
        self.run_cli()
        self.assertEqual(self.w.rows[stale]["mac"], "d4:ad:fc:00:00:13")
        self.assertEqual(self.w.rows[stale]["hostname"], "k3s-agent-01")

    # 7. scope ----------------------------------------------------------------
    def test_out_of_scope_client_is_not_looked_up_or_written(self):
        self.run_cli()
        self.assertFalse(any("10.9.9.9" in e[2] or "d4:ad:fc:00:00:99" in e[2] for e in self.w.log))
        self.assertIsNone(self.row_by_ip("10.9.9.9"))

    def test_include_filter_that_matches_nothing_is_an_error_not_a_clean_run(self):
        self.assertEqual(self.run_cli(include="192.0.2.0/24"), 1)
        self.assertEqual(self.w.ipam_writes(), [])

    def test_no_nuke_mode_any_more(self):
        with self.assertRaises(SystemExit):
            u.cli(["--nuke-and-pave"])


class TestSafety(Base):
    def setUp(self):
        super().setUp()
        self.w.clients = [client("172.16.0.10", "d4:ad:fc:00:00:10", "Pet Cam")]
        self.w.users = [user("d4:ad:fc:00:00:10", "Pet Cam", "pet-cam.iot.home.8none1.com")]
        self.keep = self.w.add_row("172.16.0.99", "d4:ad:fc:00:00:99", "keep.me", u.LEGACY_NOTE, u.OWNER)

    def assert_untouched_and_failed(self, rc):
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.w.ipam_writes(), [])
        self.assertFalse([e for e in self.w.log if e[1] == "ipam"], "phpIPAM must not even be read")
        self.assertIn(self.keep, self.w.rows)

    def test_unifi_returns_no_clients(self):
        self.w.unifi_fail = "clients-empty"
        self.assert_untouched_and_failed(self.run_cli())

    def test_unifi_clients_500(self):
        self.w.unifi_fail = "clients-500"
        self.assert_untouched_and_failed(self.run_cli())

    def test_unifi_unreachable(self):
        self.w.unifi_fail = "net"
        self.assert_untouched_and_failed(self.run_cli())

    def test_unifi_site_list_fails(self):
        self.w.unifi_fail = "sites-500"
        self.assert_untouched_and_failed(self.run_cli_no_site())

    def run_cli_no_site(self):
        try:
            return u.cli(["--config", os.devnull, "--include-subnet", "172.16.0.0/24", "--dns-zone", ZONE, "--quiet"])
        except SystemExit as e:
            return e.code

    def test_name_source_unreadable_means_nothing_written(self):
        for mode in ("users-401", "users-empty"):
            with self.subTest(mode):
                self.w.log.clear()
                self.w.unifi_fail = mode
                self.assert_untouched_and_failed(self.run_cli())

    def test_phpipam_down_writes_nothing_and_fails(self):
        self.w.ipam_fail = {"subnets/": "net"}
        self.assertEqual(self.run_cli(), 1)
        self.assertEqual(self.w.ipam_writes(), [])

    def test_phpipam_lookup_error_is_not_read_as_not_found(self):
        # a 500 on search_mac must not lead to a create (v1.0.1 would have tried one)
        for mode in (500, 401, "net", "garbage"):
            with self.subTest(mode):
                self.w.log.clear()
                self.w.ipam_fail = {"search_mac": mode}
                self.assertEqual(self.run_cli(), 1)
                self.assertEqual(self.w.ipam_writes(), [])

    def test_phpipam_write_failure_exits_nonzero(self):
        self.w.write_fail = {"post"}
        self.assertEqual(self.run_cli(), 1)

    def test_healthy_run_exits_zero(self):
        self.assertEqual(self.run_cli(), 0)

    def test_unifi_is_never_written_to(self):
        self.run_cli()
        self.assertTrue(all(e[0] == "GET" for e in self.w.log if e[1] == "unifi"))
        with self.assertRaises(ValueError):
            u.xmit(u.Config("https://x/", "k", "https://i/", "k", True, unifi_legacy_url="https://x/"), "unifi", "sites", method="post")


class TestDryRun(Base):
    def setUp(self):
        super().setUp()
        self.w.clients = [client("172.16.0.10", "d4:ad:fc:00:00:10", "Pet Cam"), client("172.16.0.13", "d4:ad:fc:00:00:13", "k3s-agent-01"),
                          client("172.16.0.14", "d4:ad:fc:00:00:14", "Moved")]
        self.w.users = [user("d4:ad:fc:00:00:10", "Pet Cam", "pet-cam.iot.home.8none1.com"), user("d4:ad:fc:00:00:13", "k3s-agent-01")]
        self.w.add_row("172.16.0.13", "d4:ad:fc:00:00:13", "old", u.LEGACY_NOTE)            # would update
        self.w.add_row("172.16.0.50", "d4:ad:fc:00:00:14", "Moved", u.LEGACY_NOTE)           # would move (create+delete)
        self.w.add_row("172.16.0.10", None, "hand.made")                                      # foreign
        self.snapshot = {k: dict(v) for k, v in self.w.rows.items()}

    def test_dry_run_sends_no_write_and_changes_nothing(self):
        self.assertEqual(self.run_cli(dry=True), 0)
        self.assertEqual(self.w.writes(), [])
        self.assertEqual(self.w.rows, self.snapshot)

    def test_dry_run_still_reads_phpipam_and_reports_the_plan(self):
        with mock.patch.object(u, "setup_logging", lambda a: None), self.assertLogs(level="INFO") as cm:
            rc = u.cli(["--config", os.devnull, "--site-id", "site-1", "--include-subnet", "172.16.0.0/24",
                        "--dns-zone", ZONE, "--workers", "1", "--dryrun"])
        self.assertEqual(rc, 0)
        out = "\n".join(cm.output)
        self.assertIn("[dry-run] would PATCH", out)
        self.assertIn("[dry-run] would POST", out)
        self.assertIn("[dry-run] would DELETE", out)
        self.assertIn("left alone", out)

    def test_dry_run_unifi_failure_still_fails(self):
        self.w.unifi_fail = "clients-empty"
        self.assertNotEqual(self.run_cli(dry=True), 0)
        self.assertEqual(self.w.writes(), [])

    def test_dry_guard_lives_in_xmit_so_no_code_path_can_bypass_it(self):
        cfg = u.Config("https://x/", "k", "https://i/api/app/", "k", True, dry_run=True)
        for m in ("post", "patch", "put", "delete"):
            self.assertEqual(u.xmit(cfg, "ipam", "addresses", {"a": 1}, m).get("dry_run"), True)
        self.assertEqual(self.w.log, [])


class TestConfig(unittest.TestCase):
    def test_legacy_url_derived_from_integration_url(self):
        with mock.patch.dict(os.environ, {"UNIFI_API_KEY": "k", "IPAM_API_KEY": "k", "UNIFI_URL": "https://h/proxy/network/integration/v1/",
                                          "IPAM_BASE_URL": "https://i", "IPAM_APP_ID": "a"}):
            args = u.build_parser().parse_args(["--config", os.devnull])
            cfg = u.load_config(args)
        self.assertEqual(cfg.unifi_legacy_url, "https://h/proxy/network/")
        self.assertEqual(cfg.unifi_site_name, "default")


if __name__ == "__main__":
    unittest.main()
