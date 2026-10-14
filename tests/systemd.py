# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

"""Run inside the same delegated user, mount and network namespaces as smoke.py."""

import json
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile

SOURCE = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = pathlib.Path(sys.argv[1]).resolve()
MACHINE_ID_STATE = sys.argv[2] if len(sys.argv) > 2 else "existing"
HOST_MACHINE_ID = pathlib.Path("/etc/machine-id").read_text().strip()

subprocess.run(["mount", "--make-rprivate", "/"], check=True)
subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/run"], check=True)
pathlib.Path("/run/systemd").mkdir()

with tempfile.TemporaryDirectory(prefix="qm-systemd-") as temporary:
    directory = pathlib.Path(temporary)
    root = directory / "root"
    root.mkdir()
    for name in ("lib", "lib64", "bin", "sbin"):
        (root / name).symlink_to("usr/" + name)
    etc = root / "etc"
    etc.mkdir()
    initial_id = "d2c79cc3388a48e494c8cd22cce8f499"
    if initial_id == HOST_MACHINE_ID:
        initial_id = "1" * 32
    if MACHINE_ID_STATE == "uninitialized":
        initial_id = "uninitialized"
    (etc / "machine-id").write_text(initial_id + "\n")
    (etc / "os-release").write_text('ID=qm-test\nPRETTY_NAME="QM test"\n')
    units = etc / "systemd/system"
    units.mkdir(parents=True)
    (units / "qm-test.target").write_text(
        "[Unit]\nDefaultDependencies=no\nRequires=qm-test-ready.service\n"
        "After=qm-test-ready.service\n")
    # Readiness here uses PID 1's /run/systemd/notify, distinct from crun's proxy.
    (units / "qm-test-ready.service").write_text(
        "[Unit]\nDefaultDependencies=no\n[Service]\nType=notify\n"
        "NotifyAccess=all\nRemainAfterExit=yes\n"
        "ExecStart=/usr/bin/systemd-notify --ready\n"
        "StandardOutput=null\nStandardError=null\n")

    config = json.loads((SOURCE / "qm.json").read_text())
    config["root"]["path"] = str(root)
    config["process"]["args"] = ["/usr/lib/systemd/systemd", "--unit=qm-test.target"]
    config["process"]["env"] += ["SYSTEMD_GENERATOR_PATH=/run/no-generators",
                                  "SYSTEMD_ENVIRONMENT_GENERATOR_PATH=/run/no-generators"]
    config["process"].pop("selinuxLabel")
    config["process"].pop("rlimits")
    config["linux"].pop("mountLabel")
    config["linux"].pop("sysctl")
    config["linux"]["resources"] = {"devices": []}
    config["mounts"] = [m for m in config["mounts"] if m["destination"] not in
                        ("/var", "/var/tmp", "/sys/fs/selinux")]
    config["mounts"] += [
        {"destination": "/usr", "source": "/usr", "type": "bind", "options": ["rbind", "ro"]},
    ]
    for mount in config["mounts"]:
        if mount["destination"] == "/etc":
            mount["source"] = str(etc)
        elif mount.get("type") == "devpts":
            mount["options"] = ["gid=0" if o == "gid=5" else o for o in mount["options"]]
    base = directory / "base.json"
    base.write_text(json.dumps(config))
    filename = directory / "qmc.conf"
    filename.write_text(json.dumps({"base-config": str(base), "userns": "off",
                                  "seccomp": str(SOURCE / "tests/seccomp-no-rt.json")}))
    runtime = directory / "runtime"

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify, \
            (directory / "console.log").open("w+") as log:
        notify.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        notify.bind("/run/systemd/notify")
        process = subprocess.Popen(
            [str(LAUNCHER), "launch", "--verbose", "--runtime-dir", str(runtime),
             "--config", str(filename)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env={**os.environ, "NOTIFY_SOCKET": "/run/systemd/notify"})
        try:
            notify.settimeout(10)
            message, ancillary, flags, address = notify.recvmsg(
                256, socket.CMSG_SPACE(struct.calcsize("3i")))
            assert message == b"READY=1", message
            credentials = next(data for level, kind, data in ancillary
                               if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS)
            assert struct.unpack("3i", credentials)[0] == process.pid
            notify.close()  # Release crun's notification barrier.
            result = subprocess.run(
                [str(LAUNCHER), "exec", "--runtime-dir", str(runtime),
                 "/usr/bin/systemctl", "is-active", "qm-test-ready.service"],
                text=True, capture_output=True, timeout=5)
            assert result.returncode == 0 and result.stdout == "active\n", result
            result = subprocess.run(
                [str(LAUNCHER), "exec", "--runtime-dir", str(runtime),
                 "/usr/bin/cat", "/etc/machine-id"],
                text=True, capture_output=True, timeout=5)
            assert result.returncode == 0, result
            assert result.stdout.strip() == HOST_MACHINE_ID, result
            result = subprocess.run(
                [str(LAUNCHER), "exec", "--runtime-dir", str(runtime),
                 "/usr/bin/systemctl", "start", "systemd-machine-id-commit.service"],
                text=True, capture_output=True, timeout=10)
            assert result.returncode == 0, result
            result = subprocess.run(
                [str(LAUNCHER), "exec", "--runtime-dir", str(runtime),
                 "/usr/bin/systemctl", "show", "--property=Result", "--property=ConditionResult",
                 "systemd-machine-id-commit.service"],
                text=True, capture_output=True, timeout=5)
            assert result.returncode == 0 and "Result=success\n" in result.stdout, result
            condition = "yes" if MACHINE_ID_STATE == "uninitialized" else "no"
            assert "ConditionResult=" + condition + "\n" in result.stdout, result
            assert (etc / "machine-id").read_text().strip() == HOST_MACHINE_ID
            result = subprocess.run(
                [str(LAUNCHER), "exec", "--runtime-dir", str(runtime),
                 "/usr/bin/cat", "/proc/self/mountinfo"],
                text=True, capture_output=True, timeout=5)
            assert result.returncode == 0, result
            assert not any(line.split()[4] == "/etc/machine-id"
                           for line in result.stdout.splitlines()), result
            os.kill(process.pid, signal.SIGRTMIN + 3)
            assert process.wait(timeout=10) == 0
            log.seek(0)
            assert "running in system mode" in log.read(), "missing early systemd console output"
            print("Systemd PID 1 inherits the host machine ID, handles machine-id commit "
                  f"({MACHINE_ID_STATE}), accepts service notifications and forwards readiness", flush=True)
        except BaseException:
            log.seek(0)
            print(log.read()[-8000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            subprocess.run([str(LAUNCHER), "cleanup", "--runtime-dir", str(runtime)], check=True)
        assert not runtime.exists()
