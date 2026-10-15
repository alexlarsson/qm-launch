qmc is an experimental podman-less launcher for the qm container,
using crun directly. The intent is to be smaller and boot faster. It
is hardcoded for the needs of qm, and is not flexible enough for any
kind of generic use.

The goal of this work is to eventually be inserted into the main
qm module, replacing the quadlet qm.container with qmc.service.
Additionally the "qmc" binary on the host replaces various things
in qm, like "podman exec qm", and "qmctl". Also, the more flexible
config drop-ins for qmc is intended to be good enough to replace
all use of quadlet dropins as well as the oci hooks currently used
by qm.

Build and install:

Build dependencies are a C compiler, Meson, json-c, libmnl, libnftables and
libsystemd (version 254 or newer) development files. Runtime dependencies include
crun and QM.

```sh
meson setup build --prefix=/usr
meson compile -C build
meson test -C build --print-errorlogs
sudo meson install -C build
sudo systemctl daemon-reload
```

The installed `qmc.service` sits alongside the existing `qm.service`,
so that this can install in parallel with the regular qm
quadlet. (Although this is just during testing, eventually qm.service
will go way once this is merged into qm.)

The defaults use `qm` as hostname and OCI container ID and `/run/qm`
for runtime files. When set, `RUNTIME_DIRECTORY` supplies the runtime path;
systemd sets it from the service's `RuntimeDirectory` setting. `--runtime-dir`
overrides that path. The launcher sets up QM networking using the configured
IPv4 addresses.
Use `qmc exec` to run a command inside QM and `qmc inspect` to examine its
configuration. `qmc signal` publishes host state for services inside QM.
The service uses the internal `launch` and `cleanup` commands to manage QM's
lifetime, and `update-resolv-conf` to refresh its DNS and hosts files.
These commands are hidden from the top-level help. Each command's `--help`
describes its own options, and each command accepts only those options.
Options follow the subcommand, for example `qmc inspect --help` or
`qmc exec --verbose /bin/bash`.

Container settings come from JSON configuration files, loaded in this order:

1. `/usr/share/qm/qmc.conf`
2. `/usr/share/qm/qmc.conf.d/*.conf`, sorted by filename
3. `/etc/qmc.conf`
4. `/etc/qmc.conf.d/*.conf`, sorted by filename

Missing main files and directories are allowed. Each file is a JSON object;
later scalar values override earlier values for the same key. Mount, device,
capability and unmask lists accumulate in configuration load order; an empty
list adds no operations. Drop-ins with the same
filename in both directories are both read. Sorting uses byte order, independent
of the host locale. Files with other suffixes are ignored. Invalid JSON, unknown
keys and incorrect value types cause an error identifying the file.

The installed vendor configuration supplies QM's paths and default capabilities
and enables verbose logging. To customize the hostname and disable debug logging,
for example, create `/etc/qmc.conf.d/50-local.conf` containing:

```json
{
  "hostname": "my-qm",
  "verbose": false
}
```

Inspect the merged settings without starting QM or writing runtime files:

```sh
qmc inspect
qmc inspect --field rootfs
```

This reads the same configuration layers as startup; `--config FILE` selects a
standalone file. The output uses the `qmc.conf` format, including defaults,
with separate `mounts`, `devices` and `capabilities` lists. Entries retain
their load order within each list. Memory limits are
printed as bytes or `"infinity"`; the derived subnet is omitted. The output can
be saved and used with `--config`.

If `rootfs` has no override, inspection reads it from the OCI base configuration.
Device selections, seccomp profiles and user namespace mappings are not resolved
during inspection.

Read the OCI configuration saved by the latest launch or `--prepare-only` run:

```sh
qmc inspect --oci
qmc inspect --oci --field /linux/devices
```

`--oci` reads `config.json` from the runtime directory without loading the current
configuration, so it also works after configuration files have changed or become
invalid. It fails if no saved bundle exists. `--runtime-dir` and
`RUNTIME_DIRECTORY` select the bundle as for `exec`; `--config` cannot be combined
with `--oci`.

