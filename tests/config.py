# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

LOADER = pathlib.Path(sys.argv.pop(1)).resolve()
DATA_DIR = sys.argv.pop(1)
DEFAULTS = {"base-config": DATA_DIR + "/qm.json", "hostname": "qm",
            "ip": "10.90.0.2", "gateway": "10.90.0.1", "prefix-length": 24,
            "subnet": "10.90.0.0",
            "userns": "auto", "verbose": False,
            "resolv-conf": "/run/systemd/resolve/resolv.conf"
            if os.access("/run/systemd/resolve/resolv.conf", os.R_OK) else "/etc/resolv.conf"}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qmc-config-")
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.vendor = self.directory / "usr"
        self.admin = self.directory / "etc"

    def write(self, root, name, contents):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contents))
        return path

    def load(self, filename=None):
        return subprocess.run([str(LOADER), str(self.vendor), str(self.admin),
                               *([str(filename)] if filename else [])],
                              text=True, capture_output=True)

    def test_layer_precedence_and_sorting(self):
        main = self.write(self.vendor, "qmc.conf",
                          {"hostname": "vendor", "userns": "auto", "verbose": True})
        # Deliberately create drop-ins in reverse order.
        late = self.write(self.vendor, "qmc.conf.d/90-last.conf",
                          {"hostname": "vendor-last", "rootfs": "/vendor-root"})
        early = self.write(self.vendor, "qmc.conf.d/10-first.conf",
                           {"hostname": "vendor-first", "seccomp": "/seccomp"})
        admin_main = self.write(self.admin, "qmc.conf", {"hostname": "admin"})
        admin_late = self.write(self.admin, "qmc.conf.d/90-last.conf",
                                {"hostname": "admin-last", "verbose": False})
        admin_early = self.write(self.admin, "qmc.conf.d/10-first.conf",
                                 {"hostname": "admin-first", "userns": "off"})
        # Non-conf files are ignored even if malformed.
        (early.parent / "80-disabled.conf.bak").write_text("broken")
        expected = {**DEFAULTS, "hostname": "admin-last", "userns": "off", "verbose": False,
                    "rootfs": "/vendor-root", "seccomp": "/seccomp"}
        for path, hostname in [(admin_late, "admin-first"), (admin_early, "admin"),
                               (admin_main, "vendor-last"), (late, "vendor-first"),
                               (early, "vendor"), (main, None)]:
            result = self.load()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
            path.unlink()
            if path == admin_late:
                expected["verbose"] = True
            elif path == admin_early:
                expected["userns"] = "auto"
            elif path == late:
                del expected["rootfs"]
            elif path == early:
                del expected["seccomp"]
            if hostname:
                expected["hostname"] = hostname

    def test_missing_main_files_and_directories(self):
        self.assertEqual(json.loads(self.load().stdout), DEFAULTS)
        self.write(self.admin, "qmc.conf.d/20-only.conf", {"hostname": "only"})
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {**DEFAULTS, "hostname": "only"})

    def test_explicit_file_replaces_standard_layers(self):
        self.write(self.vendor, "qmc.conf", {"hostname": "vendor", "verbose": True})
        (self.vendor / "qmc.conf").write_text("broken")
        filename = self.write(self.directory, "standalone.conf", {"hostname": "standalone"})
        result = self.load(filename)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {**DEFAULTS, "hostname": "standalone"})
        filename.unlink()
        self.assertNotEqual(self.load(filename).returncode, 0)

    def test_invalid_files_are_reported_even_if_overridden(self):
        path = self.write(self.vendor, "qmc.conf.d/10-invalid.conf", {})
        self.write(self.admin, "qmc.conf", {"hostname": "valid"})
        for contents in ('{', '[]', '{} {}', '{"hostname": 1}',
                         '{"verbose": "yes"}', '{"hostname": null}',
                         '{"hostname": "a\\u0000b"}', '{"unknown": true}',
                         '{"hostname": "bad host"}', '{"userns": "invalid"}',
                         '{"rootfs": "relative"}',
                         '{"ip": "bad"}', '{"gateway": "::1"}',
                         '{"ip": "127.0.0.2"}', '{"gateway": "224.0.0.1"}',
                         '{"ip": "0.0.0.1"}', '{"ip": 123}',
                         '{"prefix-length": "24"}', '{"prefix-length": 24.5}',
                         '{"prefix-length": 0}', '{"prefix-length": 32}',
                         '{"runtime-dir": "/run/other"}', '{"prepare-only": true}'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                result = self.load()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(path), result.stderr)

    def test_network_settings_merge_before_subnet_validation(self):
        self.write(self.vendor, "qmc.conf", {"ip": "172.30.20.2"})
        self.write(self.admin, "qmc.conf.d/50-network.conf",
                   {"gateway": "172.30.20.1", "prefix-length": 28})
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {**DEFAULTS, "ip": "172.30.20.2", "gateway": "172.30.20.1",
                          "prefix-length": 28, "subnet": "172.30.20.0"})

    def test_collection_operations_accumulate_across_layers(self):
        self.write(self.vendor, "qmc.conf", {
            "devices": [{"source": "/dev/kvm", "optional": True}],
            "mounts": [{"source": "/host", "destination": "/shared"}],
            "add-capabilities": ["sys_resource"], "unmask": ["/proc/keys"],
            "memory-max": "5G"})
        self.write(self.vendor, "qmc.conf.d/50-more.conf", {
            "devices": [{"source": "/dev/fuse"}]})
        self.write(self.admin, "qmc.conf", {
            "devices": [{"destination": "/dev/kvm", "remove": True}],
            "mounts": [], "drop-capabilities": ["CAP_SYS_RESOURCE"],
            "unmask": ["/proc/sys"], "memory-max": "infinity"})
        self.write(self.admin, "qmc.conf.d/90-local.conf", {
            "devices": [{"source": "/dev/kvm"}],
            "mounts": [{"destination": "/shared", "remove": True}]})
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded["memory-max"], -1)
        self.assertEqual(loaded["path-operations"], [
            {"kind": "mounts", "source": "/host", "destination": "/shared"},
            {"kind": "devices", "source": "/dev/kvm", "destination": "/dev/kvm",
             "optional": True},
            {"kind": "devices", "source": "/dev/fuse", "destination": "/dev/fuse"},
            {"kind": "devices", "destination": "/dev/kvm", "remove": True},
            {"kind": "mounts", "destination": "/shared", "remove": True},
            {"kind": "devices", "source": "/dev/kvm", "destination": "/dev/kvm"}])
        self.assertEqual(loaded["capability-operations"], [
            {"name": "CAP_SYS_RESOURCE", "remove": False},
            {"name": "CAP_SYS_RESOURCE", "remove": True}])
        self.assertEqual(loaded["unmask"], ["/proc/keys", "/proc/sys"])

    def test_collections_survive_growth_and_layer_release(self):
        options = [f"option-{i}" for i in range(20)]
        mounts = [{"source": f"/host/{i}", "destination": f"/shared/{i}"}
                  for i in range(40)]
        mounts[0]["options"] = options
        mounts[-1]["options"] = []
        unmask = [f"/proc/test-{i}" for i in range(40)]
        for root, start in [(self.vendor, 0), (self.admin, 20)]:
            self.write(root, "qmc.conf", {
                "mounts": mounts[start:start + 20],
                "add-capabilities": ["chown", "sys_resource"] * 10,
                "unmask": unmask[start:start + 20]})

        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded["path-operations"],
                         [{"kind": "mounts", **mount} for mount in mounts])
        self.assertEqual(loaded["capability-operations"], [
            {"name": f"CAP_{name}", "remove": False}
            for name in ["CHOWN", "SYS_RESOURCE"] * 20])
        self.assertEqual(loaded["unmask"], unmask)

    def test_operation_order_follows_tables_instead_of_json_keys(self):
        self.write(self.vendor, "qmc.conf", {
            "devices": [{"remove": True, "destination": "/shared"}],
            "mounts": [{"destination": "/shared", "source": "/host"}],
            "add-capabilities": ["chown"],
            "drop-capabilities": ["chown"]})
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded["path-operations"], [
            {"kind": "mounts", "source": "/host", "destination": "/shared"},
            {"kind": "devices", "destination": "/shared", "remove": True}])
        self.assertEqual(loaded["capability-operations"], [
            {"name": "CAP_CHOWN", "remove": True},
            {"name": "CAP_CHOWN", "remove": False}])

    def test_nested_errors_identify_the_field_and_array_index(self):
        cases = [
            ({"mounts": [{"destination": "/shared"}]}, "mounts[0].source is required"),
            ({"mounts": [{"source": "/host"}]}, "mounts[0].destination is required"),
            ({"devices": [{}]}, "devices[0].source is required"),
            ({"devices": [{"remove": True}]}, "devices[0].destination is required"),
            ({"mounts": [{"source": "/host", "destination": "/shared",
                          "optional": None}]}, "mounts[0].optional must be a JSON boolean"),
            ({"mounts": [{"source": "/host", "destination": "/shared",
                          "options": ["bind", 1]}]}, "mounts[0].options[1] must be a JSON string"),
            ({"devices": [{"source": "/dev/null", "options": []}]},
             "unknown configuration.devices[0] key: options"),
            ({"mounts": [{"destination": "/shared", "remove": True, "optional": False}]},
             "unknown configuration.mounts[0] key: optional"),
        ]
        for settings, error in cases:
            with self.subTest(settings=settings):
                path = self.write(self.vendor, "qmc.conf", settings)
                result = self.load()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(path), result.stderr)
                self.assertIn(error, result.stderr)

    def test_memory_sizes(self):
        for value, expected in [(123, 123), ("123", 123), ("1K", 1024),
                                ("5GiB", 5 * 1024 ** 3), ("2MB", 2 * 1024 ** 2),
                                ("1E", 1024 ** 6), ("infinity", -1),
                                (2 ** 63 - 1, 2 ** 63 - 1)]:
            with self.subTest(value=value):
                self.write(self.admin, "qmc.conf", {"memory-max": value})
                result = self.load()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["memory-max"], expected)

    def test_paths_are_canonicalized(self):
        paths = {key: f"/config/{key}" for key in
                 ("base-config", "rootfs", "etc-dir", "var-dir", "seccomp", "resolv-conf")}
        self.write(self.vendor, "qmc.conf", {
            **{key: f"//config/unused/../{key}/./" for key in paths},
            "mounts": [{"source": "//host/unused/../shared//",
                        "destination": "//shared/./tree/../"},
                       {"source": "/tmp/..", "destination": "/host-root/"}],
            "devices": [{"source": "//dev/./null//"}],
            "unmask": ["//proc/./keys/", "/proc/temp/../sys", "ALL"]})
        self.write(self.admin, "qmc.conf", {
            "mounts": [{"destination": "/shared/child/..", "remove": True}]})

        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout)
        for key, value in paths.items():
            self.assertEqual(loaded[key], value)
        self.assertEqual(loaded["path-operations"], [
            {"kind": "mounts", "source": "/host/shared", "destination": "/shared"},
            {"kind": "mounts", "source": "/", "destination": "/host-root"},
            {"kind": "devices", "source": "/dev/null", "destination": "/dev/null"},
            {"kind": "mounts", "destination": "/shared", "remove": True}])
        self.assertEqual(loaded["unmask"], ["/proc/keys", "/proc/sys", "ALL"])

    def test_invalid_container_options_include_filename(self):
        cases = [
            {"memory-max": value} for value in
            [0, -1, 1.5, True, None, "", "0", "-1", "1.5G", "1g", "1Z",
             "5Gjunk", " 5G", "+5G", "1e3", "8E", "999999999999999999999999", 2 ** 63]
        ] + [
            {"devices": None}, {"mounts": {}}, {"devices": ["/dev/null"]},
            {"devices": [{"source": "relative"}]},
            {"devices": [{"source": "/dev/null", "options": []}]},
            {"mounts": [{"source": "/host", "destination": "/shared", "typo": True}]},
            {"mounts": [{"source": "/host", "destination": "/shared", "options": "ro"}]},
            {"mounts": [{"source": "/host", "destination": "/shared", "options": [1]}]},
            {"mounts": [{"source": "/host", "destination": "/shared", "options": None}]},
            {"mounts": [{"source": "/host", "destination": "/shared", "optional": 1}]},
            {"devices": [{"destination": "/dev/kvm", "remove": "yes"}]},
            {"devices": [{"source": "/dev/kvm", "remove": True}]},
            {"devices": [{"destination": "/dev/kvm", "source": "/dev/null", "remove": True}]},
            {"devices": [{"destination": "/dev/kvm", "source": None, "remove": True}]},
            {"mounts": [{"destination": "/shared", "remove": True, "optional": False}]},
            {"add-capabilities": ["CAP_TYPO"]}, {"drop-capabilities": "all"},
            {"add-capabilities": [None]}, {"unmask": True}, {"unmask": ["relative"]},
        ]
        cases += [{"mounts": [{"source": "/host", "destination": dest}]}
                  for dest in ["/", "//", "/etc/hosts", "/etc//hosts", "/etc/./hosts",
                               "/tmp/../etc/hosts", "/etc/hosts/", "/tmp/..",
                               "/etc/a/../../run/secrets/."]]
        path = self.write(self.vendor, "qmc.conf.d/10-invalid.conf", {})
        for settings in cases:
            with self.subTest(settings=settings):
                path.write_text(json.dumps(settings))
                result = self.load()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(path), result.stderr)

    def test_network_addresses_must_be_usable_peers(self):
        for settings in ({"ip": "10.90.0.1"}, {"gateway": "10.91.0.1"},
                         {"ip": "10.90.0.0"}, {"gateway": "10.90.0.255"}):
            with self.subTest(settings=settings):
                self.write(self.admin, "qmc.conf", settings)
                result = self.load()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ip and gateway", result.stderr)
        self.write(self.admin, "qmc.conf",
                   {"ip": "192.0.2.2", "gateway": "192.0.2.3", "prefix-length": 31})
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["subnet"], "192.0.2.2")

    def test_read_and_scan_errors_are_not_ignored(self):
        self.vendor.mkdir()
        path = self.vendor / "qmc.conf"
        path.mkdir()
        result = self.load()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(path), result.stderr)
        path.rmdir()
        path = self.vendor / "qmc.conf.d"
        path.write_text("not a directory")
        result = self.load()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(path), result.stderr)


unittest.main()
