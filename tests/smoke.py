# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

import json
import os
import pathlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile

SOURCE = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = pathlib.Path(sys.argv[1]).resolve()


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stdout}{result.stderr}")
    return result.stdout


INIT = r'''
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void stop(int signum) { (void)signum; _exit(0); }

int main(int argc, char **argv)
{
    if (argc == 2 && (!strcmp(argv[1], "--read-resolv") ||
                      !strcmp(argv[1], "--read-hosts"))) {
        FILE *file = fopen(!strcmp(argv[1], "--read-resolv") ?
                           "/etc/resolv.conf" : "/etc/hosts", "r");
        if (!file)
            return 8;
        char line[4096];
        while (fgets(line, sizeof(line), file))
            fputs(line, stdout);
        int status = ferror(file) ? 9 : 0;
        fclose(file);
        return status;
    }
    if (argc > 1) {
        if (argc != 3 || strcmp(argv[1], "--exec-check") ||
            strcmp(argv[2], "argument with spaces") || getpid() == 1)
            return 7;
        printf("EXEC %s tty=%d\n", argv[2], isatty(STDIN_FILENO));
        return 23;
    }
    char buffer[512];
    FILE *file = fopen("/proc/self/cgroup", "r");
    if (!file || !fgets(buffer, sizeof(buffer), file) || strcmp(buffer, "0::/\n"))
        return 2;
    fclose(file);
    if (mkdir("/sys/fs/cgroup/init.scope", 0755))
        return 3;
    file = fopen("/sys/fs/cgroup/init.scope/cgroup.procs", "w");
    if (!file || fputs("0\n", file) < 0 || fclose(file))
        return 4;
    file = fopen("/sys/fs/cgroup/cgroup.subtree_control", "w");
    if (!file || fputs("+memory +pids\n", file) < 0 || fclose(file))
        return 5;
    file = fopen("/sys/fs/cgroup/init.scope/memory.max", "r");
    if (!file)
        return 6;
    fclose(file);
    signal(SIGRTMIN + 3, stop);
    puts("READY");
    fflush(stdout);
    for (;;) pause();
}
'''

run("mount", "--make-rprivate", "/")
run("mount", "-t", "tmpfs", "tmpfs", "/run")
supervisor = pathlib.Path("/sys/fs/cgroup") / pathlib.Path(
    pathlib.Path("/proc/self/cgroup").read_text().strip().removeprefix("0::")
).relative_to("/")
assert supervisor.name == "supervisor", "run with DelegateSubgroup=supervisor"
service = supervisor.parent