`--field` accepts a top-level key or a JSON pointer such as `/root/path`.
Strings are printed without JSON quotes; other values remain JSON. Missing
fields cause an error. Without `--field`, inspection prints the whole JSON object.

Supported keys are:

| Key | Value |
| --- | --- |
| `base-config` | Absolute path to the OCI base JSON (default `/usr/share/qm/qm.json`) |
| `hostname` | Container hostname (default `qm`) |
| `ip` | Container IPv4 address (default `10.90.0.2`) |
| `gateway` | Host veth IPv4 address (default `10.90.0.1`) |
| `prefix-length` | JSON integer from 1 to 31 (default `24`) |
| `rootfs` | Absolute path overriding the OCI root filesystem |
| `seccomp` | Absolute path to an OCI or containers-style seccomp JSON file |
| `resolv-conf` | Absolute path to the host resolver file; otherwise select the available host resolver automatically |
| `userns` | `auto` (default), `off`, or `required` |
| `verbose` | JSON boolean enabling launcher phase logging and crun debug output |
| `memory-max` | Payload memory limit: positive integer bytes, a size string such as `"5G"`, or `"infinity"` |
| `mounts` | Ordered array of bind mount additions, replacements and removals |
| `devices` | Ordered array of device additions, replacements and removals |
| `capabilities` | Ordered array of objects with a capability `name` and optional boolean `remove` (default false) |
| `unmask` | Array of container paths to remove from masked and readonly path lists, or `["ALL"]` |

Addresses, paths and mode values are strings. IP and gateway must be distinct
usable addresses in the same subnet; the subnet is derived from `ip` and
`prefix-length` after all configuration layers are loaded. For example:

```json
{
  "ip": "172.30.20.2",
  "gateway": "172.30.20.1",
  "prefix-length": 28
}
```

The root filesystem path must come from `rootfs` or the OCI base configuration.
The installed vendor file sets `rootfs` to `/usr/lib/qm/rootfs`; the shipped
`qm.json` keeps the root's read-only policy and leaves its path to configuration.
The vendor file also sets `seccomp` to `/usr/share/qm/seccomp-no-rt.json`.
Without `seccomp`, the base configuration must contain an inline filter.

The writable binds from `/etc/qm` to `/etc`, `/var/qm` to `/var`, and
`/var/qm/tmp` to `/var/tmp` are ordinary entries in the vendor configuration's
`mounts` list. They are absent from the shipped OCI base and can be replaced or
removed by later mount operations. Each source is independent. To relocate all
three, for example, add a drop-in containing:

```json
{
  "mounts": [
    {"source": "/srv/qm/etc", "destination": "/etc"},
    {"source": "/srv/qm/var", "destination": "/var"},
    {"source": "/srv/qm/var/tmp", "destination": "/var/tmp"}
  ]
}
```

The vendor configuration also supplies optional `/dev/kvm` and `/dev/fuse`
devices and an optional read-only host `/etc/localtime` bind mount. These are
ordinary configuration entries; standalone `--config` files must include any
of these defaults they need. To use the regular containers seccomp profile
instead of QM's additional realtime scheduling restrictions, set `seccomp`
to `/usr/share/containers/seccomp.json`.

Mount objects have absolute `source` and `destination` paths, an optional
boolean `optional` (default false), and an optional `options` array of additional
mount flags. `qmc` always appends `rbind` and `rprivate` when generating the OCI
configuration, including when `options` is omitted or empty; these flags need
not be listed in the configuration. Device objects have an absolute `source`, an
optional `destination` (defaulting to the source), and an optional boolean
`optional`. Character and block devices are added as OCI device nodes with the
source's major/minor,
permission bits, UID and GID, plus a matching `rwm` device cgroup permission.
The runtime creates the nodes using the container's user namespace mappings.
Use `mounts` when a device bind mount is wanted instead.

