# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

Name:           qm-launch
Version:        0.1.1
Release:        1%{?dist}
Summary:        Launch the QM container directly with crun

License:        GPL-2.0-only
URL:            https://github.com/alexlarsson/qm-launch
Source0:        %{name}-%{version}.tar.xz

BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  pkgconfig(json-c)
BuildRequires:  pkgconfig(libmnl)
BuildRequires:  pkgconfig(libnftables)
BuildRequires:  pkgconfig(libsystemd) >= 254
BuildRequires:  python3
BuildRequires:  systemd-rpm-macros

# QM supplies the seccomp profile, rootfs setup and SELinux policy.
Requires:       qm
Requires:       crun
Requires:       systemd >= 254
%{?systemd_requires}

%description
qmc prepares an OCI bundle and launches the QM container directly with
crun, rtnetlink and nftables. It provides qmc.service alongside QM's existing
service and supports running commands in the container with qmc exec.

%prep
%autosetup -p1

%build
%meson
%meson_build

%install
%meson_install

%check
%meson_test

%post
%systemd_post qmc.service qmc-resolv-conf.path qmc-resolv-conf.service

%preun
%systemd_preun qmc.service qmc-resolv-conf.path qmc-resolv-conf.service

%postun
%systemd_postun qmc.service qmc-resolv-conf.path qmc-resolv-conf.service

%files
%license COPYING
%doc README.md
%{_bindir}/qmc
%{_unitdir}/qmc.service
%{_unitdir}/qmc-resolv-conf.path
%{_unitdir}/qmc-resolv-conf.service
%{_datadir}/qm/qm.json
%{_datadir}/qm/qmc.conf
%dir %{_datadir}/qm/qmc.conf.d
%dir %{_sysconfdir}/qmc.conf.d
%{_prefix}/lib/sysctl.d/00-qm-forwarding.conf
%{_prefix}/lib/sysctl.d/90-qm-launch.conf

%changelog
* Tue Oct 13 2026 Alexander Larsson <alexl@redhat.com> - 0.1.0-1
- Initial package