with tempfile.TemporaryDirectory(prefix="qm-launch-smoke-") as temporary:
    directory = pathlib.Path(temporary)
    rootfs = directory / "rootfs"
    rootfs.mkdir()
    init_source = directory / "init.c"
    init_source.write_text(INIT)
    run("/usr/bin/cc", "-o", str(rootfs / "init"), str(init_source))
    for dependency in re.findall(r"(/[^\s()]+)", run("ldd", str(rootfs / "init"))):
        target = rootfs / dependency.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dependency, target)
    config = json.loads((SOURCE / "qm.json").read_text())
    config["root"]["path"] = str(rootfs)
    config["process"]["args"] = ["/init"]
    config["process"].pop("selinuxLabel")
    config["process"].pop("rlimits")
    config["linux"].pop("mountLabel")
    config["linux"]["resources"] = {"devices": []}
    config["linux"].pop("sysctl")
    config["mounts"] = [m for m in config["mounts"] if m["destination"] not in
                        ("/etc", "/var", "/var/tmp", "/sys/fs/selinux")]
    for mount in config["mounts"]:
        if mount["destination"] == "/etc/localtime":
            mount["source"] = str(directory / "missing-localtime")
        if mount.get("type") == "devpts":
            mount["options"] = ["gid=0" if o == "gid=5" else o for o in mount["options"]]
    base = directory / "base.json"
    base.write_text(json.dumps(config))
    resolv = directory / "resolv.conf"

    devices = directory / "devices"
    devices.mkdir()
    etc = directory / "etc"
    etc.mkdir()
    shutil.copy2("/etc/hosts", etc / "hosts")
    for name in ("null", "zero"):
        (devices / name).touch()
    mounts = []
    try:
        for source, target in (("/dev/null", devices / "null"),
                               ("/dev/zero", devices / "zero"),
                               (etc, "/etc"), (devices, "/dev"),
                               (devices / "null", "/dev/null"),
                               (devices / "zero", "/dev/zero")):
            run("mount", "--bind", str(source), str(target))
            mounts.append(str(target))
        subprocess.run([sys.executable, str(SOURCE / "tests/prepare.py"),
                        str(LAUNCHER), str(SOURCE)], check=True,
                       env={**os.environ, "QM_TEST_PRIVATE_PATHS": "1"})
    finally:
        for target in reversed(mounts):
            run("umount", target)
    subprocess.run([sys.executable, str(SOURCE / "tests/exec.py"), str(LAUNCHER)],
                   check=True, env={**os.environ, "QM_TEST_PRIVATE_CRUN": "1"})
    # Keep host-file changes within the smoke test's mount namespace.
    run("mount", "--bind", str(etc / "hosts"), "/etc/hosts")

    def launch(attempt, extra=(), existing_netns=False):
        runtime = directory / f"runtime-{attempt}"
        if existing_netns:
            runtime.mkdir(mode=0o700)
            (runtime / "netns").touch()

        process = subprocess.Popen(
            [str(LAUNCHER),
             "--seccomp", str(SOURCE / "tests/seccomp-no-rt.json"),
             "--resolv-conf", str(resolv), "--userns", "off",
             *extra, str(base)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "RUNTIME_DIRECTORY": str(runtime)},
        )
        return process, runtime

    process, runtime = launch("normal", extra=("--hostname", "qm-test"))
    try:
        ready = False
        output = b""
        while select.select([process.stdout], [], [], 10)[0]:
            chunk = os.read(process.stdout.fileno(), 4096)
            print(chunk.decode(), end="", flush=True)
            output += chunk
            if b"READY\n" in output:
                ready = True
                break
            if not chunk:
                break
        if not ready:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
            raise RuntimeError(f"container did not start: {stdout}{stderr}")
        assert str(process.pid) in (supervisor / "cgroup.procs").read_text().splitlines()
        assert (service / "payload/init.scope").exists()
        assert (service / "cgroup.procs").read_text() == ""
        assert (runtime / "files/resolv.conf").read_text() == ""
        for contents in ("search example.test\nnameserver 192.0.2.53\n",
                         "nameserver 192.0.2.54\n"):
            resolv.write_text(contents)
            run(str(LAUNCHER), "--runtime-dir", str(runtime), "--resolv-conf",
                str(resolv), "--update-resolv-conf")
            assert run(str(LAUNCHER), "--runtime-dir", str(runtime), "--exec",
                       "/init", "--read-resolv") == contents
        hosts_inode = (runtime / "files/hosts").stat().st_ino
        for contents in ("127.0.0.1\tlocalhost\n192.0.2.100\tnew-host\n",
                         "127.0.0.1\tlocalhost\n"):
            (etc / "hosts").write_text(contents)
            run(str(LAUNCHER), "--runtime-dir", str(runtime), "--resolv-conf",
                str(resolv), "--update-resolv-conf")
            expected = (contents + "\n10.90.0.1\thost.containers.internal host.docker.internal\n"
                        "10.90.0.2\tqm-test qm\n")
            assert (runtime / "files/hosts").stat().st_ino == hosts_inode
            assert run(str(LAUNCHER), "--runtime-dir", str(runtime), "--exec",
                       "/init", "--read-hosts") == expected
        network = json.loads((runtime / "network.json").read_text())
        container_ip = next(iter(network["networks"].values()))["static_ips"][0]
        print(run("nsenter", f"--net={runtime / 'netns'}", "ip", "route"), flush=True)
        run("ping", "-c", "1", "-W", "2", container_ip)
        assert "masquerade" in run("nft", "list", "ruleset")
        command = [str(LAUNCHER), "--runtime-dir", str(runtime),
                   "--exec", "/init", "--exec-check", "argument with spaces"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=10)
        assert result.returncode == 23, (result.stdout, result.stderr)
        assert result.stdout == "EXEC argument with spaces tty=0\n", result.stdout
        master, slave = os.openpty()
        try:
            result = subprocess.run(command, stdin=slave, stdout=slave,
                                    stderr=subprocess.PIPE, timeout=10)
            assert result.returncode == 23, result.stderr
            assert os.read(master, 4096) == b"EXEC argument with spaces tty=1\r\n"
        finally:
            os.close(slave)
            os.close(master)
        os.kill(process.pid, signal.SIGRTMIN + 3)
        stdout, stderr = process.communicate(timeout=10)
        print(stdout, end="")
        assert process.returncode == 0, stderr
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
        assert not (runtime / "netns").exists()
        print("Cgroup delegation, networking, exec, signal forwarding and cleanup passed", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))

    crun = "/usr/bin/crun"
    netavark = "/usr/libexec/podman/netavark"
    missing_netavark = directory / "missing-netavark"
    missing_netavark.write_text("#!/missing-interpreter\n")
    missing_netavark.chmod(0o755)
    failures = [
        ("exec-failure", f"exec {crun}", str(missing_netavark), crun),
        ("network-failure", "Netavark setup failed", "/bin/false", netavark),
        ("network-exec-failure", f"exec {netavark}", str(missing_netavark), netavark),
    ]
    for attempt, message, replacement, executable in failures:
        run("mount", "--bind", replacement, executable)
        try:
            process, runtime = launch(attempt)
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 1, (stdout, stderr)
            assert message in stderr, stderr
            assert not (runtime / "netns").exists()
            print(f"{attempt}: cleanup passed", flush=True)
        finally:
            run("umount", executable)

    process, runtime = launch("namespace-create-failure", existing_netns=True)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 1, (stdout, stderr)
    assert f"create {runtime / 'netns'}" in stderr, stderr
    assert (runtime / "netns").exists(), "must preserve a namespace file we did not create"
    print("Namespace creation failure preserves the existing file", flush=True)