Device sources also accept directories and glob patterns (`*`, `?`, and bracket
expressions). Directories are searched recursively for character and block
devices, ignoring ordinary files and symlinks within the directory. Glob matches
are sorted, and matching directories are searched in the same way. Without an
explicit destination, each discovered device keeps its source path. For a directory,
an explicit destination remaps its contents there. For a glob, it remaps the tree
below the first wildcard component: source `/dev/dri/render*` with destination
`/dev/gpu` creates `/dev/gpu/renderD128`, etc. Backslashes in patterns are literal.
Selections are expanded at startup; later host device changes are not tracked.

For either kind, `optional: true` skips a missing source; for device selections,
it also permits no matching devices. Other lookup errors still fail.

Device objects can instead select a named seat with `{"seat": "seat0"}`.
Seat entries accept `seat`, `optional` and `remove`; they have no `source`,
`destination` or mount options. Names follow logind's seat naming rules: `seat`
followed by one or more letters, digits, underscores or hyphens, up to 255 bytes
in total. Discovery uses libsystemd's device API and the host udev database,
including device-node descendants of seat-tagged parents. The nearest tagged
ancestor determines membership; an absent or empty `ID_SEAT` means `seat0`.
Selected nodes keep their normal paths and use the same metadata and cgroup
handling as other device entries. `optional: true` permits an empty selection.

Later entries for the same seat replace earlier selections;
`{"seat": "seat0", "remove": true}` cancels an earlier selection. Multiple seat
entries can select multiple seats. Individual selected nodes can also be overridden
or removed by later destination operations, as with directory and glob selections.
Seat membership is evaluated at startup, without hotplug or session management.

The old QM seat hook also added common input and GPU render devices regardless of
seat. To retain those additions, list them explicitly alongside the seat selector:

```json
{
  "devices": [
    {"seat": "seat0", "optional": true},
    {"source": "/dev/input/event[0-9]*", "optional": true},
    {"source": "/dev/input/mice*", "optional": true},
    {"source": "/dev/input/mouse[0-9]*", "optional": true},
    {"source": "/dev/dri/render*", "optional": true}
  ]
}
```

Container destination is the identity for mounts and devices. Within each list,
later entries replace earlier requests at that destination. An object with
`"remove": true` requires only `destination` and `remove`; it removes the
mount/device at that destination, and a later addition in the same list can restore
it. Replaced requests are discarded before probing their sources. Device directories
and globs expand into individual destinations, which later device entries can override
or remove. Each list accumulates across all configuration files before OCI generation;
mounts are applied first, then devices. Device entries therefore take precedence at
overlapping destinations regardless of drop-in order. Device access is granted after
resolving these operations. Existing OCI mounts are replaced in place; new
mounts are appended in first-destination order, so place parent mounts before
their children. For example, `/etc/qmc.conf.d/50-workload.conf` could contain:

```json
{
  "memory-max": "5G",
  "devices": [
    {"destination": "/dev/kvm", "remove": true},
    {"source": "/dev/net/tun", "optional": true},
    {"source": "/dev/snd", "optional": true},
    {"source": "/dev/dri/render*", "optional": true}
  ],
  "mounts": [
    {"destination": "/etc/localtime", "remove": true},
    {
      "source": "/var/lib/shared",
      "destination": "/var/lib/shared",
      "options": ["ro"]
    }
  ],
  "capabilities": [{"name": "CAP_SYS_RESOURCE"}]
}
```

Configuration paths are normalized lexically: repeated slashes, trailing slashes,
and `.` or `..` components are resolved without filesystem access or following
symlinks. Mount/device destinations `/`, `/etc/hosts`, `/etc/hostname`,
`/etc/resolv.conf`, `/run/.containerenv` and `/run/secrets` are reserved. Parent
mounts are inserted before existing child mounts so binds such as `/etc/hosts`
remain visible when adding `/etc`.

