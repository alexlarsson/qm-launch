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

LAUNCHER = pathlib.Path(sys.argv[1]).resolve()
SOURCE = pathlib.Path(sys.argv[2]).resolve()
EXPECTED_CGROUP = sys.argv[3] if len(sys.argv) > 3 else None
PRIVATE_PATHS = os.environ.get("QM_TEST_PRIVATE_PATHS") == "1"
HOST_MACHINE_ID = "d2c79cc3388a48e494c8cd22cce8f499"
sys.argv = sys.argv[:1]


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qm-prepare-")
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.runtime = self.directory / "runtime"
        self.resolv = self.directory / "resolv.conf"
        self.resolv.write_text("nameserver 192.0.2.53\n")
        self.subids = pathlib.Path("/etc")
        self.devices = pathlib.Path("/dev")
        self.hosts = pathlib.Path("/etc/hosts")
        if PRIVATE_PATHS:
            self.hosts.write_text("127.0.0.1\tlocalhost\n")
            pathlib.Path("/etc/machine-id").write_text(HOST_MACHINE_ID + "\n")
            for name in ("subuid", "subgid"):
                (self.subids / name).write_text("")
            for name in ("kvm", "fuse"):
                (self.devices / name).unlink(missing_ok=True)
        self.base = json.loads((SOURCE / "qm.json").read_text())
        self.seccomp = json.loads((SOURCE / "tests/seccomp-no-rt.json").read_text())

    def prepare(self, base=None, seccomp=None, hostname="qm", extra=(), env=None,
                host_resolv=False, settings=None):
        inputs = {
            "base.json": self.base if base is None else base,
            "seccomp.json": self.seccomp if seccomp is None else seccomp,
        }
        for name, value in inputs.items():
            (self.directory / name).write_text(json.dumps(value))
        config = {
            "base-config": str(self.directory / "base.json"),
            "hostname": hostname,
            "userns": "auto" if PRIVATE_PATHS else "off",
        }
        if seccomp is not False:
            config["seccomp"] = str(self.directory / "seccomp.json")
        if not host_resolv:
            config["resolv-conf"] = str(self.resolv)
        config.update(settings or {})
        filename = self.directory / "qmc.conf"
        filename.write_text(json.dumps(config))
        return subprocess.run(
            [str(LAUNCHER), "launch", "--prepare-only", "--runtime-dir", str(self.runtime),
             "--config", str(filename), *extra],
            text=True, capture_output=True, env=env,
        )

    def generated(self):
        return json.loads((self.runtime / "config.json").read_text())

    def test_static_config(self):
        result = self.prepare(hostname="qm-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.generated()
        self.assertEqual(config["hostname"], "qm-test")
        self.assertIn("HOSTNAME=qm-test", config["process"]["env"])
        self.assertFalse(any(e.startswith("container_uuid=") for e in config["process"]["env"]))
        self.assertEqual(config["process"]["args"],
                         [*self.base["process"]["args"], "--machine-id=" + HOST_MACHINE_ID])
        self.assertEqual(config["root"], self.base["root"])
        self.assertEqual(config["process"]["capabilities"], self.base["process"]["capabilities"])
        self.assertEqual(config["linux"]["mountLabel"], self.base["linux"]["mountLabel"])
        self.assertTrue(config["linux"]["cgroupsPath"].endswith("/payload"))
        if EXPECTED_CGROUP is not None:
            self.assertEqual(config["linux"]["cgroupsPath"], EXPECTED_CGROUP)
        netns = next(n for n in config["linux"]["namespaces"] if n["type"] == "network")
        self.assertEqual(netns["path"], str(self.runtime / "netns"))
        mounts = {m["destination"]: m for m in config["mounts"]}
        self.assertNotIn("/etc/machine-id", mounts)
        self.assertEqual(mounts["/dev/shm"]["type"], "tmpfs")
        self.assertIn("size=65536k", mounts["/dev/shm"]["options"])
        for destination in ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/run/.containerenv", "/run/secrets"):
            self.assertTrue(pathlib.Path(mounts[destination]["source"]).exists())
        self.assertEqual((self.runtime / "files/hostname").read_text(), "qm-test\n")
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), self.resolv.read_text())
        self.assertIn("10.90.0.2\tqm-test qm\n", (self.runtime / "files/hosts").read_text())
        self.assertNotIn("overlay-containers", json.dumps(config))
        self.assertEqual(config["annotations"]["run.oci.mount_context_type"], "rootcontext")
        profile = config["linux"]["seccomp"]
        self.assertNotIn("archMap", profile)
        self.assertEqual(profile["defaultErrnoRet"], 38)
        rules = profile["syscalls"]
        self.assertTrue(any(r["names"] == ["sched_setattr"] and r.get("errnoRet") == 1 for r in rules))
        scheduling = [r for r in rules if r["names"] == ["sched_setscheduler"]]
        self.assertEqual([r["args"][0]["value"] for r in scheduling], [0, 3, 5])
        self.assertTrue(all("includes" not in r and "excludes" not in r for r in rules))

    def test_machine_id_validation(self):
        for value in ("", "0" * 32, "1" * 31, "1" * 33, "g" * 32):
            with self.subTest(machine_id=value):
                if PRIVATE_PATHS:
                    pathlib.Path("/etc/machine-id").write_text(value)
                result = self.prepare(env={**os.environ, "QM_TEST_MACHINE_ID": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid host machine ID", result.stderr)

    def test_custom_init_arguments(self):
        self.base["process"]["args"] = ["/init", "--custom-argument"]
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["process"]["args"], self.base["process"]["args"])

    def test_machine_id_overrides_base_argument(self):
        self.base["process"]["args"] += ["--machine-id=" + "1" * 32]
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["process"]["args"][-1],
                         "--machine-id=" + HOST_MACHINE_ID)

    def test_oci_seccomp_roundtrip(self):
        profile = {"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 38,
                   "syscalls": [{"names": ["exit"], "action": "SCMP_ACT_ALLOW"}]}
        result = self.prepare(seccomp=profile)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["linux"]["seccomp"], profile)

    def test_console_and_verbose_logging(self):
        result = self.prepare(extra=("--verbose",))
        self.assertEqual(result.returncode, 0, result.stderr)
        process = self.generated()["process"]
        self.assertTrue(process["terminal"])
        self.assertFalse(any(e.startswith("SYSTEMD_LOG_") for e in process["env"]))
        self.base["process"]["env"] += ["SYSTEMD_LOG_LEVEL=warning",
                                        "SYSTEMD_LOG_TARGET=journal"]
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        process = self.generated()["process"]
        self.assertTrue(process["terminal"])
        self.assertIn("SYSTEMD_LOG_LEVEL=warning", process["env"])
        self.assertIn("SYSTEMD_LOG_TARGET=journal", process["env"])
        result = self.prepare(extra=("--verbose",))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.generated()["process"]["env"]
        self.assertEqual([e for e in env if e.startswith("SYSTEMD_LOG_LEVEL=")],
                         ["SYSTEMD_LOG_LEVEL=warning"])
        self.assertEqual([e for e in env if e.startswith("SYSTEMD_LOG_TARGET=")],
                         ["SYSTEMD_LOG_TARGET=journal"])
        self.assertIn("HOME=/root", env)

    def test_configuration_verbosity_and_cli_override(self):
        result = self.prepare(settings={"verbose": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Loading OCI configuration", result.stderr)
        result = self.prepare(settings={"verbose": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Loading OCI configuration", result.stderr)
        result = self.prepare(settings={"verbose": False}, extra=("--verbose",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Loading OCI configuration", result.stderr)

    def test_removed_options_and_positional_config(self):
        for option in ("--hostname", "--rootfs", "--etc-dir", "--var-dir",
                       "--seccomp", "--resolv-conf", "--userns"):
            with self.subTest(option=option):
                result = self.prepare(extra=(option, "value"))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unrecognized option", result.stderr)
        result = self.prepare(extra=(str(self.directory / "base.json"),))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("launch takes no positional arguments", result.stderr)

    def test_seccomp_required(self):
        result = self.prepare(seccomp=False)
        self.assertGreater(result.returncode, 0)
        self.assertIn("set seccomp in qmc.conf", result.stderr)

    def test_inline_seccomp(self):
        profile = {"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 38, "syscalls": []}
        self.base["linux"]["seccomp"] = profile
        result = self.prepare(seccomp=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["linux"]["seccomp"], profile)

    def test_paths_and_timezone(self):
        result = self.prepare(settings={"rootfs": "/test/rootfs", "etc-dir": "/test/etc",
                                        "var-dir": "/test/var"}, env={**os.environ, "TZ": "Europe/Stockholm"})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.generated()
        self.assertEqual(config["root"]["path"], "/test/rootfs")
        self.assertEqual(config["root"]["readonly"], self.base["root"]["readonly"])
        mounts = {m["destination"]: m["source"] for m in config["mounts"]}
        self.assertEqual(mounts["/etc"], "/test/etc")
        self.assertEqual(mounts["/var"], "/test/var")
        self.assertEqual(mounts["/var/tmp"], "/test/var/tmp")
        self.assertIn("TZ=Europe/Stockholm", config["process"]["env"])

    def test_localtime_mount(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = {m["destination"]: m for m in self.generated()["mounts"]}
        if pathlib.Path("/etc/localtime").exists():
            self.assertEqual(mounts["/etc/localtime"]["source"], "/etc/localtime")
            self.assertIn("ro", mounts["/etc/localtime"]["options"])
        else:
            self.assertNotIn("/etc/localtime", mounts)

    def test_missing_localtime_mount(self):
        for mount in self.base["mounts"]:
            if mount["destination"] == "/etc/localtime":
                mount["source"] = str(self.directory / "missing-localtime")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(m["destination"] == "/etc/localtime"
                             for m in self.generated()["mounts"]))

    def test_missing_resolver(self):
        self.resolv.unlink()
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), "")
        self.resolv.symlink_to(self.directory / "missing/resolv.conf")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), "")

    def test_resolver_read_error(self):
        self.resolv.unlink()
        self.resolv.mkdir()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"read {self.resolv}", result.stderr)

    def update_resolv(self, *extra, env=None):
        filename = self.directory / "update.conf"
        filename.write_text(json.dumps({"resolv-conf": str(self.resolv)}))
        return subprocess.run(
            [str(LAUNCHER), "update-resolv-conf", "--config", str(filename),
             *extra], text=True, capture_output=True,
            env={**os.environ, "RUNTIME_DIRECTORY": str(self.runtime), **(env or {})})

    @unittest.skipUnless(EXPECTED_CGROUP is not None, "requires the preparation test's truncate fixture")
    def test_update_resolver_before_truncate(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.runtime / "files/resolv.conf"
        snapshot = self.directory / "before-truncate"
        snapshot.mkdir()
        for contents in ("nameserver 1.1.1.1\n", "", "search example.test\nnameserver 192.0.2.54\n"):
            with self.subTest(contents=contents):
                old_size = target.stat().st_size
                self.resolv.write_text(contents)
                result = self.update_resolv(env={"QM_TEST_BEFORE_TRUNCATE_DIR": str(snapshot)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((snapshot / "resolv.conf").read_bytes(), contents.encode().ljust(old_size, b" "))
                self.assertEqual(target.read_text(), contents)

    def test_update_resolver_in_place(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.runtime / "files/resolv.conf"
        alias = self.directory / "bound-resolv.conf"
        os.link(target, alias)
        original = target.stat()
        for contents in ("search example.test\nnameserver 192.0.2.54\n",
                         "nameserver 192.0.2.55\n", ""):
            with self.subTest(contents=contents):
                self.resolv.write_text(contents)
                result = self.update_resolv()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(target.stat().st_ino, original.st_ino)
                self.assertEqual(target.stat().st_mode, original.st_mode)
                self.assertEqual(alias.read_text(), contents)

    def test_update_resolver_missing_files(self):
        result = self.update_resolv()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runtime.exists())
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = self.resolv.read_text()
        self.resolv.unlink()
        result = self.update_resolv()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), contents)

    def test_update_resolver_read_error(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = self.resolv.read_text()
        self.resolv.unlink()
        self.resolv.mkdir()
        result = self.update_resolv()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"read {self.resolv}", result.stderr)
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), contents)

    def test_update_resolver_arguments(self):
        for extra in (("cleanup",), ("--prepare-only",),
                      ("exec", "/bin/true"), (str(SOURCE / "qm.json"),)):
            with self.subTest(extra=extra):
                result = self.update_resolv(*extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("update-resolv-conf", result.stderr)

    def test_update_hosts_in_place(self):
        snapshot = self.directory / "before-truncate"
        snapshot.mkdir()
        for hostname in ("qm", "qm-test"):
            with self.subTest(hostname=hostname):
                result = self.prepare(hostname=hostname)
                self.assertEqual(result.returncode, 0, result.stderr)
                target = self.runtime / "files/hosts"
                contents = target.read_text()
                self.assertIn("10.90.0.1\thost.containers.internal host.docker.internal\n", contents)
                self.assertIn(f"10.90.0.2\t{hostname}" + (" qm" if hostname != "qm" else "") + "\n", contents)
                alias = self.directory / "bound-hosts"
                alias.unlink(missing_ok=True)
                os.link(target, alias)
                original = target.stat()
                for previous in ("", contents + "192.0.2.100\tstale-host\n"):
                    target.write_text(previous)
                    result = self.update_resolv(env={"QM_TEST_BEFORE_TRUNCATE_DIR": str(snapshot)})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(target.stat().st_ino, original.st_ino)
                    self.assertEqual(target.stat().st_mode, original.st_mode)
                    self.assertEqual(alias.read_text(), contents)
                    if EXPECTED_CGROUP is not None:
                        self.assertEqual((snapshot / "hosts").read_bytes(),
                                         contents.encode().ljust(len(previous.encode()), b" "))
                target.unlink()
                result = self.update_resolv()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(target.exists())

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc")
    def test_update_hosts_without_resolver(self):
        result = self.prepare(hostname="qm-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.resolv.unlink()
        target = self.runtime / "files/hosts"
        for contents in ("127.0.0.1\tlocalhost\n192.0.2.100\tnew-host\n", ""):
            self.hosts.write_text(contents)
            result = self.update_resolv()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.read_text().startswith(contents + "\n"))
            self.assertIn("10.90.0.2\tqm-test qm\n", target.read_text())
            self.assertNotIn("stale-host", target.read_text())
            if not contents:
                self.assertNotIn("new-host", target.read_text())
        previous = target.read_text()
        self.hosts.unlink()
        result = self.update_resolv()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), previous)

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc")
    def test_update_hosts_read_error(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.runtime / "files/hosts"
        previous = target.read_text()
        self.hosts.unlink()
        self.hosts.mkdir()
        try:
            result = self.update_resolv()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("read /etc/hosts", result.stderr)
            self.assertEqual(target.read_text(), previous)
        finally:
            self.hosts.rmdir()

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc and /run")
    def test_missing_default_resolver(self):
        result = self.prepare(host_resolv=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / "files/resolv.conf").read_text(), "")

    def test_static_mount_settings_are_preserved(self):
        localtime = self.directory / "localtime"
        localtime.touch()
        for mount in self.base["mounts"]:
            if mount["destination"] == "/dev/shm":
                mount["options"] = ["rw", "nosuid", "nodev", "size=128m"]
            elif mount["destination"] == "/etc/localtime":
                mount["source"] = str(localtime)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = [m for m in self.base["mounts"]
                    if m["destination"] in ("/dev/shm", "/etc/localtime")]
        actual = [m for m in self.generated()["mounts"]
                  if m["destination"] in ("/dev/shm", "/etc/localtime")]
        self.assertEqual(actual, expected)

    def test_missing_annotations(self):
        self.base.pop("annotations")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generated()["annotations"],
                         {"io.container.manager": "qmc"})

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc and /dev")
    def test_existing_device_mount(self):
        self.base["mounts"].append({"destination": "/dev/kvm", "type": "tmpfs",
                                    "source": "old", "options": ["ro", "nodev"]})
        (self.devices / "kvm").symlink_to("/dev/null")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = [m for m in self.generated()["mounts"] if m["destination"] == "/dev/kvm"]
        self.assertEqual(mounts, [{"destination": "/dev/kvm", "type": "bind",
                                   "source": str(self.devices / "kvm"),
                                   "options": ["bind", "rprivate", "nosuid", "noexec"]}])

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc and /dev")
    def test_optional_devices(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(m["destination"] in ("/dev/kvm", "/dev/fuse")
                             for m in self.generated()["mounts"]))
        (self.devices / "kvm").symlink_to("/dev/null")
        (self.devices / "fuse").symlink_to("/dev/zero")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.generated()
        mounts = {m["destination"]: m for m in config["mounts"]}
        self.assertEqual(mounts["/dev/kvm"]["source"], str(self.devices / "kvm"))
        self.assertEqual(mounts["/dev/fuse"]["source"], str(self.devices / "fuse"))
        rules = config["linux"]["resources"]["devices"]
        self.assertTrue(any(r.get("allow") is True and r.get("major") == 1 and
                            r.get("minor") == 3 for r in rules))
        self.assertTrue(any(r.get("allow") is True and r.get("major") == 1 and
                            r.get("minor") == 5 for r in rules))

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc and /dev")
    def test_auto_userns(self):
        (self.subids / "subuid").write_text("other:100000:65536\nqm_root:1000000000:1500000000\n")
        (self.subids / "subgid").write_text("qm_root:2000000000:500000000\n")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        linux = self.generated()["linux"]
        self.assertIn({"type": "user"}, linux["namespaces"])
        self.assertEqual(linux["uidMappings"],
                         [{"containerID": 0, "hostID": 1000000000, "size": 1500000000}])
        self.assertEqual(linux["gidMappings"],
                         [{"containerID": 0, "hostID": 2000000000, "size": 500000000}])
        result = self.prepare(settings={"userns": "off"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("uidMappings", self.generated()["linux"])

    @unittest.skipUnless(PRIVATE_PATHS, "requires the smoke test's private /etc and /dev")
    def test_userns_invalid_or_incomplete(self):
        result = self.prepare(settings={"userns": "required"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)
        (self.subids / "subuid").write_text("qm_root:1000000000:1000\n")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("both subuid and subgid", result.stderr)
        (self.subids / "subgid").write_text("qm_root:4294967290:10\n")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid qm_root size", result.stderr)

    def test_capability_filter(self):
        for capability in ("CAP_SYS_ADMIN", "CAP_BPF", "CAP_PERFMON", "CAP_AUDIT_WRITE"):
            self.base["process"]["capabilities"]["bounding"].remove(capability)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = self.generated()["linux"]["seccomp"]["syscalls"]
        for syscall in ("bpf", "perf_event_open"):
            self.assertTrue(any(syscall in r["names"] and r["action"] == "SCMP_ACT_ERRNO" for r in rules))
            self.assertFalse(any(syscall in r["names"] and r["action"] == "SCMP_ACT_ALLOW" for r in rules))
        self.assertTrue(any(r["names"] == ["socket"] and r.get("errnoRet") == 22 for r in rules))

    def test_invalid_inputs(self):
        cases = [
            ({"hostname": "bad\nhost"}, "invalid hostname"),
            ({"settings": {"userns": "invalid"}}, "invalid userns mode"),
            ({"base": {**self.base, "annotations": []}}, "invalid annotations"),
            ({"base": {**self.base, "process": {**self.base["process"], "env": [None]}}}, "JSON string"),
            ({"seccomp": {"syscalls": [{"includes": {"minKernel": "6.0"}}]}}, "unsupported seccomp condition"),
        ]
        for arguments, error in cases:
            with self.subTest(error=error):
                result = self.prepare(**arguments)
                self.assertGreater(result.returncode, 0)
                self.assertIn(error, result.stderr)

    def test_absolute_path_errors(self):
        for option in ("--runtime-dir", "rootfs", "etc-dir", "var-dir", "base-config", "seccomp", "resolv-conf"):
            with self.subTest(option=option):
                result = self.prepare(**({"extra": (option, "relative")} if option.startswith("--")
                                         else {"settings": {option: "relative"}}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{option} must be an absolute path", result.stderr)

    def test_cleanup_after_delete_failure(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.runtime / "crun/qm"
        state.mkdir()
        (state / "status").touch()
        (self.runtime / "netns").touch()
        saved = {"version": 1, "netns": str(self.runtime / "netns"),
                 "crun": {"executable": "/bin/false", "root": str(self.runtime / "crun"), "id": "qm"}}
        (self.runtime / "state.json").write_text(json.dumps(saved))
        result = subprocess.run(
            [str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
            text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / "netns").exists())
        self.assertEqual(json.loads((self.runtime / "state.json").read_text()), saved)
        (state / "status").unlink()
        result = subprocess.run(
            [str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.runtime / "state.json").exists())

    def test_cleanup_without_namespace(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.runtime / "netns").touch()
        (self.runtime / "state.json").write_text(json.dumps(
            {"version": 1, "netns": str(self.runtime / "netns")}))
        result = subprocess.run([str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)])
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.runtime / "netns").exists())
        self.assertFalse((self.runtime / "state.json").exists())

    def test_cleanup_without_state_preserves_unrecorded_files(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.runtime / "state.json").exists())
        (self.runtime / "netns").touch()
        result = subprocess.run([str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.runtime / "netns").exists())

    def test_cleanup_uses_recorded_crun_and_namespace(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        pin = self.directory / "recorded-pin"
        pin.touch()
        (self.runtime / "netns").touch()
        root = self.directory / "recorded-crun-root"
        status = root / "recorded-id/status"
        status.parent.mkdir(parents=True)
        status.touch()
        default_status = self.runtime / "crun/qm/status"
        default_status.parent.mkdir()
        default_status.touch()
        arguments = self.directory / "delete-arguments.json"
        executable = self.directory / "recorded-crun"
        executable.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"pathlib.Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"pathlib.Path({str(status)!r}).unlink()\n")
        executable.chmod(0o755)
        (self.runtime / "state.json").write_text(json.dumps({
            "version": 1, "netns": str(pin),
            "crun": {"executable": str(executable), "root": str(root), "id": "recorded-id"}}))
        result = subprocess.run([str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(arguments.read_text()),
                         ["--root", str(root), "delete", "--force", "recorded-id"])
        self.assertFalse(pin.exists())
        self.assertFalse(status.exists())
        self.assertFalse((self.runtime / "state.json").exists())
        self.assertTrue(default_status.exists())
        self.assertTrue((self.runtime / "netns").exists())

    def test_invalid_cleanup_state_is_preserved(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        pin = self.runtime / "netns"
        pin.touch()
        state = self.runtime / "state.json"
        invalid_network = json.dumps({"version": 1, "netns": str(pin),
                                      "network": {"owner": "qm-launch:/saved",
                                                  "veth": "a" * 16, "nft_table": "saved"}})
        for contents in ("{", '{"version":2}', '{"version":1,"netns":false}', invalid_network):
            with self.subTest(contents=contents):
                state.write_text(contents)
                result = subprocess.run([str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state.read_text(), contents)
                self.assertTrue(pin.exists())

    @unittest.skipUnless(os.geteuid() == 0, "startup requires root")
    def test_startup_warns_and_retries_partial_cleanup(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = (self.runtime / "config.json").read_bytes()
        status = self.runtime / "crun/qm/status"
        status.parent.mkdir()
        status.touch()
        pin = self.runtime / "netns"
        pin.touch()
        saved = json.dumps({"version": 1, "bundle": str(self.runtime), "netns": str(pin),
                            "crun": {"executable": "/bin/false",
                                     "root": str(self.runtime / "crun"), "id": "qm"}})
        state = self.runtime / "state.json"
        state.write_text(saved)
        missing_config = self.directory / "missing.json"
        filename = self.directory / "qmc.conf"
        filename.write_text(json.dumps({"base-config": str(missing_config)}))
        command = [str(LAUNCHER), "launch", "--runtime-dir", str(self.runtime),
                   "--config", str(filename)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup incomplete; continuing startup", result.stderr)
        self.assertIn(f"read {missing_config}", result.stderr)
        self.assertEqual(state.read_text(), saved)
        self.assertEqual((self.runtime / "config.json").read_bytes(), bundle)
        self.assertFalse(pin.exists())
        status.unlink()
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"read {missing_config}", result.stderr)
        self.assertFalse(state.exists())
        self.assertFalse(self.runtime.exists())

    def test_successful_cleanup_removes_generated_runtime(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.runtime / "netns").touch()
        (self.runtime / "state.json").write_text(json.dumps({
            "version": 1, "bundle": str(self.runtime), "netns": str(self.runtime / "netns")}))
        command = [str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)]
        for _ in range(2):
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.runtime.exists())

    def test_cleanup_removes_bundle_without_following_symlinks(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.runtime / "extra-file").write_text("another runtime file\n")
        target = self.directory / "symlink-target"
        target.write_text("outside the bundle\n")
        hostname = self.runtime / "files/hostname"
        hostname.unlink()
        hostname.symlink_to(target)
        crun = self.runtime / "crun"
        cache = crun / ".cache/seccomp"
        cache.mkdir(parents=True)
        (cache / "filter").write_text("cached filter\n")
        (cache / "link").symlink_to(target)
        (crun / ".empty-directory").mkdir(mode=0o555)
        (self.runtime / "state.json").write_text(json.dumps({
            "version": 1, "bundle": str(self.runtime),
            "crun": {"executable": "/bin/false", "root": str(crun), "id": "qm"}}))
        result = subprocess.run(
            [str(LAUNCHER), "cleanup", "--runtime-dir", str(self.runtime)],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(target.read_text(), "outside the bundle\n")

    def test_prepare_preserves_existing_cleanup_state(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = '{"version":1}'
        (self.runtime / "state.json").write_text(saved)
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup state already exists", result.stderr)
        self.assertEqual((self.runtime / "state.json").read_text(), saved)


unittest.main()
