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
#include <arpa/inet.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static void stop(int signum) { (void)signum; _exit(0); }

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--network-check")) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct timeval timeout = { .tv_sec = 4 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        struct sockaddr_in server = { .sin_family = AF_INET, .sin_port = htons(49000) };
        inet_pton(AF_INET, "192.0.2.2", &server.sin_addr);
        const char request[] = "qm-network\n";
        if (fd < 0 || connect(fd, (struct sockaddr *)&server, sizeof(server)) ||
            write(fd, request, sizeof(request) - 1) != sizeof(request) - 1)
            return 10;
        char response[sizeof(request) - 1];
        size_t offset = 0;
        while (offset < sizeof(response)) {
            ssize_t count = read(fd, response + offset, sizeof(response) - offset);
            if (count <= 0)
                return 11;
            offset += count;
        }
        close(fd);
        return memcmp(request, response, sizeof(response)) ? 12 : 0;
    }
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

    # An upstream with no route to QM proves that replies require masquerading.
    run("ip", "netns", "add", "qm-upstream")
    run("ip", "link", "add", "qm-uplink", "type", "veth", "peer", "name",
        "router", "netns", "qm-upstream")
    run("ip", "addr", "add", "192.0.2.1/24", "dev", "qm-uplink")
    run("ip", "link", "set", "qm-uplink", "mtu", "1400", "up")
    run("ip", "netns", "exec", "qm-upstream", "ip", "addr", "add", "192.0.2.2/24", "dev", "router")
    run("ip", "netns", "exec", "qm-upstream", "ip", "link", "set", "router", "mtu", "1400", "up")
    run("ip", "netns", "exec", "qm-upstream", "ip", "link", "set", "lo", "up")
    run("ip", "route", "add", "default", "via", "192.0.2.2")
    run("nft", "add", "table", "ip", "unrelated")
    # A host may already forward packets while disabling forwarding on new links.
    pathlib.Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
    pathlib.Path("/proc/sys/net/ipv4/conf/default/forwarding").write_text("0\n")

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

    def assert_network_removed(runtime):
        links = json.loads(run("ip", "-j", "link", "show"))
        assert not {"qm-br0", "qm-veth"}.intersection(link["ifname"] for link in links)
        assert "qm_launch" not in run("nft", "list", "tables")
        assert "unrelated" in run("nft", "list", "tables")
        assert not (runtime / "netns").exists()
        assert not (runtime / "state.json").exists()
        assert not runtime.exists(), [str(path.relative_to(runtime)) for path in runtime.rglob("*")]

    def assert_masquerading(*command):
        listener = subprocess.Popen(
            ["ip", "netns", "exec", "qm-upstream", sys.executable, "-u", "-c",
             "import socket\ns = socket.socket()\ns.settimeout(10)\n"
             "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
             "s.bind(('192.0.2.2', 49000))\ns.listen()\nprint('LISTENING', flush=True)\n"
             "c, peer = s.accept()\nprint(peer[0], flush=True)\nc.sendall(c.recv(128))\nc.close()\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert listener.stdout.readline() == "LISTENING\n"
            run(*command)
            stdout, stderr = listener.communicate(timeout=5)
            assert listener.returncode == 0, stderr
            assert stdout == "192.0.2.1\n", stdout
        finally:
            if listener.poll() is None:
                listener.kill()
                listener.wait()

    process, runtime = launch('normal with "quotes" and \\slashes', extra=("--hostname", "qm-test"))
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
        container_ip = "10.90.0.2"
        print(run("nsenter", f"--net={runtime / 'netns'}", "ip", "route"), flush=True)
        run("ping", "-c", "1", "-W", "2", container_ip)
        assert "masquerade" in run("nft", "list", "table", "ip", "qm_launch")
        bridge = json.loads(run("ip", "-j", "link", "show", "qm-br0"))[0]
        eth = json.loads(run("nsenter", f"--net={runtime / 'netns'}", "ip", "-j", "link", "show", "eth0"))[0]
        assert bridge["mtu"] == eth["mtu"] == 1400
        assert bridge["address"] == "02:00:0a:5a:00:01"
        assert bridge["ifalias"] == f"qm-launch:{runtime}"
        run("nsenter", f"--net={runtime / 'netns'}", "ping", "-c", "1", "-W", "2", "127.0.0.1")
        pathlib.Path("/proc/sys/net/ipv4/conf/qm-br0/rp_filter").write_text("1\n")
        run("/usr/lib/systemd/systemd-sysctl", "--prefix=/net/ipv4/conf/qm-br0",
            str(SOURCE / "90-qm-launch.conf"))
        assert pathlib.Path("/proc/sys/net/ipv4/conf/qm-br0/rp_filter").read_text().strip() == "2"
        assert_masquerading(str(LAUNCHER), "--runtime-dir", str(runtime),
                            "--exec", "/init", "--network-check")
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
        assert_network_removed(runtime)
        print("Cgroup delegation, native networking, MTU, NAT, exec, signal forwarding and cleanup passed", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))

    crun = "/usr/bin/crun"
    missing_crun = directory / "missing-crun"
    missing_crun.write_text("#!/missing-interpreter\n")
    missing_crun.chmod(0o755)
    failures = [
        ("exec-failure", f"exec {crun}", str(missing_crun), crun),
    ]
    for attempt, message, replacement, executable in failures:
        run("mount", "--bind", replacement, executable)
        try:
            process, runtime = launch(attempt)
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 1, (stdout, stderr)
            assert message in stderr, stderr
            assert_network_removed(runtime)
            print(f"{attempt}: cleanup passed", flush=True)
        finally:
            run("umount", executable)

    # Fail after bridge creation and check that partial setup is rolled back.
    forwarding = directory / "read-only-forwarding"
    forwarding.write_text("1\n")
    target = "/proc/sys/net/ipv4/ip_forward"
    run("mount", "--bind", str(forwarding), target)
    run("mount", "-o", "remount,bind,ro", target)
    try:
        process, runtime = launch("sysctl-failure")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 1, (stdout, stderr)
        assert "set sysctl net/ipv4/ip_forward" in stderr, stderr
        assert_network_removed(runtime)
        print("Partial network setup rolls back on sysctl failure", flush=True)
    finally:
        run("umount", target)

    # Existing objects are not ours just because their names match.
    for kind in ("interface", "table"):
        if kind == "interface":
            run("ip", "link", "add", "qm-br0", "type", "dummy")
        else:
            run("nft", "add", "table", "ip", "qm_launch", "{ comment \"foreign\"; }")
        try:
            process, runtime = launch(f"foreign-{kind}")
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 1, (stdout, stderr)
            assert "already exists" in stderr, stderr
            assert not (runtime / "state.json").exists()
            assert not (runtime / "netns").exists()
            run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
            assert not runtime.exists()
            if kind == "interface":
                run("ip", "link", "show", "qm-br0")
            else:
                assert "foreign" in run("nft", "list", "table", "ip", "qm_launch")
            assert "unrelated" in run("nft", "list", "tables")
        finally:
            if kind == "interface":
                run("ip", "link", "delete", "qm-br0")
            else:
                run("nft", "delete", "table", "ip", "qm_launch")
    print("Setup preserves interfaces and tables owned by other applications", flush=True)

    # A cleanup error must not block setup when the required resources are free.
    runtime = directory / "runtime-cleanup-warning"
    runtime.mkdir(mode=0o700)
    old_crun = directory / "old-crun"
    (old_crun / "qm").mkdir(parents=True)
    (old_crun / "qm/status").touch()
    (runtime / "state.json").write_text(json.dumps({
        "version": 1, "bundle": str(runtime),
        "crun": {"executable": "/bin/false", "root": str(old_crun), "id": "qm"}}))
    process, runtime = launch("cleanup-warning")
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == "READY\n"
        os.kill(process.pid, signal.SIGRTMIN + 3)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "cleanup incomplete; continuing startup" in stderr, stderr
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
        assert_network_removed(runtime)
        print("Cleanup errors warn while allowing a successful launch", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))

    # Restart after a crash without an intervening --cleanup call.
    process, runtime = launch("missed-cleanup")
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == "READY\n"
        process.kill()
        process.wait(timeout=5)
        crashed = process
        assert (runtime / "state.json").exists()
        process, runtime = launch("missed-cleanup")
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == "READY\n"
        crashed.communicate(timeout=5)
        os.kill(process.pid, signal.SIGRTMIN + 3)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
        assert_network_removed(runtime)
        print("Startup recovers a crashed container without prior cleanup", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))

    # Recover a killed launcher even after the namespace pin was removed.
    process, runtime = launch("recovery")
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == "READY\n"
        process.kill()
        process.wait(timeout=5)
        run("umount", str(runtime / "netns"))
        (runtime / "netns").unlink()
        run("ip", "link", "set", "qm-br0", "alias", "another owner")
        result = subprocess.run([str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime)],
                                text=True, capture_output=True)
        assert result.returncode == 1, result.stderr
        assert "another owner" in result.stderr
        assert (runtime / "state.json").exists()
        assert "qm_launch" not in run("nft", "list", "tables")
        run("ip", "link", "show", "qm-br0")
        # An empty alias represents interruption between creation and tagging.
        run("ip", "link", "set", "qm-br0", "alias", "")
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
        assert_network_removed(runtime)
        process.communicate(timeout=5)
        process, runtime = launch("recovery")
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == "READY\n"
        os.kill(process.pid, signal.SIGRTMIN + 3)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))
        assert_network_removed(runtime)
        print("Crash recovery, ownership checks, cleanup retries and restart passed", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(runtime))

    # Stopping after preparation must leave only saved state and the namespace.
    helper = str(LAUNCHER.parent / "test-network")
    prepared = directory / "prepared-network"
    prepared.mkdir(mode=0o700)
    run(helper, str(prepared), "--prepare-only")
    state = json.loads((prepared / "state.json").read_text())
    assert state["network"]["bridge"] == "test-br0"
    links = json.loads(run("ip", "-j", "link", "show"))
    assert not {"test-br0", "test-veth"}.intersection(link["ifname"] for link in links)
    assert "test_network" not in run("nft", "list", "tables")
    run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(prepared))
    assert not (prepared / "state.json").exists()
    assert not (prepared / "netns").exists()
    print("Network preparation saves cleanup state without changing the network", flush=True)

    # Exercise the backend with names, subnet, MAC, MTU and sysctls different from QM.
    custom = directory / "custom-network"
    custom.mkdir(mode=0o700)
    run("ip", "route", "del", "default")
    try:
        run(helper, str(custom))
        bridge = json.loads(run("ip", "-j", "link", "show", "test-br0"))[0]
        assert bridge["address"] == "02:00:ac:1e:14:01"
        assert bridge["mtu"] == 1234
        run("ip", "link", "show", "test-veth")
        address = json.loads(run("nsenter", f"--net={custom / 'netns'}", "ip",
                                 "-j", "-4", "addr", "show", "test0"))[0]
        assert address["mtu"] == 1234
        assert address["addr_info"][0]["local"] == "172.30.20.2"
        assert address["addr_info"][0]["prefixlen"] == 28
        routes = run("nsenter", f"--net={custom / 'netns'}", "ip", "route")
        assert "default via 172.30.20.1 dev test0" in routes, routes
        run("ping", "-c", "1", "-W", "2", "172.30.20.2")
        assert_masquerading("nsenter", f"--net={custom / 'netns'}",
                            str(rootfs / "init"), "--network-check")
        for interface in ("test-br0", "test-veth"):
            assert pathlib.Path(f"/proc/sys/net/ipv4/conf/{interface}/rp_filter").read_text().strip() == "1"
            ipv6 = pathlib.Path(f"/proc/sys/net/ipv6/conf/{interface}/disable_ipv6")
            if ipv6.exists():
                assert ipv6.read_text().strip() == "0"
        state = json.loads((custom / "state.json").read_text())
        assert state["network"]["bridge"] == "test-br0"
        assert state["network"]["veth"] == "test-veth"
        assert state["network"]["nft_table"] == "test_network"
        # The regular launcher cleans up this other configuration using only
        # saved state, including paths and ownership from another directory.
        cleanup_dir = directory / "custom-cleanup"
        cleanup_dir.mkdir(mode=0o700)
        (custom / "state.json").rename(cleanup_dir / "state.json")
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(cleanup_dir))
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(cleanup_dir))
        assert not (cleanup_dir / "state.json").exists()
        assert not (custom / "netns").exists()
        links = json.loads(run("ip", "-j", "link", "show"))
        assert not {"test-br0", "test-veth"}.intersection(link["ifname"] for link in links)
        assert "test_network" not in run("nft", "list", "tables")
        assert "unrelated" in run("nft", "list", "tables")
        print("Non-default network options, NAT, fallback MTU and cleanup from saved state passed", flush=True)
    finally:
        run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(custom))
        if (directory / "custom-cleanup").exists():
            run(str(LAUNCHER), "--cleanup", "--runtime-dir", str(directory / "custom-cleanup"))
        run("ip", "route", "add", "default", "via", "192.0.2.2")

    process, runtime = launch("namespace-create-failure", existing_netns=True)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 1, (stdout, stderr)
    assert f"create {runtime / 'netns'}" in stderr, stderr
    assert (runtime / "netns").exists(), "must preserve a namespace file we did not create"
    print("Namespace creation failure preserves the existing file", flush=True)