`memory-max` sets `linux.resources.memory.limit` on the `payload` cgroup,
including nested containers. It leaves the sibling `supervisor` cgroup alone;
service-wide systemd memory limits still constrain both. Omission preserves
any limit from the base OCI JSON; `"infinity"` explicitly sets an unlimited
payload limit. Size strings use binary units `K`, `M`, `G`, `T`, `P` or `E`,
optionally followed by `B` or `iB`; `"5G"` means 5 × 1024³ bytes. Zero, negative,
fractional and overflowing values are rejected.

Capability entries require a `name`; `remove` defaults to false. Additions affect
the bounding, effective and permitted sets; removals affect all five capability
sets. Names are case-insensitive, with an optional `CAP_` prefix. The special name
`all` adds every capability known to the launcher, or clears all five sets when
used with `remove: true`. Inspection preserves this selector without expanding it.
The vendor `qmc.conf` starts with `all`, then removes `CAP_SYS_BOOT` and
`CAP_SYS_RESOURCE`; the base OCI JSON contains no capability list.

Entries accumulate across configuration files and are applied in list order, so
a later entry can remove an earlier addition or add a capability back after
removal. For example:

```json
{
  "capabilities": [
    {"name": "all", "remove": true},
    {"name": "CAP_SYS_RESOURCE"}
  ]
}
```

Changes are applied before resolving capability-dependent seccomp rules.
`unmask` removes exact paths from both OCI
`maskedPaths` and `readonlyPaths`; it does not change explicit read-only mounts.

`qmc.service` runs `qmc launch`; container settings no longer require editing
its command line. `--config FILE` on `launch`, `inspect` or `update-resolv-conf` loads
only that file instead of the standard layers. Launch no longer takes a positional
OCI config argument. `--runtime-dir` and `--prepare-only` remain command-line
options and are rejected as configuration keys. `--verbose` remains available
to force debug logging for an individual command.

Cleanup uses recorded runtime state and does not read these configuration files.
Exec also works independently of launch configuration. Both accept `--runtime-dir`
and `--verbose`. Read service output with
`journalctl -b -u qmc.service --no-pager`. For command debugging, use
`qmc exec --verbose COMMAND`.

Host services can publish named signals independently of container startup:

```sh
qmc signal network-ready
qmc signal --clear network-ready
```

Signals are empty files in `/run/qm/signals`. The supplied OCI base binds this
directory read-only at `/run/host/qmc-signals` inside QM. The command accepts
`--runtime-dir` and `RUNTIME_DIRECTORY` as for launch, and does not read launch
configuration or require a running container. Names start with an ASCII letter
or digit and contain only ASCII letters, digits, `.`, `_` and `-`.
Setting and clearing signals are idempotent. Signals survive launch, cleanup,
service stops and container restarts until explicitly cleared; a host reboot
clears them. The directory inode stays the same so existing bind mounts and
inotify watches continue to see changes.

For example, a host service ordered after the host's `network-online.target`
could set `network-ready`. A `.path` unit in the QM rootfs could activate an
existing container service:

```ini
[Path]
PathExists=/run/host/qmc-signals/network-ready
Unit=network-dependent.service

[Install]
WantedBy=multi-user.target
```

