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
                         '{"runtime-dir": "/run/other"}', '{"prepare-only": true}'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                result = self.load()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(path), result.stderr)

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
