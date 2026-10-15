# Integrating qmc into QM

Reviewed QM at `0ce7c1a` and qm-launch at `8167b5f` on 2026-10-15.
This is a proposed commit series; no QM integration changes have been made.
qm-launch's license has been aligned with QM's `GPL-2.0-only` as a prerequisite
to this series.

The agreed scope is CentOS 10, replacing the outer QM Quadlet with qmc and
retiring qmctl. Missing qmctl features, including copying files and several
inspection commands, are acceptable. EPEL/CentOS 9 compatibility is outside
this work.

## Resulting user interface

- Keep `systemctl start/stop/restart/status qm.service` as the service interface.
  Install the native service as `qm.service`, replacing `qm.container`.
- Use `qmc exec`, `qmc inspect`, and `qmc signal` for host interaction.
  Do not introduce a qmctl compatibility wrapper or reproduce its output format.
- Configure the container through `qmc.conf` and JSON drop-ins. Configure service
  resource limits and ordering through `qm.service.d` systemd drop-ins.
- Preserve the default rootfs, writable etc, and writable var locations:
  `/usr/lib/qm/rootfs`, `/etc/qm`, and `/var/qm`.
- Continue supporting Podman and application Quadlets *inside* QM. Hooks used by
  nested or other Podman containers remain separate from the outer launcher.

## Commit series

Each commit should build and pass the checks relevant to it. These are separate
reviewable changes with dependencies, rather than commits that can all be
reordered. The build preparation can land separately. Commits 5–9 form a
coordinated runtime conversion and should be validated as a complete stack
before release; intermediate commits need not each provide a complete deployable
system. Replace each integration point directly with its qmc version. Do not add
a temporary backend selector, dual-format configuration, or compatibility
branches to keep the old outer Quadlet working during the conversion.

### 1. `build: introduce Meson for existing QM build and installation`

Reproduce the current main package's build and install behavior while retaining
the Quadlet. Include SELinux policy compilation/compression, generated file
contexts, man pages, scripts, data, the Python qmctl module, and install modes.
Read the version from `VERSION` rather than adding another version constant.

Use the SELinux development Makefile through a Meson custom target and a small
helper running in the build directory. There is no benefit in reimplementing the
policy compiler's build system. Keep privileged policy installation out of normal
build and staged-install operations.

Retain a small top-level Makefile for developer conveniences such as `rpm`,
`dist`, and `subpackages`. Existing subsystem Makefiles and their independent
Packit pipelines need not be converted in this series.

Validation: compare Make and Meson staged installation manifests, permissions,
and generated policy/context/man-page output on CentOS 10. Verify an out-of-tree
build and the existing subsystem entry points.

### 2. `packaging: build QM with Meson on CentOS 10`

Switch `rpm/qm.spec` to Meson build/install macros and remove its Makefile-editing
workaround. Preserve the existing SELinux installation and removal scriptlets.
Keep distribution archive generation compatible with Packit and local RPM
builds; archives must include tests once `%check` uses them. Do not require a Git
checkout to build a release archive.

Remove EPEL 9 jobs and repository references from `.packit.yaml`, and retire the
CentOS 9 policy/setup workarounds. Make CentOS 10 the primary package/integration
target. Retain useful existing Fedora coverage without making it a prerequisite
for designing the CentOS 10 integration.

Validation: build the RPM from a source archive on CentOS 10 and compare its
payload with the previous package. Check that separately packaged subsystems
still build through their current pipelines.

### 3. `qmc: add the native QM launcher and configuration`

Import the launcher as one coherent implementation. Keep the existing C module
boundaries rather than reconstructing every historical qm-launch change.
Suggested new directories are `src/` for C sources, `data/` for configuration,
and later `systemd/` and `tests/qmc/`. Leave existing QM policy/scripts in place.

Add qmc, the OCI base `qm.json`, vendor `qmc.conf`, and configuration drop-in
directories. Adapt source/test paths to the chosen layout. Keep the existing
rootfs and generated seccomp profile; do not ship the test seccomp fixture as
production policy. Preserve copyright/SPDX notices, update full-license pointers
to QM's `LICENSE`, and reuse that existing license file. Both projects use
`GPL-2.0-only`, so the import needs no additional license text.

Add the compiler, json-c, libmnl, libnftables and libsystemd dependencies, retaining
the launcher's libsystemd >=254 requirement. Make the main RPM architecture
dependent and enable normal C debuginfo generation. Retain QM's `GPL-2.0-only`
license metadata. Replace the standalone `qm-launch.spec` with integration into
QM's existing spec; do not introduce a second launcher package requiring QM.

Validation: compile on CentOS 10, check command help and standalone configuration
inspection, and stage the install. Check architecture-specific dependencies and
package paths, especially on aarch64 and x86_64.