`PathExists=` checks the initial state too, so a signal published before QM
starts is observed. A oneshot service triggered this way should use
`RemainAfterExit=yes` to avoid repeated activation while the marker exists.
Starting a `.path` unit does not wait for the marker; gating
`network-online.target` requires a wait service in the QM rootfs.
See [systemd.path](https://github.com/systemd/systemd/blob/main/man/systemd.path.xml).
The host publisher and container services are configured by the QM rootfs;
qmc supplies the generic signal transport.

The supplied OCI configuration enables a pseudo-terminal for QM's
`/dev/console`. Crun hands its master descriptor to a launcher child, which
copies console output to the launcher's stdout, captured by the service journal.
This includes systemd's early startup output, using its normal logging settings.
The relay provides output only; use `qmc exec` for an interactive shell.
It drains the console when the container exits and stops if crun dies.
Custom configurations with `process.terminal=false` retain ordinary stdio.

Run a command in the running container with `qmc exec`. For an interactive shell
in the container started by `qmc.service`:

```sh
sudo qmc exec /bin/bash
```

All arguments after the container command belong to it, including options:

```sh
sudo qmc exec /bin/bash -c 'systemctl status'
```

User namespaces are automatic when both `/etc/subuid` and `/etc/subgid` contain
a `qm_root` range, as produced by QM's `userns enable` helper at image build
time. The launcher maps container ID zero to the start of each range and uses
the recorded range size. With neither entry, it launches without a user
namespace. A partial or invalid configuration fails before crun starts.
`"userns": "off"` disables detection; `"userns": "required"` fails when no ranges
exist. User namespace
operation with the QM rootfs, bind mounts and SELinux policy still needs a
live integration test.

The `seccomp` setting accepts OCI JSON or the supplied containers-style profile. The
latter's `archMap` and `includes`/`excludes` conditions are resolved against
the host architecture and the configured bounding capabilities. Conditions
other than `arches` and `caps` are rejected. Without this setting, the base
config's inline seccomp filter is retained. The supplied `qm.json` contains
only the static configuration; the vendor `qmc.conf` supplies the seccomp path. Hostname, cgroup path, network namespace path, and generated mount sources
are filled in by the launcher. The resulting OCI bundle is written as
`/run/qm/config.json` when launched by `qmc.service`.

The launcher passes the host's `/etc/machine-id` to systemd through
`--machine-id=`, overriding any existing container ID. Systemd manages the
container's own machine-id file, including committing a transient ID when needed.
If the host file contains an invalid ID, including the first-boot value
`uninitialized`, the launcher warns and leaves the init arguments unchanged so
systemd can select the container's ID.
Custom init commands retain their arguments.
The base JSON defines the `/dev/shm` tmpfs. The vendor configuration defines
the read-only host `/etc/localtime` bind mount, omitted when its source is missing.
Runtime setup replaces
Podman's generated mount sources and removes
`container_uuid`. Systemd starts the launcher in
the service's `supervisor` subgroup. The launcher strips that component from
its cgroup path, and crun creates the sibling `payload` cgroup.
The container retains its cgroup namespace and writable cgroup mount.

The service's namespace is pinned at `/run/qm/netns`. The launcher creates
a veth pair through rtnetlink, with `qm-veth` on the host and `eth0` inside
QM. The gateway address is assigned directly to `qm-veth`; no bridge is needed.
The defaults use subnet `10.90.0.0/24`, gateway `10.90.0.1` and container address
`10.90.0.2`; `qmc.conf` can override the addresses and prefix length.
The interfaces inherit the host's default IPv4 route MTU, falling
back to 1500 when no default route exists. The MTU is chosen once during setup.
For forwarded TCP connections, rules scoped to QM's veth clamp the MSS in
both SYN and SYN+ACK packets using the current route MTU. This accommodates
an uplink that appears later or has a smaller MTU when a new connection starts.
It does not adjust existing connections after a route change or solve MTU
problems for UDP. Path MTU discovery still requires the relevant ICMP messages
to reach the endpoints, especially for bottlenecks beyond the host's uplink.
Host uplink configuration can finish later; QM uses the host's routes for
outbound traffic without binding its NAT rules to a particular uplink address.
The namespace creation child brings up loopback before pinning the namespace,
so local networking is ready before container init starts. The launcher then
starts the remaining network setup in the background and execs crun immediately.
Container boot overlaps veth, address, route and firewall setup; `eth0` and
connectivity become available when the job finishes. Crun remains the service's
main process.

Forwarding and masquerading rules live in a dedicated nftables table,
`ip qm_launch`. The background network job configures the interfaces and
installs the firewall rules in the same process. The host veth stays down
until setup finishes successfully. A temporary monitor aborts crun if network
setup fails or crashes. Cleanup waits for setup workers using a lock on the saved
state file, including after crun exits before setup finishes.
Setup preserves unrelated tables and refuses existing QM
interface or table names. Cleanup checks the recorded interface and table
ownership. The fixed interface and table names support one QM instance per host.
QMC's forwarding filter rejects traffic from QM with a source address outside
its configured subnet. It permits other forwarded traffic, including unsolicited
inbound connections to QM when a route exists. Masquerading does not provide an
inbound access policy. Host-to-QM traffic uses the host output path rather than
the forwarding filter.

An additional host firewall must permit the intended QM traffic in its own
policy; QMC's accept verdicts cannot override drops in other base chains.
Configure forwarding from QM's veth and return traffic, plus any intended
inbound services, through that firewall's supported configuration mechanism.
Configure host input/output permissions separately for communication with QM.
Firewall reloads must preserve `ip qm_launch`; a global `flush ruleset` removes
QM's NAT, source filtering and MSS rules. QMC installs its rules during setup
and does not automatically restore them after deletion. A reload that removes
them must coordinate restarting `qmc.service` after the host policy is applied.

The installed `00-qm-forwarding.conf` enables host IPv4 forwarding early in the
host sysctl pass, before the remaining IPv4 tuning. Changing `ip_forward` resets
other IPv4 settings, so the launcher checks that forwarding is enabled and
never changes it. Forwarding remains enabled after cleanup as host policy.
For manual launches before the host sysctl pass, apply the installed forwarding
drop-in with `systemd-sysctl` first, then apply the remaining host tuning.
The installed `90-qm-launch.conf` sysctl drop-in preserves interface settings when udev
runs systemd-sysctl. Missing interfaces are ignored during the early-boot pass;
udev applies the settings when the interfaces appear. Setup creates no files
in `/run/sysctl.d`.

IPv6 and DNS proxying are disabled. The `resolv-conf` setting
selects a file to copy; the default uses systemd-resolved's upstream file when
available, otherwise `/etc/resolv.conf`. A missing resolver file produces an
empty container resolver file. Nameservers must be reachable from
the container, so a host loopback resolver is unsuitable.

`qmc.service` starts `qmc-resolv-conf.path` before launching QM. It watches
`/etc/resolv.conf`, `/etc/hosts` and systemd-resolved's upstream file. Changes
trigger `qmc-resolv-conf.service` to run `qmc update-resolv-conf`; changes during
startup wait until QM is ready. It updates
`/run/qm/files/resolv.conf` and `/run/qm/files/hosts` in place so the container's
bind mounts see the new contents. Shorter contents are padded with spaces before
truncation. The hosts file retains the generated gateway and container hostname
entries, using the hostname and addresses saved at startup in `files/hosts.conf`.
Configuration changes to these values take effect on the next launch.
Missing source or runtime
files are left alone. The watcher stops with `qmc.service`.
For manual runs, invoke `qmc update-resolv-conf` with the same `--runtime-dir`
and `--config` override used at startup, if any.

`qmc.service` uses `DefaultDependencies=no` so it can start before `basic.target`.
It waits for the mounts backing `/usr/lib/qm/rootfs`, `/etc/qm` and `/var/qm`,
host filesystem remounting, static device-node setup, tmpfiles setup (including
`/var/qm/tmp`) and the host sysctl pass.
The sysctl service is ordered after module loading; completing the pass first
prevents it from overwriting qmc's network settings. QM does not wait for host
network connectivity, DNS, D-Bus or all udev events. The DNS updater can also
run before `basic.target`. Explicit shutdown ordering keeps container shutdown
and cleanup ahead of filesystem unmounting.

The service uses `KillMode=mixed` and `KillSignal=SIGRTMIN+3`: systemd signals
foreground crun, which forwards the signal to container PID 1 for orderly
shutdown. After ten seconds systemd kills any remaining service processes.
`ExecStopPost` calls `qmc cleanup` to delete surviving crun state,
tear down networking, and unpin the namespace, including after startup failure.
The launcher also attempts cleanup if setup or exec fails. For manual runs,
invoke `qmc cleanup` after crun exits, using the same `--runtime-dir` if overridden.
This also applies when a background network failure terminates crun.
Setup records teardown identities in `/run/qm/state.json` before creating
namespaces, network interfaces or the container, and updates the file
atomically. It records the crun executable,
state directory and container ID, the bundle directory, the namespace pin path,
and the network interface names, nftables table and owner. Cleanup uses only
this recorded state; startup options and current network defaults are not
needed. A failed
cleanup retains the state for a retry. Successful cleanup removes the recorded
bundle contents recursively, including crun caches and the state file, preserving
the `signals` directory and its markers. It leaves
the runtime directory itself intact, preserving systemd's ownership, mode and
SELinux label. This also applies to manual launches; remove the directory
separately if desired.
Missing resources count as already cleaned up.
Normal startup retries cleanup before writing a new bundle, warns if cleanup
is incomplete, and continues with setup. The service preserves the runtime
directory across stops so the cleanup journal survives, and waits five seconds
between restart attempts.
Without saved state, cleanup leaves unrecorded resources alone. `qmc launch --prepare-only`
does not record ownership or perform cleanup, and refuses pending cleanup state.
Use one launcher per runtime directory.
Avoid systemd filesystem/network namespace sandbox options: cleanup must see
the namespace mount created during startup. The service uses `Type=notify`:
crun inherits systemd's `NOTIFY_SOCKET`, exposes a proxy socket inside QM, and
forwards `READY=1` from the container's systemd. The launcher gives crun a
symlink at `/run/qm/notify.sock` pointing to the host socket, placing the
container proxy at `/run/qm/notify.sock/notify`. This leaves
`/run/systemd/notify` available for QM's own services. The alias follows
`--runtime-dir`; the host notify socket must use an absolute filesystem path.
Startup completes when QM
finishes booting. If readiness never arrives, the service's startup timeout
applies. This requires crun built with systemd support.

Inspect the generated bundle without privileged setup:

```sh
build/qmc launch --prepare-only --runtime-dir /tmp/qm-preview
```

This uses the installed configuration. Add `--config /absolute/path/qmc.conf`
to preview a standalone configuration. It writes the bundle and files, without
changing namespaces or cgroups or applying SELinux labels.

The optional live smoke test runs with temporary user, mount and network
namespaces, plus a delegated transient user service. It exercises cgroup
delegation, payload memory limits, native networking, MTU selection, NAT traffic, foreground crun,
TCP MSS clamping with a late smaller-MTU uplink, host firewall coexistence,
graceful shutdown, failure rollback and crash recovery. It also checks that
interface and firewall setup use the same process and links stay down until
the firewall is ready. It boots container
PID 1 with network setup paused, checks that job failures and crashes abort
startup, and checks cleanup waits for active setup workers. It uses
a small test init and a separate systemd boot test using the host's `/usr`,
without the QM rootfs or its SELinux policy. The systemd test checks
notifications from services inside the container, readiness forwarded to
the host, host machine-ID inheritance and committing an uninitialized ID. It uses
`tests/seccomp-no-rt.json` and disables host subid detection for the live launch.
Device and subid preparation tests use private `/dev` and `/etc` mounts. Seat
tests also mount a synthetic sysfs tree and udev database:

```sh
systemd-run --user --service-type=exec --pipe --wait --collect --same-dir \
    -p Delegate=yes -p DelegateSubgroup=supervisor \
    unshare --user --map-root-user --net --mount \
    python3 tests/smoke.py build/qmc
```