### 4. `tests: add qmc configuration and command regression tests`

Import the useful existing fast tests for configuration loading, inspection,
bundle preparation, command parsing, signals, and utility error handling. These
already check meaningful behavior and are worth keeping. Use Python's standard
unittest runner; no new test framework is needed.

Register them with Meson and RPM `%check`, and add a CentOS 10 CI build/test job.
Require Python when tests are enabled instead of silently omitting most tests
when Python is missing. Keep privileged fixtures in a distinct integration suite
and report skipped checks clearly.

The minimum coverage is configuration precedence and invalid input; mount/device
replacement and removal; seccomp/capability preparation; inspect output; CLI
errors; and cleanup behavior with absent or partial state. Preserve existing
regression cases unless they depend on an implementation detail being removed.

Validation: run the unprivileged suite from a clean build and an RPM source
archive. No running QM instance or host network changes should be required.

### 5. `systemd: integrate the native QM service and host networking`

Adapt `qmc.service.in` into `qm.service.in`; update the resolver watcher/updater
dependencies to match. Keep `qmc` as the executable name. Install the native
service in place of the outer Quadlet and stop installing `qm.container`.

Integrate runtime directory ownership, readiness, cgroup delegation, shutdown and
cleanup, DNS/hosts updates, and the two host sysctl files. Install the sysctl
files with the native backend so importing the binary alone does not change host
forwarding policy. Resolve systemd unit/sysctl paths through the build setup.

Retain the existing service resource defaults and review intentional differences
in the resulting OCI process limits. Verify crun's notification support on the
target package. Test existing host firewall policy together with QM's nftables
rules rather than assuming qmc's accept rules override another firewall.

Validation: verify generated units and the staged payload, then test native-service
start/stop/restart with a prepared rootfs in a disposable CentOS 10 VM. Observe
actual readiness, graceful shutdown, and removal of owned networking resources.

### 6. `setup: use qmc configuration for rootfs and user namespaces`

Update `setup`, `tools/qm-rootfs`, and `userns` for the native backend. Make
`qm-rootfs` use `qmc inspect --field rootfs`. Ensure non-default setup rootfs,
etc and var arguments are reflected in qmc configuration, including the separate
var/tmp mount, and matching service mount dependencies. Preserve image-building
and `--skip-systemctl` use without invoking the Quadlet generator.

Keep sysusers/subuid/subgid generation. For qmc, have `userns enable` write a
JSON `userns: required` setting and `disable` write `userns: off`; merely removing
an override would leave the launcher's automatic subid detection enabled.
Test enable/disable and repeat invocations through the real script functions.

Replace the old Quadlet-specific script behavior directly. Setup and removal
must use the same configured rootfs, etc and var directories. If reading that
configuration fails, removal must stop before deleting any directories.

Validation: default and custom locations, setup without a running system manager,
and userns enable/disable against temporary roots. Do not duplicate production
function bodies inside the revised tests.

### 7. `subsystems: configure outer QM devices with qmc drop-ins`

Convert package-owned outer-container drop-ins and their spec/archive references
to JSON vendor drop-ins under `/usr/share/qm/qmc.conf.d`. Cover KVM/tun, sound,
video, TTYs and seat-related selections where applicable. Replace the old
package-owned outer Quadlet drop-ins with the qmc format.

Replace outer QM annotation/hook use with explicit device/directory/glob/seat
entries. Preserve optional-device behavior and required mount flags. Seat
selection alone does not reproduce the old seat hook's additional common input
and render devices: include those explicitly where intended.

Keep application Quadlets and independently packaged OCI hooks that are used
inside QM or by other Podman containers. Audit these by purpose, not by a global
search-and-replace of every `.container` or hook reference.

Validation: build affected subsystem RPMs and inspect their merged configuration
and resulting OCI device permissions. Exercise at least one real device path in
integration testing where suitable VM hardware is available.

### 8. `tests: exercise the native service and port QM integration checks`

Provide a bounded CentOS 10 VM test of the installed package, its actual service,
and QM rootfs with SELinux enforcing. Basic required checks are readiness,
`qmc exec` arguments/stdin/exit status, read-only root and writable etc/var,
nested Podman/Quadlet operation, networking and DNS updates, graceful stop,
restart, and cleanup after a failed start. Cover userns both enabled and disabled.
Keep test images/workloads local or pinned where practical.

Adapt the useful parts of `smoke.py`/`systemd.py` into separately runnable groups
with explicit prerequisites, deadlines and teardown. Keep focused failure and
network ownership regressions. The current harness is useful, but it disables
SELinux labels and cannot replace the installed-service VM check.

Port existing TMT/E2E helpers and tests from outer `podman exec qm` to `qmc exec`,
and from outer Quadlet configuration to qmc/systemd configuration. Preserve inner
`podman exec` and application Quadlets. Keep isolation, seccomp, read-only-unit,
resource and BlueChi assertions. Retire qmctl-specific tests with commit 9.

Validation: run the native backend's tier-0 checks and relevant FFI tests on
CentOS 10. Record the tests actually executed; a test being discoverable is not
evidence that it passed.

### 9. `qm: retire qmctl and finish the runtime packaging migration`

Remove the unused `qm.container` source and remaining obsolete outer Quadlet
references from build/setup/test integration.
Remove qmctl's Python code, entry point, manual, subpackage, dedicated tests and
CI job. Keep `qmc` as the supported CLI with its current feature set.

Finalize RPM upgrade/removal handling for the converted runtime. Account
for the old generated service, administrator-owned Quadlet overrides, service
enablement, daemon reloads, and old Podman container state. Test a package upgrade
from a running old installation as well as fresh installation and reboot.
Use bounded package obsoletes as needed for installed `qmctl`/historical `qm-ctl`
packages; do not claim command compatibility through a fake CLI alias.

Inventory old administrator configuration and provide an actionable migration
path. Existing `qm.service.d` service overrides should retain their meaning;
Quadlet `[Container]` overrides require conversion. Settings without a qmc
equivalent, such as the documented host network mode, need an explicit migration
limitation. Do not silently ignore them or promise a general Quadlet converter.

Remove the main package's host Podman dependency when its remaining consumers
have been removed. Preserve Podman installation inside QM and test provisioners
that use Podman for their own infrastructure. Retain containers-common/jq while
the production seccomp generator or other installed tools still need them.

Validation: clean installation, upgrade with and without customization, preserved
service activation, reboot, and uninstall. Repeat basic native-service tests on
the final RPM. Only one outer QM runtime should start.

### 10. `docs: update existing QM documentation for qmc`

Keep QM's README mostly as-is, preserving its structure, project overview and
existing topics. Update descriptions and examples affected by the new runtime,
build setup and supported platform. Mention the qmc binary and briefly explain
its `exec` and `inspect` operations, with examples such as:

```sh
qmc exec /bin/bash
qmc inspect
qmc inspect --field rootfs
```

Update references to outer Podman/Quadlet configuration and qmctl to describe
the new service, commands and configuration files. Keep descriptions of Podman
and application Quadlets inside QM. Link to detailed command/configuration
documentation where appropriate. The README changes should stay focused on
making the existing document accurate for qmc.

Update the existing MkDocs pages and man pages in place where they describe the
old runtime. Add user-facing command and configuration details where needed:

- Setup, starting/stopping QM, logs, entering QM, and managing applications inside.
- `qmc exec`, `inspect`, and `signal`, with command examples and exit behavior.
- Configuration locations and precedence, scalar overrides, ordered list
  operations, empty-list behavior, optional devices, mounts, seats, userns,
  hostname/network/DNS settings, and applying changes by restarting QM.
- The distinction between container configuration and systemd service limits,
  including payload `memory-max` versus service-wide `MemoryMax`.
- Small recipes for devices, shared host directories and resource limits; update
  the existing IPC and network guides accordingly.
- Migration from outer Quadlet drop-ins and host Podman/qmctl commands, including
  accepted feature removals and unsupported old settings.

Add `qmc(1)` and a configuration reference such as `qmc.conf(5)`; keep `qm(8)` for
overall operation/setup. Document new build/test instructions and implementation
details in the developer guide, while retaining existing README topics. Keep
internal launch/cleanup commands out of normal user examples.
Document operational network limitations without exposing unnecessary nftables
or cleanup-journal implementation detail. Update MkDocs navigation and affected
demos/examples, preserving examples for application Quadlets inside QM.

Validation: build MkDocs and man pages, check links and obsolete commands, and
run the documented command/configuration examples on the CentOS 10 test image.

## What the current tests establish

The clean qm-launch build and staged install succeeded on the local aarch64 host
with libsystemd 259. All six registered Meson groups passed. The Python groups
reported 109 cases, of which 15 were skipped: four exec fixtures and eleven
preparation fixtures require privileges/private mounts. The network helpers
compile, but the live smoke/systemd harness is not registered with Meson and was
not run during this review.

QM's 62 qmctl Python tests and nine userns shell tests passed. The qmctl tests
primarily mock the old backend and can go away with that tool. Some userns tests
call real functions, while subordinate-ID cases duplicate implementation logic;
replace the latter when updating userns. Existing TMT integration/FFI tests have
valuable behavioral assertions but need their outer runtime access/configuration
ported. They were inspected, not executed during this review.

CentOS 10 package builds, enforcing-SELinux service tests and upgrade tests remain
implementation acceptance checks, not results established by the local build.
